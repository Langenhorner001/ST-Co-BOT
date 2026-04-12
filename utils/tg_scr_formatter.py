# -*- coding: utf-8 -*-
"""
utils/tg_scr_formatter.py
Response formatting for .scr CC scraper — matches spec + bot UI style.
"""

_SEP   = "──────────────────────────────"
_SIG   = "⌤ <a href='https://t.me/yadistan'>@yadistan</a>"
_LIMIT = 4096     # Telegram hard limit (raw HTML chars)
_SAFE  = 4000     # stay under limit with some buffer


def fmt_processing(username: str) -> str:
    return (
        f"<b>🃏 CC SCRAPER\n"
        f"{_SEP}\n"
        f"│  🔗  Source  ›  @{username}\n"
        f"│  ⏳  Status  ›  Fetching cards...\n"
        f"{_SEP}</b>"
    )


def fmt_success(username: str, cards: list, msgs_scanned: int,
                pages: int, elapsed: float, cached: bool = False) -> str:
    """
    Main success response.
    Dynamically fills as many cards as Telegram's 4096-char limit allows;
    rest go to the attached .txt file.
    """
    total     = len(cards)
    cache_tag = " <i>(cached)</i>" if cached else ""

    header = (
        f"<b>📡 SCRAPE SUCCESS{cache_tag}\n"
        f"{_SEP}\n"
        f"│  🔗  Source   ›  @{username}\n"
        f"│  💳  Cards    ›  {total}\n"
        f"│  📨  Scanned  ›  {msgs_scanned} msgs ({pages} pages)\n"
        f"│  ⏱️  Time     ›  {elapsed:.2f}s\n"
        f"{_SEP}\n"
        f"│  📄 Clean Results:\n"
    )
    footer = (
        f"{_SEP}\n"
        f"       {_SIG}</b>"
    )

    # Budget: total limit minus fixed parts and a placeholder for the "more" line
    more_placeholder = f"│  <i>… +{total} more — see attached file</i>\n"
    budget = _SAFE - len(header) - len(footer) - len(more_placeholder)

    preview_lines = ""
    shown = 0
    for cc in cards:
        line = f"│  <code>{cc}</code>\n"
        if len(preview_lines) + len(line) > budget:
            break
        preview_lines += line
        shown += 1

    more = ""
    if total > shown:
        more = f"│  <i>… +{total - shown} more — see attached file</i>\n"

    return (
        f"{header}"
        f"{preview_lines}"
        f"{more}"
        f"{footer}"
    )


def fmt_no_cards(username: str, msgs_scanned: int, pages: int,
                 elapsed: float) -> str:
    return (
        f"<b>⚠️ NO DATA FOUND\n"
        f"{_SEP}\n"
        f"│  🔗  Source   ›  @{username}\n"
        f"│  📨  Scanned  ›  {msgs_scanned} msgs ({pages} pages)\n"
        f"│  ⏱️  Time     ›  {elapsed:.2f}s\n"
        f"{_SEP}\n"
        f"│  Try a different source or increase limit.\n"
        f"{_SEP}\n"
        f"       {_SIG}</b>"
    )


def fmt_error(reason: str) -> str:
    return (
        f"<b>❌ SCRAPE FAILED\n"
        f"{_SEP}\n"
        f"│  ⚠️  Reason  ›  {reason}\n"
        f"│  💡  Tip     ›  Check link format\n"
        f"{_SEP}\n"
        f"       {_SIG}</b>"
    )


def fmt_usage() -> str:
    return (
        f"<b>🃏 CC Scraper — Usage\n"
        f"{_SEP}\n"
        f"│  <code>.scr &lt;link&gt; &lt;quantity&gt;</code>\n"
        f"│\n"
        f"│  <b>Examples:</b>\n"
        f"│  <code>.scr https://t.me/channel 50</code>\n"
        f"│  <code>.scr t.me/channel 100</code>\n"
        f"│\n"
        f"│  📌 Quantity: any number — no upper limit\n"
        f"│  📌 Paginates until quota met or channel exhausted\n"
        f"│  📌 Public channels only\n"
        f"│  📌 Format: <code>num|mm|yyyy|cvv</code>\n"
        f"│\n"
        f"│  🕷️  Proxy scraper → <code>.pscr</code>\n"
        f"{_SEP}\n"
        f"       {_SIG}</b>"
    )


def fmt_file_caption(username: str, total_cards: int, elapsed: float,
                     msgs_scanned: int = 0, dupes_removed: int = 0,
                     user_handle: str = "") -> str:
    """STREAD SCRAPPER BOT style caption for the .txt document."""
    sep = "━━━━━━━━━━━━━━━━━━━━━━━━"
    user_tag = f"@{user_handle}" if user_handle else "Unknown"
    return (
        f"🛒 <b>SCRAPPED</b>\n"
        f"{sep}\n"
        f"Source  : https://t.me/{username}\n"
        f"Scanned : {msgs_scanned}\n"
        f"Found   : {total_cards} CARDS\n"
        f"Removed : {dupes_removed} DUPE\n"
        f"{sep}\n"
        f"👤 User : {user_tag}"
    )
