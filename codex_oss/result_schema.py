"""OSSSubagentResultV1 canonical JSON and Markdown projection."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

JSON = dict[str, Any]

REQUIRED_RESULT_FIELDS = (
    "run_id",
    "mode",
    "runtime_status",
    "acceptance_status",
    "confidence",
    "inspected_files",
    "changed_files",
    "claims",
    "verification_adequacy",
    "caveats",
    "escalation_recommendation",
    "review_required",
)


def build_oss_subagent_result(
    *,
    run_id: str,
    mode: str,
    runtime_status: str,
    acceptance_status: str,
    confidence: str,
    inspected_files: Sequence[str] = (),
    changed_files: Sequence[str] = (),
    claims: Sequence[Mapping[str, Any]] = (),
    verification_adequacy: Mapping[str, Any] | None = None,
    caveats: Sequence[str] = (),
    escalation_recommendation: str = "",
    review_required: bool = False,
) -> JSON:
    result = {
        "schema_version": "oss_subagent_result.v1",
        "run_id": run_id,
        "mode": mode,
        "runtime_status": runtime_status,
        "acceptance_status": acceptance_status,
        "confidence": confidence,
        "inspected_files": list(inspected_files),
        "changed_files": list(changed_files),
        "claims": [dict(claim) for claim in claims],
        "verification_adequacy": dict(verification_adequacy or {}),
        "caveats": list(caveats),
        "escalation_recommendation": escalation_recommendation,
        "review_required": bool(review_required),
    }
    validate_oss_subagent_result(result)
    return result


def validate_oss_subagent_result(result: Mapping[str, Any]) -> None:
    missing = [field for field in REQUIRED_RESULT_FIELDS if field not in result]
    if missing:
        raise ValueError(f"OSSSubagentResultV1 missing fields: {', '.join(missing)}")


def markdown_projection(result: Mapping[str, Any]) -> str:
    validate_oss_subagent_result(result)
    return "\n".join(
        [
            f"Status: {result['runtime_status']}",
            f"Acceptance: {result['acceptance_status']}",
            f"Confidence: {result['confidence']}",
            f"Review required: {str(bool(result['review_required'])).lower()}",
        ]
    )
