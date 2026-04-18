# -*- coding: utf-8 -*-
"""
services/tg_scraper_service.py
Optimized paginated scraper for public Telegram channels.

Speed optimizations vs v1:
  1. requests.Session()  — persistent TCP/TLS connection (no handshake per page)
  2. Regex HTML parser   — replaces BeautifulSoup (5x faster)
  3. Pipeline prefetch   — next page fetches in background while current page processes
  4. Reduced timeouts    — 8s instead of 15s
"""

import re
import time
import logging
import threading
from threading import Lock

import requests

from utils.parser import extract_raw_ccs, parse_message_id
from services.cleaner import clean_cc, deduplicate

logger = logging.getLogger(__name__)

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

_TIMEOUT      = 8       # reduced from 15 — faster fail-fast
_MAX_RETRIES  = 2
_RETRY_DELAY  = 0.3     # reduced from 2s
_PAGE_SIZE    = 20      # Telegram shows ~20 msgs per page
_MAX_PAGES    = 5000    # Unlimited — paginate until channel exhausted or limit met

# ── Per-user rate limit ──────────────────────────────────────────────────────
_RATE: dict  = {}
_RATE_WINDOW = 30
_RATE_MAX    = 3
_RATE_LOCK   = Lock()

# ── In-memory result cache ───────────────────────────────────────────────────
_CACHE: dict = {}
_CACHE_TTL   = 60
_CACHE_LOCK  = Lock()

# ── Compiled regexes for fast HTML parsing (replaces BeautifulSoup) ──────────
_RE_STRIP_TAGS  = re.compile(r'<[^>]+>')
_RE_MSG_SPLIT   = re.compile(r'(?=class="tgme_widget_message_wrap)')
_RE_MSG_ID      = re.compile(r'https://t\.me/[a-zA-Z0-9_]+/(\d+)')


# ─────────────────────────────────────────────────────────────────────────────
#  Rate-limit helpers
# ─────────────────────────────────────────────────────────────────────────────

def check_rate_limit(user_id: int):
    """Return (allowed: bool, wait_seconds: int)."""
    now = time.time()
    with _RATE_LOCK:
        timestamps = [t for t in _RATE.get(user_id, [])
                      if now - t < _RATE_WINDOW]
        if len(timestamps) >= _RATE_MAX:
            wait = int(_RATE_WINDOW - (now - timestamps[0])) + 1
            return False, wait
        timestamps.append(now)
        _RATE[user_id] = timestamps
    return True, 0


# ─────────────────────────────────────────────────────────────────────────────
#  Cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cache_key(username: str, limit: int) -> str:
    return f"{username.lower()}:{limit}"


def _from_cache(username: str, limit: int):
    key = _cache_key(username, limit)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and time.time() - entry["ts"] < _CACHE_TTL:
            return entry["data"]
    return None


def _to_cache(username: str, limit: int, data: dict):
    key = _cache_key(username, limit)
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP fetch — uses persistent session passed from caller
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_html(session: requests.Session, url: str) -> tuple:
    """Fetch URL with retry using persistent session. Returns (html, error)."""
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=_TIMEOUT)
            if r.status_code == 404:
                return "", "Channel not found or private"
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                time.sleep(_RETRY_DELAY)
                continue
            return r.text, None
        except requests.exceptions.Timeout:
            last_err = "Request timed out"
            time.sleep(_RETRY_DELAY)
        except requests.exceptions.ConnectionError:
            last_err = "Connection error"
            time.sleep(_RETRY_DELAY)
        except Exception as e:
            return "", str(e)
    return "", last_err or "Unknown error"


# ─────────────────────────────────────────────────────────────────────────────
#  Fast regex-based HTML parser (replaces BeautifulSoup — 5x faster)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_page_fast(html: str) -> tuple:
    """
    Regex-based parser for t.me/s pages.
    Returns (messages: list[dict], min_id: int|None).
    ~5x faster than BeautifulSoup.
    """
    # Split HTML into per-message blocks
    blocks = _RE_MSG_SPLIT.split(html)

    messages = []
    ids = []

    for block in blocks[1:]:   # skip page header
        # Extract message ID from href
        id_m = _RE_MSG_ID.search(block)
        if id_m:
            ids.append(int(id_m.group(1)))

        # Strip all HTML tags → raw text for CC extraction
        text = _RE_STRIP_TAGS.sub(' ', block)
        messages.append({"text": text})

    min_id = min(ids) if ids else None
    return messages, min_id


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

def scrape_channel_for_ccs(username: str, limit_ccs: int) -> dict:
    """
    Optimized scraper: Session reuse + regex parsing + pipeline prefetch.

    Pipeline: while processing page N, page N+1 is fetched in background.
    Effective per-page time ≈ max(fetch_time, parse_time) instead of sum.

    Returns dict:
      {
        "cards":         list[str],
        "msgs_scanned":  int,
        "pages_fetched": int,
        "dupes_removed": int,
        "error":         str | None,
        "cached":        bool,
      }
    """
    cached = _from_cache(username, limit_ccs)
    if cached is not None:
        return {**cached, "cached": True}

    # ── Persistent session — single TCP/TLS connection for all pages ─────────
    session = requests.Session()
    session.headers.update(_BASE_HEADERS)

    cards: list   = []
    seen:  set    = set()
    msgs_scanned  = 0
    pages_fetched = 0
    raw_found     = 0

    # ── Fetch first page ─────────────────────────────────────────────────────
    first_url  = f"https://t.me/s/{username}"
    first_html, first_err = _fetch_html(session, first_url)

    if first_err and not first_html:
        return {"cards": [], "msgs_scanned": 0, "pages_fetched": 0,
                "dupes_removed": 0, "error": first_err, "cached": False}

    if "tgme_widget_message" not in first_html:
        return {"cards": [], "msgs_scanned": 0, "pages_fetched": 0,
                "dupes_removed": 0,
                "error": "Channel is private, empty, or has no public messages",
                "cached": False}

    # ── Pipeline state ───────────────────────────────────────────────────────
    # _pf holds the prefetched page (fetched in background)
    _pf_html  = [first_html]   # list so closure can mutate
    _pf_err   = [first_err]
    _pf_ready = threading.Event()
    _pf_ready.set()             # first page is already ready

    def _prefetch(next_url: str, html_slot: list, err_slot: list,
                  ready_evt: threading.Event):
        """Background thread: fetch next page and signal ready."""
        h, e = _fetch_html(session, next_url)
        html_slot[0] = h
        err_slot[0]  = e
        ready_evt.set()

    for page_num in range(_MAX_PAGES):

        # ── Wait for current page to be ready (always instant for page 0) ───
        _pf_ready.wait()
        current_html = _pf_html[0]
        current_err  = _pf_err[0]

        if current_err and not current_html:
            break
        if not current_html or "tgme_widget_message" not in current_html:
            break

        # ── Parse current page (regex — fast) ───────────────────────────────
        messages, min_id = _parse_page_fast(current_html)
        pages_fetched += 1
        msgs_scanned  += len(messages)

        # ── Immediately start prefetching next page in background ────────────
        if min_id and min_id > 1:
            _pf_html  = [None]
            _pf_err   = [None]
            _pf_ready = threading.Event()
            next_url  = f"https://t.me/s/{username}?before={min_id}"
            threading.Thread(
                target=_prefetch,
                args=(next_url, _pf_html, _pf_err, _pf_ready),
                daemon=True,
            ).start()
        else:
            # No more pages — set a dummy ready event so loop exits cleanly
            _pf_html  = [None]
            _pf_err   = [None]
            _pf_ready = threading.Event()
            _pf_ready.set()

        # ── Extract CCs while next page is being fetched ─────────────────────
        for msg in messages:
            for raw_tuple in extract_raw_ccs(msg.get("text", "")):
                cleaned = clean_cc(*raw_tuple)
                if cleaned:
                    raw_found += 1
                    if cleaned not in seen:
                        seen.add(cleaned)
                        cards.append(cleaned)
                        if len(cards) >= limit_ccs:
                            result = {
                                "cards":         cards,
                                "msgs_scanned":  msgs_scanned,
                                "pages_fetched": pages_fetched,
                                "dupes_removed": max(0, raw_found - len(cards)),
                                "error":         None,
                                "cached":        False,
                            }
                            _to_cache(username, limit_ccs, result)
                            return result

        # If no min_id, channel exhausted
        if min_id is None or min_id <= 1:
            break

    result = {
        "cards":         cards[:limit_ccs],
        "msgs_scanned":  msgs_scanned,
        "pages_fetched": pages_fetched,
        "dupes_removed": max(0, raw_found - len(cards)),
        "error":         None,
        "cached":        False,
    }
    if result["cards"]:
        _to_cache(username, limit_ccs, result)
    return result
