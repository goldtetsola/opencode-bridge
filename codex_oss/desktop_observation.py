"""DesktopObservationV1 — provenance for user-observed spawned-agent transcripts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from codex_oss.native_work_ux import native_work_note

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


def build_spawned_transcript_authority(
    *,
    mission_id: str,
    transcript_meta: JSON,
    witness: JSON,
    reconciliation: JSON,
    transcript_path: str,
    source: str,
    thread_id: str = "",
    spawned_agent_id: str = "",
) -> JSON:
    """Build SpawnedTranscriptAuthorityV1 from raw Desktop child evidence."""
    provenance = classify_transcript_provenance(transcript_meta if isinstance(transcript_meta, dict) else {})
    resolved_thread_id = str(thread_id or transcript_meta.get("thread_id") or transcript_meta.get("desktop_thread_id") or "")
    resolved_agent_id = str(spawned_agent_id or transcript_meta.get("spawned_agent_id") or transcript_meta.get("agent_id") or "")
    reasons: list[str] = []
    if not provenance.get("ok"):
        reasons.extend(str(reason) for reason in provenance.get("reasons", []) if str(reason))
    if not witness.get("ok"):
        reasons.append("consumer_observation_witness_failed")
    if not reconciliation.get("pass"):
        reasons.append("commentary_delivery_reconciliation_failed")
    return {
        "schema_version": "spawned_transcript_authority.v1",
        "ok": not reasons,
        "mission_id": str(mission_id or ""),
        "spawned_agent_id": resolved_agent_id,
        "desktop_thread_id": resolved_thread_id,
        "transcript_path": str(transcript_path or ""),
        "consumer_kind": provenance.get("consumer_kind", ""),
        "transcript_kind": provenance.get("transcript_kind", ""),
        "source": str(source or ""),
        "claim_scope": "desktop_observed" if not reasons else "desktop_unproven",
        "basis": "raw_desktop_child_transcript" if not reasons else "capture_authority_not_proven",
        "pre_final_progress_count": int(witness.get("progress_before_final", 0) or 0),
        "matched_event_ids_before_final": list(witness.get("matched_event_ids_before_final", []) or []),
        "final_present": bool(witness.get("final_present")),
        "witness_path": "consumer_observation_witness.json",
        "reconciliation_path": "commentary_delivery_reconciliation.json",
        "provenance": provenance,
        "reasons": reasons,
    }


def capture_unavailable_result(
    *,
    mission_id: str,
    attempted_handles: list[JSON] | None = None,
    reason: str = "desktop_child_transcript_capture_unavailable",
) -> JSON:
    """Return a structured fail-closed result when child transcript capture is unavailable."""
    return {
        "schema_version": "spawned_transcript_capture_attempt.v1",
        "ok": False,
        "status": "CAPTURE_UNAVAILABLE",
        "mission_id": str(mission_id or ""),
        "attempted_handles": list(attempted_handles or []),
        "next_required_artifact": "spawned_transcript_authority.json",
        "reasons": [str(reason or "desktop_child_transcript_capture_unavailable")],
    }


def capture_thread_observation(
    *,
    project_root: str,
    mission_id: str,
    transcript_path: str,
    thread_id: str = "",
    spawned_agent_id: str = "",
    source: str = "codex_app.read_thread",
) -> JSON:
    """Persist a Desktop transcript witness and non-destructive reconciliation.

    The CLI cannot call Codex Desktop APIs by itself; callers provide a raw
    exported transcript from the Desktop spawned-agent thread. This function
    records that transcript as the consumer authority and reconciles it against
    producer-side commentary artifacts without mutating commentary_delivery.json.
    """
    mission = str(mission_id or "").strip()
    mission_dir = Path(project_root) / ".codex-oss" / "missions" / mission
    mission_dir.mkdir(parents=True, exist_ok=True)
    input_path = Path(transcript_path)
    try:
        transcript = json.loads(input_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": "desktop_observation_capture.v1",
            "ok": False,
            "mission_id": mission,
            "reasons": [f"transcript_unreadable:{exc}"],
        }
    if not isinstance(transcript, dict):
        return {
            "schema_version": "desktop_observation_capture.v1",
            "ok": False,
            "mission_id": mission,
            "reasons": ["transcript_must_be_object"],
        }

    transcript = dict(transcript)
    transcript.setdefault("mission_id", mission)
    transcript.setdefault("consumer_kind", "codex_desktop_spawned")
    transcript.setdefault("transcript_kind", "codex_desktop_raw_export")
    if thread_id:
        transcript["thread_id"] = thread_id
    if spawned_agent_id:
        transcript["spawned_agent_id"] = spawned_agent_id
    transcript["source"] = source
    messages = [message for message in transcript.get("messages", []) if isinstance(message, dict)]
    messages = _annotate_messages_with_visible_event_ids(messages, mission_dir)
    transcript["messages"] = messages
    output_transcript = mission_dir / "desktop_thread_transcript.json"
    output_transcript.write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    delivery = _read_json(mission_dir / "commentary_delivery.json")
    emitted_ids = _delivery_event_ids(delivery)
    witness = build_consumer_observation_witness(
        mission_id=mission,
        transcript_meta=transcript,
        messages=messages,
        min_progress_before_final=3,
        expected_event_ids=emitted_ids,
    )
    witness.update({
        "thread_id": thread_id or str(transcript.get("thread_id") or ""),
        "source": source,
        "transcript_path": str(output_transcript),
        "route_authority_path": str(mission_dir / "route_authority.json"),
        "observed_before_final_count": int(witness.get("progress_before_final", 0) or 0),
        "rendered_before_final_event_ids": list(witness.get("matched_event_ids_before_final", []) or []),
        "final_message_id": _final_message_id(messages),
        "first_progress_at": _first_progress_at(messages),
        "final_at": _final_at(messages),
    })
    (mission_dir / "consumer_observation_witness.json").write_text(
        json.dumps(witness, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    reconciliation = _commentary_delivery_reconciliation(delivery, witness, messages)
    (mission_dir / "commentary_delivery_reconciliation.json").write_text(
        json.dumps(reconciliation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    spawned_authority = build_spawned_transcript_authority(
        mission_id=mission,
        transcript_meta=transcript,
        witness=witness,
        reconciliation=reconciliation,
        transcript_path=str(output_transcript),
        source=source,
        thread_id=thread_id,
        spawned_agent_id=spawned_agent_id,
    )
    (mission_dir / "spawned_transcript_authority.json").write_text(
        json.dumps(spawned_authority, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": "desktop_observation_capture.v1",
        "ok": bool(spawned_authority.get("ok")),
        "mission_id": mission,
        "transcript_path": str(output_transcript),
        "consumer_observation_witness_path": str(mission_dir / "consumer_observation_witness.json"),
        "commentary_delivery_reconciliation_path": str(mission_dir / "commentary_delivery_reconciliation.json"),
        "spawned_transcript_authority_path": str(mission_dir / "spawned_transcript_authority.json"),
        "consumer_observation_witness": witness,
        "commentary_delivery_reconciliation": reconciliation,
        "spawned_transcript_authority": spawned_authority,
        "reasons": list(spawned_authority.get("reasons", []) or []),
    }


def _progress_before_final(messages: list[JSON]) -> int:
    count = 0
    for message in messages:
        if _is_final_message(message):
            break
        if _is_progress_message(message):
            count += 1
    return count


def _annotate_messages_with_visible_event_ids(messages: list[JSON], mission_dir: Path) -> list[JSON]:
    visible_events = _visible_events_by_message(mission_dir / "visible_commentary.jsonl")
    if not visible_events:
        return list(messages)
    annotated: list[JSON] = []
    used: set[str] = set()
    for message in messages:
        item = dict(message)
        text = str(item.get("text") or "")
        candidates = visible_events.get(text) or []
        for event in candidates:
            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in used:
                continue
            item.setdefault("event_id", event_id)
            metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
            metadata.setdefault("oss_visible_event", {
                "mission_id": str(event.get("mission_id") or ""),
                "event_id": event_id,
                "seq": event.get("seq"),
                "event_type": str(event.get("event_type") or ""),
                "safe_for_user": bool(event.get("safe_for_user", True)),
            })
            item["metadata"] = metadata
            used.add(event_id)
            break
        annotated.append(item)
    return annotated


def _visible_events_by_message(path: Path) -> dict[str, list[JSON]]:
    out: dict[str, list[JSON]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = str(event.get("message") or "")
        event_id = str(event.get("event_id") or "")
        if not event_id:
            continue
        for candidate in {message, native_work_note(event)}:
            if candidate:
                out.setdefault(candidate, []).append(event)
    return out


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
    metadata = message.get("metadata") if isinstance(message, dict) else {}
    visible = metadata.get("oss_visible_event") if isinstance(metadata, dict) else None
    return (
        phase in {"commentary", "progress", "plan", "apply", "verify", "report"}
        or "[OSS progress]" in text
        or "oss-progress" in text
        or isinstance(visible, dict)
    )


def _is_final_message(message: JSON) -> bool:
    phase = str(message.get("phase", "") or "").lower()
    text = str(message.get("text", "") or "").lower()
    return phase in {"final", "final_answer", "answer"} or "oss_implementation_report_begin" in text


def _read_json(path: Path) -> JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _delivery_event_ids(delivery: JSON) -> list[str]:
    events = delivery.get("events", {}) if isinstance(delivery, dict) else {}
    if not isinstance(events, dict):
        return []
    out: set[str] = set()
    for key, event in events.items():
        if not isinstance(event, dict):
            continue
        states = event.get("states") if isinstance(event.get("states"), dict) else {}
        if states.get("created") or states.get("stream_enqueued") or states.get("sse_emitted"):
            event_id = str(event.get("event_id") or key or "")
            if event_id:
                out.add(event_id)
    return sorted(out)


def _commentary_delivery_reconciliation(delivery: JSON, witness: JSON, messages: list[JSON]) -> JSON:
    emitted_ids = set(_delivery_event_ids(delivery))
    observed_ids = set(str(item) for item in witness.get("observed_event_ids", []) if str(item))
    before_final_ids = set(str(item) for item in witness.get("matched_event_ids_before_final", []) if str(item))
    matched = sorted(emitted_ids & observed_ids)
    missing = sorted(emitted_ids - observed_ids)
    unexpected = sorted(observed_ids - emitted_ids)
    rendered = sorted(emitted_ids & before_final_ids)
    reasons: list[str] = []
    if not delivery:
        reasons.append("commentary_delivery_missing")
    if len(rendered) < 3:
        reasons.append("matched_pre_final_event_ids_below_floor")
    return {
        "schema_version": "commentary_delivery_reconciliation.v1",
        "mission_id": str(witness.get("mission_id", "") or ""),
        "emitted_count": len(emitted_ids),
        "observed_count": len(observed_ids),
        "rendered_before_final_count": len(rendered),
        "matched_event_ids": matched,
        "rendered_before_final_event_ids": rendered,
        "missing_from_transcript": missing,
        "unexpected_transcript_progress": unexpected,
        "message_count": len(messages),
        "pass": not reasons,
        "reasons": reasons,
    }


def _final_message_id(messages: list[JSON]) -> str:
    for idx, message in enumerate(messages):
        if _is_final_message(message):
            return str(message.get("id") or message.get("message_id") or idx)
    return ""


def _first_progress_at(messages: list[JSON]) -> str:
    for message in messages:
        if _is_progress_message(message):
            return str(message.get("created_at") or message.get("timestamp") or message.get("time") or "")
    return ""


def _final_at(messages: list[JSON]) -> str:
    for message in messages:
        if _is_final_message(message):
            return str(message.get("created_at") or message.get("timestamp") or message.get("time") or "")
    return ""
