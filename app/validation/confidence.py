"""Confidence is earned from what reconciles, never self-reported by the
model. It starts at 1.0 and is docked per finding by severity; a
structurally broken document (totals that don't reconcile against anything)
is capped low regardless of how few findings accumulated elsewhere, because
an unverifiable document isn't "mostly fine."
"""

from __future__ import annotations

from app.schemas import Finding, FindingCode, Severity, Status
from app.settings import Settings

_UNRECONCILABLE_CAP = 0.4

_SEVERITY_WEIGHT_FIELD = {
    Severity.ERROR: "confidence_weight_error",
    Severity.WARNING: "confidence_weight_warning",
    Severity.INFO: "confidence_weight_info",
}


def compute_confidence(findings: list[Finding], settings: Settings) -> float:
    score = 1.0
    for f in findings:
        score -= getattr(settings, _SEVERITY_WEIGHT_FIELD[f.severity])

    if any(f.code == FindingCode.DOCUMENT_TOTALS_UNRECONCILABLE for f in findings):
        score = min(score, _UNRECONCILABLE_CAP)

    return max(0.0, min(1.0, score))


def derive_status(
    confidence: float, findings: list[Finding], line_item_count: int, settings: Settings
) -> Status:
    if line_item_count == 0 or confidence < settings.status_threshold_review:
        return "unusable"

    has_error = any(f.severity == Severity.ERROR for f in findings)
    if confidence < settings.status_threshold_usable or has_error:
        return "needs_human_review"

    return "usable"
