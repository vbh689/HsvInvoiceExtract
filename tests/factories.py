"""Test-only builders for RawExtraction payloads. Not collected by pytest
(no test_ prefix) — imported by the validation-layer tests.
"""

from __future__ import annotations

from typing import Literal

from app.schemas import RawExtraction, RawLineItem


def make_line(
    *,
    product_code: str | None = "SKU-001",
    product_name: str | None = "Widget",
    unit: str | None = "cái",
    quantity: str | None = "2",
    unit_price: str | None = "100.000",
    line_total: str | None = "200.000",
    discount: str | None = None,
    tax_rate: str | None = None,
    line_no: str | None = None,
) -> RawLineItem:
    return RawLineItem(
        line_no=line_no,
        product_code=product_code,
        product_name=product_name,
        unit=unit,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        discount=discount,
        tax_rate=tax_rate,
    )


def make_raw(
    *,
    line_items: list[RawLineItem] | None = None,
    supplier_name: str | None = "ACME Co.",
    printed_subtotal: str | None = None,
    printed_tax_amount: str | None = None,
    printed_tax_rate: str | None = None,
    printed_grand_total: str | None = None,
    printed_discount: str | None = None,
    stated_price_basis: Literal["pre_tax", "post_tax", "unknown"] = "unknown",
) -> RawExtraction:
    return RawExtraction(
        supplier_name=supplier_name,
        supplier_tax_code=None,
        invoice_number=None,
        invoice_date=None,
        currency=None,
        printed_subtotal=printed_subtotal,
        printed_tax_amount=printed_tax_amount,
        printed_tax_rate=printed_tax_rate,
        printed_grand_total=printed_grand_total,
        printed_discount=printed_discount,
        stated_price_basis=stated_price_basis,
        line_items=line_items if line_items is not None else [make_line()],
        model_notes=None,
    )
