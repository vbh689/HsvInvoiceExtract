from app.schemas import FindingCode, Severity
from app.settings import Settings
from app.validation.reconcile import reconcile
from tests.factories import make_line, make_raw

SETTINGS = Settings()


def test_missing_optional_field_is_soft_warning():
    raw = make_raw(
        line_items=[
            make_line(product_code=None, quantity="2", unit_price="100.000", line_total="200.000")
        ],
        printed_subtotal="200.000",
        printed_tax_rate="10%",
        printed_grand_total="220.000",
    )
    result = reconcile(raw, SETTINGS)

    codes = {f.code for f in result.all_findings}
    assert FindingCode.MISSING_OPTIONAL_FIELD in codes


def test_missing_required_field_is_error():
    raw = make_raw(
        line_items=[make_line(quantity=None, unit_price="100.000", line_total="200.000")],
        printed_subtotal="200.000",
    )
    result = reconcile(raw, SETTINGS)

    finding = next(
        f
        for f in result.all_findings
        if f.code == FindingCode.MISSING_REQUIRED_FIELD and f.field_path == "line_items[0].quantity"
    )
    assert finding.severity == Severity.ERROR
    # line-item findings carry the 1-based line_no for the flattened findings[] response
    assert finding.line_no == 1


def _five_lines(mismatched: int) -> tuple[list, int]:
    """Five lines, `mismatched` of which have a printed line_total that
    disagrees with quantity x unit_price. printed_subtotal is computed to
    match the sum of *printed* line totals exactly, so the document-level
    price-basis check stays clean and only the line-level mismatch(es) show
    up as findings — isolating the thing each test actually exercises.
    """
    lines = []
    subtotal = 0
    for i in range(5):
        if i < mismatched:
            lines.append(
                make_line(
                    product_code=f"SKU-{i}",
                    quantity="2",
                    unit_price="100.000",
                    line_total="999.000",
                )
            )
            subtotal += 999_000
        else:
            lines.append(
                make_line(
                    product_code=f"SKU-{i}",
                    quantity="2",
                    unit_price="100.000",
                    line_total="200.000",
                )
            )
            subtotal += 200_000
    return lines, subtotal


def test_single_line_arithmetic_mismatch_is_not_multi():
    lines, subtotal = _five_lines(mismatched=1)
    raw = make_raw(line_items=lines, printed_subtotal=f"{subtotal:,}".replace(",", "."))
    result = reconcile(raw, SETTINGS)

    codes = [f.code for f in result.all_findings]
    assert codes.count(FindingCode.LINE_ARITHMETIC_MISMATCH) == 1
    assert FindingCode.LINE_ARITHMETIC_MISMATCH_MULTI not in codes


def test_majority_line_mismatch_triggers_multi_finding():
    lines, subtotal = _five_lines(mismatched=3)
    raw = make_raw(line_items=lines, printed_subtotal=f"{subtotal:,}".replace(",", "."))
    result = reconcile(raw, SETTINGS)

    codes = {f.code for f in result.document_findings}
    assert FindingCode.LINE_ARITHMETIC_MISMATCH_MULTI in codes


def test_discount_line_reduces_total_correctly():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", discount="20.000", line_total="180.000")
        ],
        printed_subtotal="180.000",
    )
    result = reconcile(raw, SETTINGS)

    mismatch_codes = [
        f.code for f in result.all_findings if f.code == FindingCode.LINE_ARITHMETIC_MISMATCH
    ]
    assert mismatch_codes == []
    assert result.line_items[0].discount == 20_000


def test_document_level_discount_applied_before_tax():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000"),
            make_line(
                product_code="SKU-002", quantity="3", unit_price="50.000", line_total="150.000"
            ),
        ],
        printed_subtotal="350.000",
        printed_discount="50.000",
        printed_tax_rate="10%",
        printed_tax_amount="30.000",
        printed_grand_total="330.000",
    )
    result = reconcile(raw, SETTINGS)

    assert result.document.subtotal_calculated == 350_000
    assert result.document.tax_amount_calculated == 30_000
    assert result.document.grand_total_calculated == 330_000
    mismatch_codes = [
        f.code
        for f in result.document_findings
        if f.code == FindingCode.SUBTOTAL_TAX_GRANDTOTAL_MISMATCH
    ]
    assert mismatch_codes == []


def test_zero_line_items_is_flagged_empty():
    raw = make_raw(line_items=[])
    result = reconcile(raw, SETTINGS)

    codes = {f.code for f in result.all_findings}
    assert FindingCode.EXTRACTION_EMPTY in codes
    assert result.line_items == []
