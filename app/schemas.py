"""Every schema in the service: what the vision model is allowed to produce
(`RawExtraction`), the finding taxonomy, and the final API contract
(`ExtractionResponse`). The model transcribes numbers as raw strings and
never computes — see app.normalize.vn_number and app.validation.reconcile
for where those strings become numbers and get checked.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---- Findings ----


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FindingCode(StrEnum):
    LINE_ARITHMETIC_MISMATCH = "LINE_ARITHMETIC_MISMATCH"
    LINE_ARITHMETIC_MISMATCH_MULTI = "LINE_ARITHMETIC_MISMATCH_MULTI"
    PRICE_BASIS_AMBIGUOUS = "PRICE_BASIS_AMBIGUOUS"
    PRICE_BASIS_MODEL_DISAGREEMENT = "PRICE_BASIS_MODEL_DISAGREEMENT"
    PRICE_BASIS_UNRESOLVED = "PRICE_BASIS_UNRESOLVED"
    SUBTOTAL_INFERRED = "SUBTOTAL_INFERRED"
    TAX_RATE_INFERRED = "TAX_RATE_INFERRED"
    DOCUMENT_TOTALS_UNRECONCILABLE = "DOCUMENT_TOTALS_UNRECONCILABLE"
    SUBTOTAL_TAX_GRANDTOTAL_MISMATCH = "SUBTOTAL_TAX_GRANDTOTAL_MISMATCH"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    MISSING_OPTIONAL_FIELD = "MISSING_OPTIONAL_FIELD"
    TAX_EXEMPT = "TAX_EXEMPT"
    EXTRACTION_EMPTY = "EXTRACTION_EMPTY"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"


class Finding(BaseModel):
    code: FindingCode
    severity: Severity
    message: str
    field_path: str | None = None
    # None = document-level finding. Set for line-item findings so the
    # flattened top-level findings[] (see ExtractionResponse) can be filtered
    # per line without a caller having to parse field_path.
    line_no: int | None = None


# ---- What the vision model is allowed to produce ----
#
# Every numeric field is a raw string, exactly as printed on the document.
# The model transcribes; it never parses "150.000" into a number and never
# decides whether "," means decimal or thousands — that ambiguity is resolved
# deterministically downstream in app.normalize.vn_number. This is the
# enforcement mechanism for "the model transcribes, it does not compute."


class RawLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Kept as a raw string for faithful transcription. The response's integer
    # line_no is generated from array order downstream.
    line_no: str | None
    product_code: str | None
    product_name: str | None
    unit: str | None
    quantity: str | None
    unit_price: str | None
    line_total: str | None
    discount: str | None
    tax_rate: str | None


class RawExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v2"] = "v2"
    # Required-but-nullable makes omission structurally different from a
    # deliberate null transcription.
    supplier_name: str | None
    supplier_tax_code: str | None
    invoice_number: str | None
    invoice_date: str | None
    currency: str | None
    printed_subtotal: str | None
    printed_tax_amount: str | None
    printed_tax_rate: str | None
    printed_grand_total: str | None
    printed_discount: str | None
    # Advisory only: used to raise PRICE_BASIS_MODEL_DISAGREEMENT when it
    # conflicts with the arithmetic-derived basis. Never authoritative.
    stated_price_basis: Literal["pre_tax", "post_tax", "unknown"]
    line_items: list[RawLineItem]
    # Free-text illegibility/uncertainty notes. Never a self-reported score.
    model_notes: str | None


# ---- The final API contract ----
#
# `status` is the field a downstream system can branch on without parsing
# anything else; everything downstream of it exists to justify or act on
# that value. Money fields are `int` (VND has no decimal subunits).
# Quantities and tax rates are `float`. All reconciliation math is done with
# Decimal internally (app.validation) and converted to these types only at
# this boundary.

Status = Literal["usable", "needs_human_review", "unusable"]
PriceBasis = Literal["pre_tax", "post_tax", "unknown"]
ResolvedBy = Literal[
    "line_vs_subtotal",
    "line_vs_grand_total",
    "model_stated_fallback",
    "tax_neutral",
    "insufficient_data",
]


class PriceBasisResolution(BaseModel):
    basis: PriceBasis
    resolved_by: ResolvedBy
    agrees_with_model: bool | None = None


class DocumentFields(BaseModel):
    supplier_name: str | None = None
    supplier_tax_code: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    currency: str = "VND"
    subtotal_printed: int | None = None
    subtotal_calculated: int | None = None
    tax_amount_printed: int | None = None
    tax_amount_calculated: int | None = None
    tax_rate: float | None = None
    grand_total_printed: int | None = None
    grand_total_calculated: int | None = None
    discount: int | None = None


class LineItemResult(BaseModel):
    line_no: int
    product_code: str | None = None
    product_name: str | None = None
    unit: str | None = None
    quantity: float | None = None
    unit_price_as_printed: int | None = None
    unit_price_pre_tax: int | None = None
    unit_price_post_tax: int | None = None
    line_total_pre_tax: int | None = None
    line_total_post_tax: int | None = None
    discount: int | None = None
    tax_rate: float | None = None


class UsageInfo(BaseModel):
    cached: bool
    model: str
    attempts: int
    latency_ms: float
    tokens_in: int
    tokens_out: int
    tokens_total: int
    cost_usd: float
    cost_vnd: int = 0  # cost_usd converted at settings.usd_to_vnd_rate
    prompt_version: str
    schema_version: str


class ExtractionResponse(BaseModel):
    request_id: str
    created_at: datetime
    status: Status
    confidence: float
    price_basis: PriceBasisResolution
    document: DocumentFields
    line_items: list[LineItemResult] = Field(default_factory=list)
    # Flattened: one array. Document-level findings have line_no: None.
    findings: list[Finding] = Field(default_factory=list)
    usage: UsageInfo
