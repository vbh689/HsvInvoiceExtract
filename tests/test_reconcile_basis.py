"""Tax-basis inference: the core of the whole problem. Arithmetic decides;
the model's stated_price_basis is advisory and only ever produces a
disagreement finding.
"""

from app.schemas import FindingCode, RawExtraction, RawLineItem, Severity
from app.settings import Settings
from app.validation.reconcile import reconcile
from tests.factories import make_line, make_raw

SETTINGS = Settings()


def test_pretax_invoice_reconciles():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000"),
            make_line(
                product_code="SKU-002", quantity="3", unit_price="50.000", line_total="150.000"
            ),
        ],
        printed_subtotal="350.000",
        printed_tax_rate="10%",
        printed_tax_amount="35.000",
        printed_grand_total="385.000",
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "pre_tax"
    assert result.price_basis.resolved_by == "line_vs_subtotal"
    assert result.all_findings == []
    assert result.document.subtotal_calculated == 350_000
    assert result.document.tax_amount_calculated == 35_000
    assert result.document.grand_total_calculated == 385_000

    line1, line2 = result.line_items
    assert line1.unit_price_pre_tax == 100_000
    assert line1.unit_price_post_tax == 110_000
    assert line2.unit_price_pre_tax == 50_000
    assert line2.unit_price_post_tax == 55_000


def test_posttax_invoice_reconciles():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="110.000", line_total="220.000"),
            make_line(
                product_code="SKU-002", quantity="3", unit_price="55.000", line_total="165.000"
            ),
        ],
        printed_subtotal="350.000",
        printed_tax_rate="10%",
        printed_tax_amount="35.000",
        printed_grand_total="385.000",
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "post_tax"
    assert result.price_basis.resolved_by == "line_vs_grand_total"
    assert result.all_findings == []

    line1, line2 = result.line_items
    assert line1.unit_price_post_tax == 110_000
    assert line1.unit_price_pre_tax == 100_000
    assert line2.unit_price_post_tax == 55_000
    assert line2.unit_price_pre_tax == 50_000


def test_missing_subtotal_falls_back_to_grand_total_and_rate():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000"),
            make_line(
                product_code="SKU-002", quantity="3", unit_price="50.000", line_total="150.000"
            ),
        ],
        printed_subtotal=None,
        printed_tax_rate="10%",
        printed_grand_total="385.000",
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "pre_tax"
    codes = {f.code for f in result.document_findings}
    assert FindingCode.SUBTOTAL_INFERRED in codes
    assert all(
        f.severity == Severity.INFO
        for f in result.document_findings
        if f.code == FindingCode.SUBTOTAL_INFERRED
    )


def test_broken_totals_unreconcilable():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000"),
            make_line(
                product_code="SKU-002", quantity="3", unit_price="50.000", line_total="150.000"
            ),
        ],
        printed_subtotal="999.000",
        printed_grand_total="1.500.000",
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "unknown"
    codes = {f.code for f in result.document_findings}
    assert FindingCode.DOCUMENT_TOTALS_UNRECONCILABLE in codes
    assert FindingCode.PRICE_BASIS_UNRESOLVED in codes
    for li in result.line_items:
        assert li.unit_price_pre_tax is None
        assert li.unit_price_post_tax is None
        # the raw printed number is still surfaced even though basis is unknown
        assert li.unit_price_as_printed is not None


def test_ambiguous_basis_with_model_stated_resolves():
    # tax-exempt: subtotal == grand_total, so both hypotheses match arithmetically.
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000", tax_rate="KCT"),
        ],
        printed_subtotal="200.000",
        printed_grand_total="200.000",
        printed_tax_rate="KCT",
        stated_price_basis="pre_tax",
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "pre_tax"
    assert result.price_basis.resolved_by == "model_stated_fallback"
    codes = {f.code for f in result.all_findings}
    assert FindingCode.PRICE_BASIS_AMBIGUOUS in codes
    assert FindingCode.PRICE_BASIS_UNRESOLVED not in codes
    assert FindingCode.TAX_EXEMPT in codes


def test_tax_neutral_basis_without_model_stated_exposes_equal_values():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000", tax_rate="KCT"),
        ],
        printed_subtotal="200.000",
        printed_grand_total="200.000",
        printed_tax_rate="KCT",
        stated_price_basis="unknown",
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "unknown"
    assert result.price_basis.resolved_by == "tax_neutral"
    codes = {f.code for f in result.all_findings}
    assert FindingCode.PRICE_BASIS_AMBIGUOUS in codes
    assert FindingCode.PRICE_BASIS_UNRESOLVED not in codes
    line = result.line_items[0]
    assert line.unit_price_pre_tax == 100_000
    assert line.unit_price_post_tax == 100_000
    assert line.line_total_pre_tax == 200_000
    assert line.line_total_post_tax == 200_000


def test_equal_subtotal_and_grand_total_is_tax_neutral_without_tax_marker():
    raw = make_raw(
        line_items=[make_line(quantity="1", unit_price="800.000", line_total="800.000")],
        printed_subtotal="800.000",
        printed_grand_total="800.000",
        stated_price_basis="unknown",
    )

    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "unknown"
    assert result.price_basis.resolved_by == "tax_neutral"
    assert FindingCode.PRICE_BASIS_UNRESOLVED not in {f.code for f in result.all_findings}
    assert result.line_items[0].line_total_pre_tax == 800_000
    assert result.line_items[0].line_total_post_tax == 800_000


def test_model_stated_basis_overridden_by_arithmetic():
    raw = make_raw(
        line_items=[
            make_line(quantity="2", unit_price="100.000", line_total="200.000"),
            make_line(
                product_code="SKU-002", quantity="3", unit_price="50.000", line_total="150.000"
            ),
        ],
        printed_subtotal="350.000",
        printed_tax_rate="10%",
        printed_grand_total="385.000",
        stated_price_basis="post_tax",  # wrong: arithmetic will resolve pre_tax
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "pre_tax"
    assert result.price_basis.agrees_with_model is False
    codes = {f.code for f in result.document_findings}
    assert FindingCode.PRICE_BASIS_MODEL_DISAGREEMENT in codes
    disagreement = next(
        f for f in result.document_findings if f.code == FindingCode.PRICE_BASIS_MODEL_DISAGREEMENT
    )
    assert disagreement.severity == Severity.WARNING


def test_tax_rate_inferred_when_never_printed_as_a_percentage():
    # Regression case from a real "Phieu nhap mua hang" (FAST accounting software
    # export): quantity/amounts use a space as the thousands separator and a
    # comma as the decimal separator, and no tax *rate* is printed anywhere —
    # only a tax amount. Rate must be inferred as tax_amount / pre-tax base,
    # not left null, since that's a deterministic calculation, not a guess.
    raw = RawExtraction(
        supplier_name="CN Cong ty TNHH Phan Mem FAST tai TP.HCM",
        supplier_tax_code=None,
        invoice_number="NM001",
        invoice_date="26/10/2022",
        currency="VND",
        printed_subtotal="150 000 000",
        printed_discount="0",
        printed_tax_amount="15 000 000",
        printed_tax_rate=None,
        printed_grand_total="165 000 000",
        stated_price_basis="unknown",
        line_items=[
            RawLineItem(
                line_no="1",
                product_code="1551",
                product_name="Hang hoa 01",
                unit="Cai",
                quantity="1 000,00",
                unit_price="150 000",
                line_total="150 000 000",
                discount=None,
                tax_rate=None,
            )
        ],
        model_notes=None,
    )
    result = reconcile(raw, SETTINGS)

    assert result.price_basis.basis == "pre_tax"
    assert result.document.tax_rate == 0.1
    assert result.document.tax_amount_calculated == 15_000_000
    assert result.document.grand_total_calculated == 165_000_000

    line = result.line_items[0]
    assert line.quantity == 1000.0
    assert line.unit_price_post_tax == 165_000
    assert line.line_total_post_tax == 165_000_000

    codes = {f.code for f in result.document_findings}
    assert FindingCode.TAX_RATE_INFERRED in codes
