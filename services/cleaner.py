# -*- coding: utf-8 -*-
"""
services/cleaner.py
Data cleaning and normalization for scraped CC entries.
"""

import re

_DIGITS_ONLY = re.compile(r'^\d+$')


def clean_cc(num: str, month: str, year: str, cvv: str) -> str | None:
    """
    Normalize and validate a raw (num, month, year, cvv) tuple.
    Returns clean 'num|mm|yyyy|cvv' string or None if invalid.
    """
    num = num.strip()
    month = month.strip().zfill(2)
    year = year.strip()
    cvv = cvv.strip()

    # Basic digit checks
    if not all(_DIGITS_ONLY.match(x) for x in [num, month, year, cvv]):
        return None

    # Card number length
    if not (13 <= len(num) <= 19):
        return None

    # Month range
    mo_int = int(month)
    if not (1 <= mo_int <= 12):
        return None

    # Normalize year to 4 digits
    if len(year) == 2:
        year = "20" + year
    if len(year) != 4:
        return None

    # CVV length
    if not (3 <= len(cvv) <= 4):
        return None

    return f"{num}|{month}|{year}|{cvv}"


def deduplicate(cards: list) -> list:
    """Remove duplicate cards while preserving insertion order."""
    seen = set()
    result = []
    for card in cards:
        if card not in seen:
            seen.add(card)
            result.append(card)
    return result
