"""DesktopObservationV1 — provenance for user-observed spawned-agent transcripts."""

from __future__ import annotations

import time
from typing import Any

JSON = dict[str, Any]

RAW_DESKTOP_TRANSCRIPT_KINDS = {
    "codex_desktop_raw_export",
    "codex_desktop_spawned_raw_transcript",
    "desktop_raw_transcript",
}

RECONSTRUCTED_TRANSCRIPT_KINDS = {
    "artifact_reconciled_candidate_not_raw_desktop_export",
    "artifact_reconciled_candidate",
    "bridge_harness_transcript",
    "desktop_terminal_notification_only",
}


def classify_transcript_provenance(transcript_meta: JSON) -> JSON:
    """Classify whether transcript metadata can prove Desktop-observed UX."""

    consumer_kind = str(transcript_meta.get("consumer_kind", "") or "")
    transcript_kind = str(transcript_meta.get("transcript_kind", "") or "")
    reasons: list[str] = []

    if consumer_kind != "codex_desktop_spawned":
        reasons.append("consumer_kind_not_codex_desktop_spawned")
    if transcript_kind not in RAW_DESKTOP_TRANSCRIPT_KINDS:
        if transcript_kind in RECONSTRUCTED_TRANSCRIPT_KINDS:
            reasons.append("transcript_is_not_raw_desktop_export")
        else:
            reasons.append("raw_desktop_transcript_kind_missing")

    ok = not reasons
    return {
        "schema_version": "desktop_transcript_provenance.v1",
        "ok": ok,
        "consumer_kind": consumer_kind,
        "transcript_kind": transcript_kind,
        "claim_scope": "desktop_observed" if ok else "desktop_unproven",
        "reasons": reasons,
    }


def build_consumer_observation_witness(
    *,
    mission_id: str,
    transcript_meta: JSON,
    messages: list[JSON],
    min_progress_before_final: int = 3,
    expected_event_ids: list[str] | None = None,
) -> JSON:
    """Build ConsumerObservationWitnessV1 from raw Desktop-observed messages.

    This witness is the Desktop Consumer Witness Lane. It deliberately depends
    on raw Desktop transcript provenance plus observed messages before final;
    producer-side runtime artifacts cannot make this witness pass.
    """
    expected_mission = str(mission_id or "").strip()
    provenance = classify_transcript_provenance(transcript_meta if isinstance(transcript_meta, dict) else {})
    observed = [message for message in messages if isinstance(message, dict)]
    progress_before_final = _progress_before_final(observed)
    event_ids_before_final = _event_ids_before_final(observed)
    all_event_ids = _event_ids(observed)
    expected_ids = {str(event_id) for event_id in (expected_event_ids or []) if str(event_id)}
    matched_ids_before_final = sorted(event_ids_before_final & expected_ids) if expected_ids else sorted(event_ids_before_final)
    identity_progress_before_final = len(matched_ids_before_final)
    final_present = any(_is_final_message(message) for message in observed)
    transcript_mission = str((transcript_meta or {}).get("mission_id", "") or "").strip()
    reasons: list[str] = []
    if not provenance.get("ok"):
        reasons.extend(str(reason) for reason in provenance.get("reasons", []) if str(reason))
    if not expected_mission or transcript_mission != expected_mission:
        reasons.append("desktop_transcript_mission_id_mismatch")
    if progress_before_final < min_progress_before_final:
        reasons.append("desktop_progress_before_final_missing")
    if identity_progress_before_final < min_progress_before_final:
        reasons.append("desktop_progress_event_identity_missing")
    if expected_ids and len(event_ids_before_final & expected_ids) < min_progress_before_final:
        reasons.append("desktop_progress_event_reconciliation_missing")
    if not final_present:
        reasons.append("desktop_final_missing")
    return {
        "schema_version": "consumer_observation_witness.v1",
        "ok": not reasons,
        "consumer_kind": provenance.get("consumer_kind", ""),
        "transcript_kind": provenance.get("transcript_kind", ""),
        "mission_id": expected_mission,
        "transcript_mission_id": transcript_mission,
        "progress_before_final": progress_before_final,
        "identity_progress_before_final": identity_progress_before_final,
        "min_progress_before_final": min_progress_before_final,
        "observed_event_ids": sorted(all_event_ids),
        "observed_event_ids_before_final": sorted(event_ids_before_final),
        "expected_event_ids": sorted(expected_ids),
        "matched_event_ids_before_final": matched_ids_before_final,
        "final_present": final_present,
        "raw_desktop_provenance_ok": bool(provenance.get("ok")),
        "provenance": provenance,
        "reasons": reasons,
    }


def build_raw_desktop_transcript(
    *,
    mission_id: str,
    messages: list[JSON],
    agent_id: str = "",
    agent_name: str = "",
    transcript_kind: str = "codex_desktop_raw_export",
    captured_at: float | None = None,
) -> JSON:
    """Build a Desktop transcript bundle from already-observed Desktop messages.

    The caller must supply messages captured from the real Codex Desktop
    spawned-agent surface. This function does not convert artifact commentary
    into Desktop proof.
    """

    return {
        "schema_version": "desktop_observation_transcript.v1",
        "mission_id": str(mission_id),
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": transcript_kind,
        "agent_id": str(agent_id or ""),
        "agent_name": str(agent_name or ""),
        "captured_at": captured_at if captured_at is not None else time.time(),
        "messages": [message for message in messages if isinstance(message, dict)],
    }


def _progress_before_final(messages: list[JSON]) -> int:
    count = 0
    for message in messages:
        if _is_final_message(message):
            break
        if _is_progress_message(message):
            count += 1
    return count


def _event_ids(messages: list[JSON]) -> set[str]:
    return {event_id for event_id in (_event_id(message) for message in messages) if event_id}


def _event_ids_before_final(messages: list[JSON]) -> set[str]:
    event_ids: set[str] = set()
    for message in messages:
        if _is_final_message(message):
            break
        event_id = _event_id(message)
        if event_id:
            event_ids.add(event_id)
    return event_ids


def _event_id(message: JSON) -> str:
    event_id = str(message.get("event_id") or "")
    if event_id:
        return event_id
    metadata = message.get("metadata") if isinstance(message, dict) else {}
    visible = metadata.get("oss_visible_event") if isinstance(metadata, dict) else None
    if isinstance(visible, dict):
        return str(visible.get("event_id") or "")
    return ""


def _is_progress_message(message: JSON) -> bool:
    phase = str(message.get("phase", "") or "").lower()
    text = str(message.get("text", "") or "")
    return phase in {"commentary", "progress", "plan", "apply", "verify", "report"} or "[OSS progress]" in text


def _is_final_message(message: JSON) -> bool:
    phase = str(message.get("phase", "") or "").lower()
    text = str(message.get("text", "") or "").lower()
    return phase in {"final", "final_answer", "answer"} or "oss_implementation_report_begin" in text
