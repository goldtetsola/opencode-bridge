"""MissionEventLog event vocabulary and payload hygiene."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

JSON = dict[str, Any]

EVENT_SCHEMA_VERSION = "mission_event.v1"

SOURCE_KINDS = {
    "runtime",
    "desktop_app",
    "adapter",
    "model",
    "projection",
    "cli_import",
    "test_fixture",
}

AUTHORITIES = {
    "runtime_authoritative",
    "desktop_observed",
    "model_narrative",
    "compatibility_projection",
    "diagnostic_only",
}

EVENT_TYPES = {
    "MissionAdmitted",
    "ModelNarrationEmitted",
    "RuntimeProgressEmitted",
    "DesktopTranscriptCaptured",
    "DesktopMessageObserved",
    "ToolCallEmitted",
    "DesktopToolCallResolved",
    "RuntimeRecoveryRecorded",
    "PatchIntentProposed",
    "PatchAppliedByRuntime",
    "VerificationRan",
    "RollbackRecorded",
    "MissionFinalized",
    "ProjectionWritten",
    "ClaimGateEvaluated",
}

SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization|credential)", re.I)


class MissionEventError(ValueError):
    """Raised when an event cannot enter the append-only mission log."""


def validate_event_parts(*, event_type: str, source_kind: str, authority: str, created_at: str, payload: JSON) -> None:
    if event_type not in EVENT_TYPES:
        raise MissionEventError(f"unsupported_event_type:{event_type}")
    if source_kind not in SOURCE_KINDS:
        raise MissionEventError(f"unsupported_source_kind:{source_kind}")
    if authority not in AUTHORITIES:
        raise MissionEventError(f"unsupported_authority:{authority}")
    if not isinstance(payload, dict):
        raise MissionEventError("payload_must_be_object")
    _parse_timestamp(created_at)


def validate_payload(event_type: str, payload: JSON) -> None:
    required: dict[str, tuple[str, ...]] = {
        "MissionAdmitted": ("task_spec", "risk_tier", "allowed_tool_classes", "write_allowed", "model_alias", "route_class"),
        "ModelNarrationEmitted": ("text", "visible", "pre_final"),
        "RuntimeProgressEmitted": ("progress_type", "text", "visible"),
        "DesktopTranscriptCaptured": ("agent_id", "thread_id", "transcript_kind", "consumer_kind", "capture_method"),
        "DesktopMessageObserved": ("agent_id", "message_id", "text", "pre_final", "final"),
        "ToolCallEmitted": ("call_id", "tool_name", "arguments_hash", "tool_class", "response_id"),
        "DesktopToolCallResolved": ("call_id", "output_hash", "consumer_kind", "adopted"),
        "RuntimeRecoveryRecorded": ("call_id", "recovery_class", "status", "reason", "fail_closed"),
        "PatchIntentProposed": ("owned_paths", "rationale", "model_alias", "risk_tier"),
        "PatchAppliedByRuntime": ("owned_paths", "patch_hash", "apply_status"),
        "VerificationRan": ("command", "exit_code", "result", "duration_ms"),
        "RollbackRecorded": ("reason", "rollback_status", "affected_paths"),
        "MissionFinalized": ("status", "confidence", "summary_hash", "final_text"),
        "ProjectionWritten": ("projection_name", "path", "source_run_record_hash"),
        "ClaimGateEvaluated": ("target", "status", "allowed_claims", "disallowed_claims", "basis_event_ids"),
    }
    missing = [key for key in required.get(event_type, ()) if key not in payload]
    if missing:
        raise MissionEventError(f"payload_missing:{','.join(missing)}")


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: JSON = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str) and _looks_like_secret(value):
        return "[REDACTED]"
    return value


def _looks_like_secret(value: str) -> bool:
    if len(value) < 24:
        return False
    return bool(re.match(r"^(sk-|ghp_|xox|Bearer\s+)", value))


def _parse_timestamp(created_at: str) -> None:
    if not created_at or not isinstance(created_at, str):
        raise MissionEventError("created_at_missing")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MissionEventError("created_at_invalid") from exc
    if parsed.tzinfo is None:
        raise MissionEventError("created_at_timezone_missing")
