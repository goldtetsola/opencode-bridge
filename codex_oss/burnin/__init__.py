"""Burn-in reliability and quality validity for live OSS agent testing.

Provides:
  BurninRunManifestV1  — declared expected cases and preflight status
  BurninCaseResultV1   — per-case state, metrics, and quality grade
  BurninSummaryV1      — aggregate results across all cases
  FailureClassV1       — taxonomy of failure reasons
  QualityValidityV1    — earned/suspicious/truthful classification
  BurninPreflightV1    — runtime freshness, auth, supervision gates
  BurninHarness        — manifest-before-run, summary-in-finally, missing-case detection
"""

from codex_oss.burnin.models import (
    BurninRunManifest,
    BurninCaseResult,
    BurninSummary,
    FailureClass,
    CaseState,
    QualityGrade,
)
from codex_oss.burnin.quality import (
    classify_quality_validity,
    is_earned_complete,
    detect_suspicious_complete,
    is_false_complete,
)
from codex_oss.burnin.preflight import (
    run_burnin_preflight,
    PreflightResult,
)
from codex_oss.burnin.harness import (
    BurninHarness,
)

__all__ = [
    "BurninRunManifest",
    "BurninCaseResult",
    "BurninSummary",
    "FailureClass",
    "CaseState",
    "QualityGrade",
    "classify_quality_validity",
    "is_earned_complete",
    "detect_suspicious_complete",
    "is_false_complete",
    "run_burnin_preflight",
    "PreflightResult",
    "BurninHarness",
]
