# -*- coding: utf-8 -*-
"""
utils/parser.py
Regex-based extraction utilities. Reusable across services.
"""

import re

# Matches card patterns with |, /, or space as separator
_CC_PATTERN = re.compile(
    r'\b(\d{13,19})[\|/\s](\d{1,2})[\|/\s](\d{2,4})[\|/\s](\d{3,4})\b'
)

# Telegram message ID from href like /channel/12345
_MSG_ID_PATTERN = re.compile(r'/[a-zA-Z0-9_]+/(\d+)$')


def extract_raw_ccs(text: str) -> list:
    """
    Extract all raw (num, month, year, cvv) tuples from a text string.
    Returns list of tuples.
    """
    results = []
    for m in _CC_PATTERN.finditer(text):
        results.append(m.groups())
    return results


def parse_message_id(href: str) -> str:
    """Extract numeric message ID from a t.me href attribute."""
    m = _MSG_ID_PATTERN.search(href or "")
    return m.group(1) if m else ""
