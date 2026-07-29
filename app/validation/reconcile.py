"""The validation layer: every calculation the model was forbidden to do
happens here instead, because a calculation can be verified and a model
output cannot.

`reconcile()` takes a RawExtraction (transcribed strings) and produces
resolved document/line figures, a price-basis decision, and a list of
structured findings. Tax basis is inferred purely from arithmetic: if the
line totals sum to the printed subtotal, prices are pre-tax; if they sum to
the printed grand total, they're post-tax. The model's own opinion
(`stated_price_basis`) only ever produces a disagreement finding — it never
overrides the arithmetic result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.normalize.vn_number import parse_vn_number, parse_vn_percent
from app.schemas import (
    DocumentFields,
    Finding,
    FindingCode,
    LineItemResult,
    PriceBasisResolution,
    RawExtraction,
    RawLineItem,
    Severity,
)
from app.settings import Settings

ZERO = Decimal(0)


@dataclass
class ReconciliationResult:
    document: DocumentFields
    line_items: list[LineItemResult]
    price_basis: PriceBasisResolution
    document_findings: list[Finding] = field(default_factory=list)
    all_findings: list[Finding] = field(default_factory=list)


def _finding(
    code: FindingCode,
    severity: Severity,
    message: str,
    field_path: str | None = None,
    line_no: int | None = None,
) -> Finding:
    return Finding(
        code=code, severity=severity, message=message, field_path=field_path, line_no=line_no
    )


def _to_int(d: Decimal | None) -> int | None:
    return int(d) if d is not None else None


def _to_float(d: Decimal | None) -> float | None:
    return float(d) if d is not None else None


def _sum_or_none(values: list[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present, ZERO)


def _tolerance(settings: Settings, reference: Decimal) -> Decimal:
    return max(
        Decimal(settings.tolerance_absolute_floor_vnd),
        Decimal(str(settings.tolerance_relative)) * abs(reference),
    )


@dataclass
class _ParsedLine:
    line_no: int
    raw: RawLineItem
    quantity: Decimal | None
    unit_price: Decimal | None
    line_total_printed: Decimal | None
    discount: Decimal | None
    tax_rate: Decimal | None
    line_total_effective: Decimal | None  # printed if present, else quantity*price-discount
    findings: list[Finding]


def _parse_and_check_line(i: int, raw_line: RawLineItem, settings: Settings) -> _ParsedLine:
    # Public line numbers are stable array positions. The model's printed
    # line_no remains a raw string and is never trusted as an index.
    line_no = i + 1
    findings: list[Finding] = []

    quantity = parse_vn_number(raw_line.quantity, allow_decimal=True)
    unit_price = parse_vn_number(raw_line.unit_price, allow_decimal=False)
    line_total_printed = parse_vn_number(raw_line.line_total, allow_decimal=False)
    discount = parse_vn_number(raw_line.discount, allow_decimal=False)
    tax_rate = parse_vn_percent(raw_line.tax_rate)

    if not raw_line.product_name:
        findings.append(
            _finding(
                FindingCode.MISSING_REQUIRED_FIELD,
                Severity.ERROR,
                "product_name is missing",
                f"line_items[{i}].product_name",
                line_no,
            )
        )
    if quantity is None:
        findings.append(
            _finding(
                FindingCode.MISSING_REQUIRED_FIELD,
                Severity.ERROR,
                "quantity is missing or unparseable",
                f"line_items[{i}].quantity",
                line_no,
            )
        )
    if unit_price is None:
        findings.append(
            _finding(
                FindingCode.MISSING_REQUIRED_FIELD,
                Severity.ERROR,
                "unit_price is missing or unparseable",
                f"line_items[{i}].unit_price",
                line_no,
            )
        )
    if not raw_line.product_code:
        findings.append(
            _finding(
                FindingCode.MISSING_OPTIONAL_FIELD,
                Severity.WARNING,
                "product_code is missing",
                f"line_items[{i}].product_code",
                line_no,
            )
        )

    line_calc: Decimal | None = None
    if quantity is not None and unit_price is not None:
        line_calc = quantity * unit_price - (discount or ZERO)

    if line_total_printed is not None and line_calc is not None:
        tol = _tolerance(settings, line_total_printed)
        if abs(line_calc - line_total_printed) > tol:
            findings.append(
                _finding(
                    FindingCode.LINE_ARITHMETIC_MISMATCH,
                    Severity.ERROR,
                    f"quantity x unit_price - discount ({line_calc}) does not match "
                    f"printed line_total ({line_total_printed})",
                    f"line_items[{i}].line_total",
                    line_no,
                )
            )

    line_total_effective = line_total_printed if line_total_printed is not None else line_calc

    if tax_rate is not None and tax_rate == ZERO:
        findings.append(
            _finding(
                FindingCode.TAX_EXEMPT,
                Severity.INFO,
                "line is tax-exempt",
                f"line_items[{i}].tax_rate",
                line_no,
            )
        )

    return _ParsedLine(
        line_no=line_no,
        raw=raw_line,
        quantity=quantity,
        unit_price=unit_price,
        line_total_printed=line_total_printed,
        discount=discount,
        tax_rate=tax_rate,
        line_total_effective=line_total_effective,
        findings=findings,
    )


def _infer_tax_rate(
    tax_amount_printed: Decimal,
    subtotal_printed: Decimal | None,
    grand_total_printed: Decimal | None,
    sum_lines: Decimal | None,
) -> tuple[Decimal | None, Finding | None]:
    """Some invoices print a tax amount but never print a rate percentage
    anywhere on the document. VAT is always computed on the pre-tax base, so
    rate = tax_amount / pretax_base is a deterministic calculation (not a
    guess) as long as some pre-tax reference figure is available.
    """
    if grand_total_printed is not None:
        pretax_base = grand_total_printed - tax_amount_printed
    elif subtotal_printed is not None:
        pretax_base = subtotal_printed
    else:
        pretax_base = sum_lines

    if pretax_base is None or pretax_base <= ZERO:
        return None, None

    rate = tax_amount_printed / pretax_base
    if rate < ZERO or rate > 1:
        return None, None

    return rate, _finding(
        FindingCode.TAX_RATE_INFERRED,
        Severity.INFO,
        f"tax_rate not printed anywhere on the document; "
        f"inferred as tax_amount / pre-tax base = {rate}",
        "document.tax_rate",
    )


def _infer_price_basis(
    sum_lines: Decimal | None,
    subtotal_printed: Decimal | None,
    grand_total_printed: Decimal | None,
    tax_rate_doc: Decimal | None,
    model_stated: str | None,
    settings: Settings,
) -> tuple[str, str, list[Finding]]:
    findings: list[Finding] = []

    pretax_diff = (
        abs(sum_lines - subtotal_printed)
        if sum_lines is not None and subtotal_printed is not None
        else None
    )
    posttax_diff = (
        abs(sum_lines - grand_total_printed)
        if sum_lines is not None and grand_total_printed is not None
        else None
    )
    pretax_ok = pretax_diff is not None and pretax_diff <= _tolerance(settings, subtotal_printed)
    posttax_ok = posttax_diff is not None and posttax_diff <= _tolerance(
        settings, grand_total_printed
    )

    if pretax_ok and posttax_ok:
        tax_neutral = tax_rate_doc == ZERO or (
            subtotal_printed is not None
            and grand_total_printed is not None
            and abs(subtotal_printed - grand_total_printed)
            <= _tolerance(settings, grand_total_printed)
        )
        if model_stated in ("pre_tax", "post_tax"):
            basis, resolved_by = model_stated, "model_stated_fallback"
        elif tax_neutral:
            # Pre/post-tax amounts are identical, so expose both without
            # forcing the model to guess a semantically irrelevant basis.
            basis, resolved_by = "unknown", "tax_neutral"
        else:
            basis, resolved_by = "unknown", "insufficient_data"
        findings.append(
            _finding(
                FindingCode.PRICE_BASIS_AMBIGUOUS,
                Severity.INFO,
                "line totals reconcile against both subtotal and grand total "
                "(e.g. a tax-exempt document) — basis cannot be determined from arithmetic alone",
            )
        )
        return basis, resolved_by, findings

    if pretax_ok:
        return "pre_tax", "line_vs_subtotal", findings
    if posttax_ok:
        return "post_tax", "line_vs_grand_total", findings

    if (
        subtotal_printed is None
        and grand_total_printed is not None
        and tax_rate_doc is not None
        and sum_lines is not None
    ):
        inferred_subtotal = grand_total_printed / (1 + tax_rate_doc)
        if abs(sum_lines - inferred_subtotal) <= _tolerance(settings, inferred_subtotal):
            findings.append(
                _finding(
                    FindingCode.SUBTOTAL_INFERRED,
                    Severity.INFO,
                    "subtotal not printed; inferred from grand_total / (1 + tax_rate) "
                    "and confirmed against line sum",
                    "document.subtotal",
                )
            )
            return "pre_tax", "line_vs_subtotal", findings

    had_printed_totals = subtotal_printed is not None or grand_total_printed is not None
    if had_printed_totals and sum_lines is not None:
        findings.append(
            _finding(
                FindingCode.DOCUMENT_TOTALS_UNRECONCILABLE,
                Severity.ERROR,
                "line items do not reconcile against either the printed subtotal "
                "or the printed grand total",
                "document",
            )
        )
    return "unknown", "insufficient_data", findings


def reconcile(raw: RawExtraction, settings: Settings) -> ReconciliationResult:
    document_findings: list[Finding] = []

    subtotal_printed = parse_vn_number(raw.printed_subtotal, allow_decimal=False)
    tax_amount_printed = parse_vn_number(raw.printed_tax_amount, allow_decimal=False)
    tax_rate_doc = parse_vn_percent(raw.printed_tax_rate)
    grand_total_printed = parse_vn_number(raw.printed_grand_total, allow_decimal=False)
    discount_doc = parse_vn_number(raw.printed_discount, allow_decimal=False)

    if not raw.line_items:
        document_findings.append(
            _finding(FindingCode.EXTRACTION_EMPTY, Severity.ERROR, "no line items were extracted")
        )
        document = DocumentFields(
            supplier_name=raw.supplier_name,
            supplier_tax_code=raw.supplier_tax_code,
            invoice_number=raw.invoice_number,
            invoice_date=_parse_date(raw.invoice_date),
            subtotal_printed=_to_int(subtotal_printed),
            tax_amount_printed=_to_int(tax_amount_printed),
            tax_rate=_to_float(tax_rate_doc),
            grand_total_printed=_to_int(grand_total_printed),
            discount=_to_int(discount_doc),
        )
        price_basis = PriceBasisResolution(basis="unknown", resolved_by="insufficient_data")
        return ReconciliationResult(
            document=document,
            line_items=[],
            price_basis=price_basis,
            document_findings=document_findings,
            all_findings=list(document_findings),
        )

    parsed_lines = [_parse_and_check_line(i, li, settings) for i, li in enumerate(raw.line_items)]

    total_lines = len(parsed_lines)
    mismatched_lines = sum(
        1
        for pl in parsed_lines
        if any(f.code == FindingCode.LINE_ARITHMETIC_MISMATCH for f in pl.findings)
    )
    if total_lines and mismatched_lines / total_lines > settings.line_mismatch_ratio_trigger:
        document_findings.append(
            _finding(
                FindingCode.LINE_ARITHMETIC_MISMATCH_MULTI,
                Severity.ERROR,
                f"{mismatched_lines}/{total_lines} lines fail arithmetic reconciliation",
                "line_items",
            )
        )

    sum_lines = _sum_or_none([pl.line_total_effective for pl in parsed_lines])

    if tax_rate_doc is None and tax_amount_printed is not None:
        tax_rate_doc, rate_finding = _infer_tax_rate(
            tax_amount_printed, subtotal_printed, grand_total_printed, sum_lines
        )
        if rate_finding is not None:
            document_findings.append(rate_finding)

    basis, resolved_by, basis_findings = _infer_price_basis(
        sum_lines,
        subtotal_printed,
        grand_total_printed,
        tax_rate_doc,
        raw.stated_price_basis,
        settings,
    )
    document_findings.extend(basis_findings)

    agrees_with_model: bool | None = None
    if raw.stated_price_basis in ("pre_tax", "post_tax"):
        if basis in ("pre_tax", "post_tax"):
            agrees_with_model = raw.stated_price_basis == basis
            if not agrees_with_model:
                document_findings.append(
                    _finding(
                        FindingCode.PRICE_BASIS_MODEL_DISAGREEMENT,
                        Severity.WARNING,
                        f"model stated basis '{raw.stated_price_basis}' "
                        f"but arithmetic resolved '{basis}' — arithmetic wins",
                        "document.price_basis",
                    )
                )

    if basis == "unknown" and resolved_by != "tax_neutral":
        document_findings.append(
            _finding(
                FindingCode.PRICE_BASIS_UNRESOLVED,
                Severity.ERROR,
                "tax basis could not be determined; "
                "pre/post-tax figures withheld rather than guessed",
                "document.price_basis",
            )
        )

    line_items: list[LineItemResult] = []
    for pl in parsed_lines:
        rate = pl.tax_rate if pl.tax_rate is not None else tax_rate_doc
        unit_price_pre_tax: Decimal | None = None
        unit_price_post_tax: Decimal | None = None
        line_total_pre_tax: Decimal | None = None
        line_total_post_tax: Decimal | None = None

        if basis == "pre_tax":
            unit_price_pre_tax = pl.unit_price
            line_total_pre_tax = pl.line_total_effective
            if rate is not None:
                if pl.unit_price is not None:
                    unit_price_post_tax = (pl.unit_price * (1 + rate)).to_integral_value()
                if pl.line_total_effective is not None:
                    line_total_post_tax = (pl.line_total_effective * (1 + rate)).to_integral_value()
        elif basis == "post_tax":
            unit_price_post_tax = pl.unit_price
            line_total_post_tax = pl.line_total_effective
            if rate is not None:
                if pl.unit_price is not None:
                    unit_price_pre_tax = (pl.unit_price / (1 + rate)).to_integral_value()
                if pl.line_total_effective is not None:
                    line_total_pre_tax = (pl.line_total_effective / (1 + rate)).to_integral_value()
        elif resolved_by == "tax_neutral":
            unit_price_pre_tax = pl.unit_price
            unit_price_post_tax = pl.unit_price
            line_total_pre_tax = pl.line_total_effective
            line_total_post_tax = pl.line_total_effective

        line_items.append(
            LineItemResult(
                line_no=pl.line_no,
                product_code=pl.raw.product_code,
                product_name=pl.raw.product_name,
                unit=pl.raw.unit,
                quantity=_to_float(pl.quantity),
                unit_price_as_printed=_to_int(pl.unit_price),
                unit_price_pre_tax=_to_int(unit_price_pre_tax),
                unit_price_post_tax=_to_int(unit_price_post_tax),
                line_total_pre_tax=_to_int(line_total_pre_tax),
                line_total_post_tax=_to_int(line_total_post_tax),
                discount=_to_int(pl.discount),
                tax_rate=_to_float(rate),
            )
        )

    resolved_doc_rate = tax_rate_doc
    if resolved_doc_rate is None:
        resolved_doc_rate = next(
            (pl.tax_rate for pl in parsed_lines if pl.tax_rate is not None), None
        )

    subtotal_calculated = _sum_or_none([_dec_or_none(li.line_total_pre_tax) for li in line_items])
    tax_amount_calculated: Decimal | None = None
    grand_total_calculated: Decimal | None = None
    if subtotal_calculated is not None and resolved_doc_rate is not None:
        base = subtotal_calculated - (discount_doc or ZERO)
        tax_amount_calculated = (base * resolved_doc_rate).to_integral_value()
        grand_total_calculated = base + tax_amount_calculated
    elif subtotal_calculated is not None:
        grand_total_calculated = _sum_or_none(
            [_dec_or_none(li.line_total_post_tax) for li in line_items]
        )

    document_findings.extend(
        _compare_totals(subtotal_printed, subtotal_calculated, "document.subtotal", settings)
    )
    document_findings.extend(
        _compare_totals(tax_amount_printed, tax_amount_calculated, "document.tax_amount", settings)
    )
    document_findings.extend(
        _compare_totals(
            grand_total_printed, grand_total_calculated, "document.grand_total", settings
        )
    )

    document = DocumentFields(
        supplier_name=raw.supplier_name,
        supplier_tax_code=raw.supplier_tax_code,
        invoice_number=raw.invoice_number,
        invoice_date=_parse_date(raw.invoice_date),
        subtotal_printed=_to_int(subtotal_printed),
        subtotal_calculated=_to_int(subtotal_calculated),
        tax_amount_printed=_to_int(tax_amount_printed),
        tax_amount_calculated=_to_int(tax_amount_calculated),
        tax_rate=_to_float(resolved_doc_rate),
        grand_total_printed=_to_int(grand_total_printed),
        grand_total_calculated=_to_int(grand_total_calculated),
        discount=_to_int(discount_doc),
    )
    price_basis = PriceBasisResolution(
        basis=basis, resolved_by=resolved_by, agrees_with_model=agrees_with_model
    )

    all_findings = list(document_findings)
    for pl in parsed_lines:
        all_findings.extend(pl.findings)

    return ReconciliationResult(
        document=document,
        line_items=line_items,
        price_basis=price_basis,
        document_findings=document_findings,
        all_findings=all_findings,
    )


def _dec_or_none(value: int | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _compare_totals(
    printed: Decimal | None, calculated: Decimal | None, field_path: str, settings: Settings
) -> list[Finding]:
    if printed is None or calculated is None:
        return []
    tol = _tolerance(settings, printed)
    diff = abs(printed - calculated)
    if diff <= tol:
        return []
    severity = Severity.WARNING if diff <= 2 * tol else Severity.ERROR
    return [
        _finding(
            FindingCode.SUBTOTAL_TAX_GRANDTOTAL_MISMATCH,
            severity,
            f"printed value ({printed}) does not match value recalculated "
            f"from line items ({calculated})",
            field_path,
        )
    ]


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None
