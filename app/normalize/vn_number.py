"""Deterministic parsing of Vietnamese-formatted numbers.

Convention for this domain (stated, not inferred): "." is the thousands
grouping separator and "," is the decimal separator. VND amounts never carry
a fractional part; quantities occasionally do (e.g. "12,5" kg).

This is the only place that turns a model-transcribed string into a number —
the model itself never parses or computes (see app.schemas.extraction).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CURRENCY_SUFFIX = re.compile(r"(?i)(đ|vnđ|vnd)\s*$")
_WHITESPACE = re.compile(r"\s+")
_VALID_NUMBER = re.compile(r"^-?[0-9.,]+$")

_EXEMPT_MARKERS = {
    "kct",
    "k.c.t",
    "khong chiu thue",
    "không chịu thuế",
    "non-vat",
    "n/a",
    "exempt",
}


def _strip_currency(raw: str) -> str:
    s = raw.strip()
    s = _CURRENCY_SUFFIX.sub("", s).strip()
    s = _WHITESPACE.sub("", s)
    return s


def parse_vn_number(raw: str | None, *, allow_decimal: bool = False) -> Decimal | None:
    """Parse a raw VN-formatted number string.

    `allow_decimal=False` for money (VND has no subunits): any decimal
    fraction present is dropped rather than rounded, since a fractional VND
    amount indicates a misread, not a real value. `allow_decimal=True` for
    quantities.
    """
    if raw is None:
        return None
    s = _strip_currency(raw)
    if s in ("", "-", "--"):
        return None

    negative = s.startswith("-")
    if negative:
        s = s[1:]
    if not s or not _VALID_NUMBER.match(s):
        return None

    if "," in s:
        int_part, _, frac_part = s.rpartition(",")
        int_part = int_part.replace(".", "")
        if not int_part or not frac_part.isdigit():
            return None
        try:
            value = (
                Decimal(f"{int_part}.{frac_part}") if allow_decimal else Decimal(int_part or "0")
            )
        except InvalidOperation:
            return None
    else:
        int_part = s.replace(".", "")
        if not int_part.isdigit():
            return None
        try:
            value = Decimal(int_part)
        except InvalidOperation:
            return None

    if not allow_decimal:
        value = value.to_integral_value()
    if negative:
        value = -value
    return value


def parse_vn_percent(raw: str | None) -> Decimal | None:
    """Parse a printed tax/discount rate, e.g. "10%" -> 0.10, "KCT" -> 0."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.strip(".").lower() in _EXEMPT_MARKERS:
        return Decimal(0)
    s = s.replace("%", "").strip()
    if not s:
        return None
    value = parse_vn_number(s, allow_decimal=True)
    if value is None:
        return None
    return value / Decimal(100)


def is_exempt_marker(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in _EXEMPT_MARKERS
