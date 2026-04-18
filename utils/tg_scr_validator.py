# -*- coding: utf-8 -*-
"""
utils/tg_scr_validator.py
Input validation for .scr Telegram channel scraper command.
"""

import re

_TG_PATTERNS = [
    re.compile(r'(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([a-zA-Z0-9_]{4,32})/?$'),
    re.compile(r'(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([a-zA-Z0-9_]{4,32})\b'),
]

_RESERVED = {'joinchat', 's', 'share', 'addstickers', 'addstickerr', 'proxy',
             'socks', 'resolve', 'login', 'iv', 'msg'}

MAX_QUANTITY = 999999   # no upper cap — scrape as many as channel has
MIN_QUANTITY = 1


def extract_username(link: str):
    """Return channel username from a t.me link, or None if invalid."""
    link = link.strip()
    for pat in _TG_PATTERNS:
        m = pat.match(link)
        if m:
            u = m.group(1).lower()
            if u not in _RESERVED:
                return m.group(1)
    return None


def is_tg_link(text: str) -> bool:
    """Return True if text looks like a Telegram channel link."""
    t = text.strip().lower()
    return 't.me/' in t or 'telegram.me/' in t


def validate_link(link: str):
    """
    Returns (username, error_str).
    username is None if invalid; error_str is None if valid.
    """
    username = extract_username(link)
    if not username:
        return None, "Invalid Telegram link. Use: https://t.me/channelname"
    return username, None


def validate_quantity(q_str: str):
    """
    Returns (quantity_int, error_str).
    """
    try:
        q = int(q_str)
    except ValueError:
        return None, "Quantity must be a number (e.g. 100, 500, 1000)"
    if q < MIN_QUANTITY:
        return None, f"Minimum quantity is {MIN_QUANTITY}"
    return q, None
