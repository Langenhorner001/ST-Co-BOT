# -*- coding: utf-8 -*-
"""
services/stripe_link_converter.py
Enhanced Stripe link analyzer for .strp command.
Inspired by OP Extension multi-layer detection approach.

Detection layers (same as browser extension):
  1. URL hash XOR decode  → pk_live (instant, no HTTP)
  2. Page HTML scan       → pk_live, client_secret, setup_secret, pm_ids
  3. <script> tag scan    → JSON blobs, window variables
  4. Meta / data attrs    → hidden values
  5. JSON blob deep scan  → nested apiKey, paymentIntent.client_secret

Supports:
  • checkout.stripe.com/c/pay/cs_live_XXXX#HASH  — checkout session
  • checkout.stripe.com/c/pay/ppage_XXXX          — payment page
  • buy.stripe.com/XXXX                           — payment link
  • checkout.stripe.com/pay/XXXX                  — clean session URL
"""

import re
import base64
import json
import time
import requests
import urllib.parse

# ── HTTP ───────────────────────────────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 10

# ── Core Stripe ID patterns (from extension) ───────────────────────────────────
_RE_PK_BARE      = re.compile(r'pk_(?:live|test)_[A-Za-z0-9]{24,}')
_RE_PK_KEYED     = re.compile(
    r'(?:apiKey|publishableKey|public_key|stripeKey|stripe_key|stripe-key)'
    r'["\s\']*[:=]["\s\']*(pk_(?:live|test)_[A-Za-z0-9]+)'
)
_RE_CLIENT_SECRET = re.compile(
    r'pi_[A-Za-z0-9\-_]{10,}_secret_[A-Za-z0-9\-_]{10,}'
)
_RE_SETUP_SECRET  = re.compile(
    r'seti_[A-Za-z0-9\-_]{10,}_secret_[A-Za-z0-9\-_]{10,}'
)
_RE_PM_ID         = re.compile(r'pm_[A-Za-z0-9]{24,}')
_RE_CS            = re.compile(r'cs_(?:live|test)_[A-Za-z0-9]+')
_RE_PPAGE         = re.compile(r'ppage_[A-Za-z0-9_]+')

# ── Amount / merchant patterns ─────────────────────────────────────────────────
_RE_AMOUNT_INT  = re.compile(r'"(?:total|amount)"\s*:\s*(\d+)')
_RE_AMOUNT_FMT  = re.compile(r'"(?:displayAmount|formattedAmount)"\s*:\s*"([^"]+)"')
_RE_CURRENCY    = re.compile(r'"currency"\s*:\s*"([a-z]{3})"')
_RE_MERCHANT    = [
    re.compile(r'"merchantName"\s*:\s*"([^"]+)"'),
    re.compile(r'"merchant_name"\s*:\s*"([^"]+)"'),
    re.compile(r'<title>Pay\s+([^<|]+)'),
    re.compile(r'"company"\s*:\s*"([^"]+)"'),
    re.compile(r'"businessName"\s*:\s*"([^"]+)"'),
]

# ── Script tag extractor ───────────────────────────────────────────────────────
_RE_SCRIPT_TAG  = re.compile(
    r'<script(?:\s[^>]*)?>([^<]{20,})</script>', re.S | re.I
)
_RE_META_CONTENT = re.compile(
    r'<meta\s[^>]*content=["\']([^"\']*(?:pk_live|pi_|pm_)[^"\']*)["\']',
    re.I
)
_RE_DATA_ATTR    = re.compile(
    r'data-(?:key|stripe|pk|secret|client)["\s]*=["\s]*([^\s"\'<>]{10,})',
    re.I
)
_RE_INPUT_HIDDEN = re.compile(
    r'<input[^>]+type=["\']hidden["\'][^>]*value=["\']([^"\']{10,})["\']',
    re.I
)
_RE_JSON_BLOB    = re.compile(r'\{[^{}]{30,}\}', re.S)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — URL validation & type detection
# ─────────────────────────────────────────────────────────────────────────────

def _is_stripe_url(url: str) -> bool:
    return bool(re.search(r'https?://(?:[a-z0-9\-]+\.)*stripe\.com', url, re.I))


def _detect_type(url: str) -> str:
    if re.search(r'buy\.stripe\.com', url, re.I):
        return "payment_link"
    if _RE_CS.search(url):
        return "checkout_session"
    if _RE_PPAGE.search(url):
        return "payment_page"
    if re.search(r'checkout\.stripe\.com', url, re.I):
        return "checkout_unknown"
    return "unknown"


def _extract_session_id(url: str) -> str | None:
    m = _RE_PPAGE.search(url)
    if m:
        return m.group(0)
    m = _RE_CS.search(url)
    if m:
        return m.group(0)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — URL hash XOR decode (instant, no HTTP)
# Same approach Stripe uses to embed pk_live in checkout URL
# ─────────────────────────────────────────────────────────────────────────────

def _decode_hash_for_pk(url: str) -> str | None:
    if '#' not in url:
        return None
    raw_hash = url.split('#', 1)[1]
    if not raw_hash:
        return None
    try:
        decoded = urllib.parse.unquote(raw_hash)
        padded  = decoded + '=' * ((4 - len(decoded) % 4) % 4)
        for b64fn in (
            lambda s: base64.b64decode(s, validate=False),
            lambda s: base64.urlsafe_b64decode(s),
        ):
            try:
                raw_bytes = b64fn(padded)
            except Exception:
                continue
            for xor_key in range(1, 16):
                try:
                    xored = bytes([b ^ xor_key for b in raw_bytes])
                    s = xored.decode('utf-8')
                    if '"apiKey"' in s or '"publishableKey"' in s:
                        data = json.loads(s)
                        pk = data.get('apiKey') or data.get('publishableKey')
                        if pk and re.match(r'pk_(?:live|test)_', pk):
                            return pk
                except Exception:
                    continue
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — HTML deep scan (extension-style multi-source)
# Scans: full HTML, script tags, meta tags, data attributes, hidden inputs,
#        JSON blobs embedded in page JS
# ─────────────────────────────────────────────────────────────────────────────

class _ScanResult:
    """Holds all values found across all scan layers."""
    def __init__(self):
        self.pk_live       : str | None = None
        self.client_secret : str | None = None  # pi_XXX_secret_XXX
        self.setup_secret  : str | None = None  # seti_XXX_secret_XXX
        self.pm_ids        : list[str]  = []
        self.merchant      : str | None = None
        self.amount        : str | None = None
        self.amount_type   : str        = 'cents'
        self.currency      : str | None = None

    def absorb(self, text: str, label: str = ''):
        """Extract all Stripe artifacts from a text chunk."""
        if not text or len(text) < 5:
            return
        # Limit to 200KB per chunk (like extension's 100KB)
        if len(text) > 200_000:
            text = text[:200_000]

        # pk_live — keyed format first (more reliable), bare fallback
        if not self.pk_live:
            m = _RE_PK_KEYED.search(text)
            if m:
                self.pk_live = m.group(1)
        if not self.pk_live:
            m = _RE_PK_BARE.search(text)
            if m and 'pk_live_' in m.group(0):
                self.pk_live = m.group(0)

        # client_secret — PaymentIntent (most valuable!)
        if not self.client_secret:
            m = _RE_CLIENT_SECRET.search(text)
            if m:
                self.client_secret = m.group(0)

        # setup_secret — SetupIntent
        if not self.setup_secret:
            m = _RE_SETUP_SECRET.search(text)
            if m:
                self.setup_secret = m.group(0)

        # pm_ IDs
        for m in _RE_PM_ID.finditer(text):
            pid = m.group(0)
            if pid not in self.pm_ids:
                self.pm_ids.append(pid)

        # Merchant
        if not self.merchant:
            for pat in _RE_MERCHANT:
                m2 = pat.search(text)
                if m2:
                    self.merchant = m2.group(1).strip()
                    break

        # Amount
        if not self.amount:
            m3 = _RE_AMOUNT_INT.search(text)
            if m3:
                self.amount      = m3.group(1)
                self.amount_type = 'cents'
            else:
                m3b = _RE_AMOUNT_FMT.search(text)
                if m3b:
                    self.amount      = m3b.group(1).strip()
                    self.amount_type = 'formatted'

        # Currency
        if not self.currency:
            m4 = _RE_CURRENCY.search(text)
            if m4:
                self.currency = m4.group(1).upper()


def _deep_scan_html(html: str) -> _ScanResult:
    """
    Extension-style deep scan across all layers:
      - Full HTML pass
      - <script> tag bodies (separate pass)
      - JSON blobs inside scripts
      - Meta content attributes
      - data-* attributes
      - Hidden inputs
    """
    sr = _ScanResult()

    # ── Layer A: full HTML pass ───────────────────────────────────────────────
    sr.absorb(html, 'full_html')

    # ── Layer B: <script> tag bodies (like extension's scanScripts) ───────────
    for script_match in _RE_SCRIPT_TAG.finditer(html):
        body = script_match.group(1)
        sr.absorb(body, 'script_tag')

        # ── Layer C: JSON blobs inside scripts (like extension's deepScan) ───
        for blob_match in _RE_JSON_BLOB.finditer(body):
            blob = blob_match.group(0)
            if any(k in blob for k in ('pi_', 'secret', 'pk_', 'apiKey',
                                        'publishableKey', 'pm_', 'seti_')):
                sr.absorb(blob, 'json_blob')
                # Try to parse as JSON for deeper key lookup
                try:
                    data = json.loads(blob)
                    _scan_json_obj(data, sr)
                except Exception:
                    pass

    # ── Layer D: meta content attributes ─────────────────────────────────────
    for m in _RE_META_CONTENT.finditer(html):
        sr.absorb(m.group(1), 'meta_content')

    # ── Layer E: data-* attributes ────────────────────────────────────────────
    for m in _RE_DATA_ATTR.finditer(html):
        sr.absorb(m.group(1), 'data_attr')

    # ── Layer F: hidden inputs (like extension's hidden input scan) ───────────
    for m in _RE_INPUT_HIDDEN.finditer(html):
        sr.absorb(m.group(1), 'hidden_input')

    return sr


def _scan_json_obj(obj, sr: _ScanResult, depth: int = 0):
    """Recursively scan JSON object for Stripe keys (like extension deepScan)."""
    if depth > 5:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_low = str(k).lower()
            if isinstance(v, str):
                if k_low in ('apikey', 'publishablekey', 'stripekey', 'public_key'):
                    if re.match(r'pk_(?:live|test)_', v) and not sr.pk_live:
                        sr.pk_live = v
                elif k_low in ('client_secret', 'clientsecret'):
                    if _RE_CLIENT_SECRET.match(v) and not sr.client_secret:
                        sr.client_secret = v
                    elif _RE_SETUP_SECRET.match(v) and not sr.setup_secret:
                        sr.setup_secret = v
                else:
                    sr.absorb(v, f'json_key:{k}')
            elif isinstance(v, (dict, list)):
                _scan_json_obj(v, sr, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _scan_json_obj(item, sr, depth + 1)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — HTTP fetch + full deep scan
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_and_scan(url: str) -> _ScanResult:
    """Fetch checkout page and run full deep scan."""
    sr = _ScanResult()
    try:
        resp = requests.get(
            url,
            headers={
                'User-Agent': _UA,
                'Accept-Language': 'en-US,en;q=0.9',
            },
            timeout=_TIMEOUT,
            allow_redirects=True,
        )
        html = resp.text
        sr   = _deep_scan_html(html)
    except Exception:
        pass
    return sr


# ─────────────────────────────────────────────────────────────────────────────
# Reverse: build working checkout URL from session_id + pk_live
# ─────────────────────────────────────────────────────────────────────────────

def build_url_from_parts(session_id: str, pk_live: str, xor_key: int = 5) -> str:
    """
    Reconstruct a working checkout.stripe.com URL from session_id + pk_live.

    How it works (reverse of _decode_hash_for_pk):
      1. Build minimal JSON payload: {"apiKey": pk_live}
      2. XOR every byte with xor_key (bot's decode loop tries 1-15, so any key works)
      3. base64 encode → strip padding
      4. URL-encode → append as #hash to checkout URL

    The result is accepted by:
      • Browser  — Stripe.js reads hash, inits checkout
      • .co cmd  — ext_stripe_check parses session_id + pk_live from hash
    """
    payload   = json.dumps({"apiKey": pk_live}, separators=(',', ':'))
    xored     = bytes([b ^ xor_key for b in payload.encode('utf-8')])
    b64       = base64.b64encode(xored).decode('utf-8').rstrip('=')
    hash_enc  = urllib.parse.quote(b64, safe='')
    return f"https://checkout.stripe.com/c/pay/{session_id}#{hash_enc}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_amount(amount: str, amount_type: str, currency: str) -> str:
    if not amount:
        return ''
    if amount_type == 'cents':
        try:
            return f"{int(amount) / 100:.2f} {currency}".strip()
        except Exception:
            pass
    return f"{amount} {currency}".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────────────

def convert_stripe_link(raw_input: str) -> dict:
    """
    Multi-layer Stripe link analyzer.

    Returns dict with:
      ok            : bool
      link_type     : str
      session_id    : str | None
      clean_url     : str
      pk_live       : str | None     — publishable key
      client_secret : str | None     — PaymentIntent client_secret (pi_XXX_secret_XXX)
      setup_secret  : str | None     — SetupIntent secret (seti_XXX_secret_XXX)
      pm_ids        : list[str]      — payment method IDs (pm_XXX)
      merchant      : str | None
      amount_fmt    : str
      suggested_cmd : str
      already_clean : bool
      elapsed       : float
      error         : str | None
    """
    t0 = time.time()

    url = raw_input.strip()
    m_url = re.search(r'https?://\S+', url)
    if m_url:
        url = m_url.group(0)

    # ── Validate ───────────────────────────────────────────────────────────────
    if not _is_stripe_url(url):
        return {
            'ok': False,
            'error': 'Not a valid Stripe URL (checkout.stripe.com or buy.stripe.com).',
            'link_type': 'unknown',
        }

    link_type = _detect_type(url)
    if link_type == 'unknown':
        return {'ok': False, 'error': 'Unsupported Stripe URL format.', 'link_type': link_type}

    # ── buy.stripe.com — payment link ─────────────────────────────────────────
    if link_type == 'payment_link':
        return {
            'ok'           : True,
            'link_type'    : 'Payment Link',
            'session_id'   : None,
            'clean_url'    : url,
            'pk_live'      : None,
            'client_secret': None,
            'setup_secret' : None,
            'pm_ids'       : [],
            'merchant'     : None,
            'amount_fmt'   : '',
            'suggested_cmd': '.co',
            'already_clean': True,
            'elapsed'      : round(time.time() - t0, 2),
            'error'        : None,
        }

    # ── Checkout Session / Payment Page ───────────────────────────────────────
    session_id = _extract_session_id(url)
    if not session_id:
        return {
            'ok'      : False,
            'error'   : 'Could not extract session/page ID from URL.',
            'link_type': link_type,
        }

    is_cs = session_id.startswith(('cs_live_', 'cs_test_'))

    # ── Layer 1: XOR hash decode (instant) ────────────────────────────────────
    pk_from_hash = _decode_hash_for_pk(url)

    # ── Layer 2-5: HTTP fetch + deep HTML scan ─────────────────────────────────
    sr = _fetch_and_scan(url)

    # Merge: hash decode wins for pk_live (more reliable than HTML scrape)
    if pk_from_hash:
        sr.pk_live = pk_from_hash

    amount_fmt = _format_amount(sr.amount, sr.amount_type, sr.currency or '')

    # Build reconstructed URL from session_id + pk_live (for when original URL
    # is unavailable or hash has been stripped — allows .co to work correctly)
    rebuilt_url = None
    if is_cs and sr.pk_live:
        rebuilt_url = build_url_from_parts(session_id, sr.pk_live)

    return {
        'ok'           : True,
        'link_type'    : 'Checkout Session' if is_cs else 'Payment Page',
        'session_id'   : session_id,
        'clean_url'    : url,
        'rebuilt_url'  : rebuilt_url,   # reconstructed URL usable with .co
        'pk_live'      : sr.pk_live,
        'client_secret': sr.client_secret,
        'setup_secret' : sr.setup_secret,
        'pm_ids'       : sr.pm_ids[:3],
        'merchant'     : sr.merchant,
        'amount_fmt'   : amount_fmt,
        # cs_live_ → .co (ext_stripe_check fallback handles sessions)
        # ppage_   → .sco (stripe-hitter supports payment pages natively)
        'suggested_cmd': '.co' if is_cs else '.sco',
        'already_clean': False,
        'elapsed'      : round(time.time() - t0, 2),
        'error'        : None,
    }
