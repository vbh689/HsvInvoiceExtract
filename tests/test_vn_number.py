from decimal import Decimal

import pytest

from app.normalize.vn_number import is_exempt_marker, parse_vn_number, parse_vn_percent


@pytest.mark.parametrize(
    "raw, allow_decimal, expected",
    [
        ("150.000", False, Decimal(150000)),
        ("1.234.567", False, Decimal(1234567)),
        ("12,5", True, Decimal("12.5")),
        ("1.234.567,89", True, Decimal("1234567.89")),
        ("1.234.567,89", False, Decimal(1234567)),  # money: fractional part dropped
        ("0", False, Decimal(0)),
        ("", False, None),
        (" ", False, None),
        ("-", False, None),
        (None, False, None),
        ("150.000 đ", False, Decimal(150000)),
        ("150.000đ", False, Decimal(150000)),
        ("150000", False, Decimal(150000)),
        ("-50.000", False, Decimal(-50000)),
        ("abc", False, None),
    ],
)
def test_parse_vn_number(raw, allow_decimal, expected):
    assert parse_vn_number(raw, allow_decimal=allow_decimal) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10%", Decimal("0.10")),
        ("8%", Decimal("0.08")),
        ("0%", Decimal("0")),
        ("KCT", Decimal(0)),
        ("kct", Decimal(0)),
        ("", None),
        (None, None),
    ],
)
def test_parse_vn_percent(raw, expected):
    assert parse_vn_percent(raw) == expected


def test_is_exempt_marker():
    assert is_exempt_marker("KCT")
    assert is_exempt_marker("kct")
    assert not is_exempt_marker("10%")
    assert not is_exempt_marker(None)
