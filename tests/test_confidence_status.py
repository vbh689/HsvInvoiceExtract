import pytest

from app.schemas import Finding, FindingCode, Severity
from app.settings import Settings
from app.validation.confidence import compute_confidence, derive_status

SETTINGS = Settings()


def _finding(severity: Severity, code: FindingCode = FindingCode.MISSING_REQUIRED_FIELD) -> Finding:
    return Finding(code=code, severity=severity, message="test")


@pytest.mark.parametrize(
    "findings, expected_confidence",
    [
        ([], 1.0),
        ([_finding(Severity.INFO)], 1.0),
        ([_finding(Severity.WARNING)], 0.92),
        ([_finding(Severity.ERROR)], 0.75),
        ([_finding(Severity.ERROR), _finding(Severity.ERROR)], 0.5),
        ([_finding(Severity.ERROR) for _ in range(10)], 0.0),  # clamped, not negative
    ],
)
def test_compute_confidence(findings, expected_confidence):
    assert compute_confidence(findings, SETTINGS) == pytest.approx(expected_confidence)


def test_unreconcilable_totals_cap_confidence_even_with_few_findings():
    findings = [_finding(Severity.ERROR, FindingCode.DOCUMENT_TOTALS_UNRECONCILABLE)]
    assert compute_confidence(findings, SETTINGS) <= 0.4


@pytest.mark.parametrize(
    "confidence, findings, line_count, expected_status",
    [
        (1.0, [], 1, "usable"),
        (0.92, [_finding(Severity.WARNING)], 1, "usable"),
        (0.75, [_finding(Severity.ERROR)], 1, "needs_human_review"),
        (0.5, [_finding(Severity.ERROR)], 1, "needs_human_review"),
        (0.2, [_finding(Severity.ERROR)], 1, "unusable"),
        (1.0, [], 0, "unusable"),
    ],
)
def test_derive_status(confidence, findings, line_count, expected_status):
    assert derive_status(confidence, findings, line_count, SETTINGS) == expected_status
