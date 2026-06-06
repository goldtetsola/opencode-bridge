"""Reducer from MissionEventLog history to RunRecord state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codex_oss.mission_event_log import hash_json, verify_event_chain

JSON = dict[str, Any]


@dataclass
class RunRecord:
    run_id: str
    mission_id: str
    task_spec: JSON = field(default_factory=dict)
    route_authority: JSON = field(default_factory=dict)
    desktop_observation: JSON = field(default_factory=dict)
    tool_calls: JSON = field(default_factory=dict)
    implementation: JSON = field(default_factory=dict)
    evidence: JSON = field(default_factory=dict)
    final_status: JSON = field(default_factory=dict)
    claim_state: JSON = field(default_factory=dict)
    projection_state: JSON = field(default_factory=dict)
    event_ids_by_type: dict[str, list[str]] = field(default_factory=dict)
    source_event_hash: str = ""

    def to_dict(self) -> JSON:
        payload = {
            "schema_version": "run_record.v1",
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "task_spec": self.task_spec,
            "route_authority": self.route_authority,
            "desktop_observation": self.desktop_observation,
            "tool_calls": self.tool_calls,
            "implementation": self.implementation,
            "evidence": self.evidence,
            "final_status": self.final_status,
            "claim_state": self.claim_state,
            "projection_state": self.projection_state,
            "event_ids_by_type": self.event_ids_by_type,
            "source_event_hash": self.source_event_hash,
        }
        payload["run_record_hash"] = hash_json(payload)
        return payload


def build_run_record(events: list[JSON]) -> RunRecord:
    if not events:
        raise ValueError("mission_admitted_missing")
    verification = verify_event_chain(events)
    admitted = events[0]
    if admitted.get("event_type") != "MissionAdmitted":
        raise ValueError("mission_admitted_missing")
    task_spec = admitted.get("payload", {}).get("task_spec", {})
    run_id = str(admitted.get("run_id") or "")
    mission_id = str(admitted.get("mission_id") or run_id)
    record = RunRecord(
        run_id=run_id,
        mission_id=mission_id,
        task_spec=dict(task_spec) if isinstance(task_spec, dict) else {},
        route_authority={
            "route_class": str(admitted.get("payload", {}).get("route_class") or ""),
            "model_alias": str(admitted.get("payload", {}).get("model_alias") or ""),
            "write_allowed": bool(admitted.get("payload", {}).get("write_allowed")),
        },
        source_event_hash=str(verification.get("last_event_hash") or ""),
    )
    tool_calls: dict[str, JSON] = {}
    desktop_messages: list[JSON] = []
    implementation: JSON = {
        "patch_intents": [],
        "runtime_applies": [],
        "verification": [],
        "rollbacks": [],
    }
    projections: list[JSON] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        event_id = str(event.get("event_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        record.event_ids_by_type.setdefault(event_type, []).append(event_id)
        if event_type == "DesktopMessageObserved":
            desktop_messages.append({"event_id": event_id, **payload, "authority": event.get("authority")})
        elif event_type == "ToolCallEmitted":
            call = tool_calls.setdefault(str(payload.get("call_id") or ""), {"events": []})
            call.update({"call_id": payload.get("call_id"), "tool_name": payload.get("tool_name"), "emitted_event_id": event_id})
            call["events"].append(event_id)
        elif event_type == "DesktopToolCallResolved":
            call = tool_calls.setdefault(str(payload.get("call_id") or ""), {"events": []})
            call.update({"resolved_event_id": event_id, "adopted": bool(payload.get("adopted")), "consumer_kind": payload.get("consumer_kind")})
            call["events"].append(event_id)
        elif event_type == "RuntimeRecoveryRecorded":
            call = tool_calls.setdefault(str(payload.get("call_id") or ""), {"events": []})
            call.update({"recovery_event_id": event_id, "recovery_status": payload.get("status"), "fail_closed": bool(payload.get("fail_closed"))})
            call["events"].append(event_id)
        elif event_type == "PatchIntentProposed":
            implementation["patch_intents"].append({"event_id": event_id, **payload})
        elif event_type == "PatchAppliedByRuntime":
            implementation["runtime_applies"].append({"event_id": event_id, **payload})
        elif event_type == "VerificationRan":
            implementation["verification"].append({"event_id": event_id, **payload})
        elif event_type == "RollbackRecorded":
            implementation["rollbacks"].append({"event_id": event_id, **payload})
        elif event_type == "MissionFinalized":
            record.final_status = {"event_id": event_id, **payload}
        elif event_type == "ProjectionWritten":
            projections.append({"event_id": event_id, **payload})
    pre_final = [msg for msg in desktop_messages if msg.get("authority") == "desktop_observed" and msg.get("pre_final")]
    final = [msg for msg in desktop_messages if msg.get("authority") == "desktop_observed" and msg.get("final")]
    transcript_events = [event for event in events if event.get("event_type") == "DesktopTranscriptCaptured"]
    trusted_transcripts = [event for event in transcript_events if event.get("authority") == "desktop_observed"]
    diagnostic_transcripts = [event for event in transcript_events if event.get("authority") == "diagnostic_only"]
    diagnostic_methods = [
        str((event.get("payload") or {}).get("capture_method") or "")
        for event in diagnostic_transcripts
    ]
    record.desktop_observation = {
        "trusted_transcript_count": len(trusted_transcripts),
        "diagnostic_transcript_count": len(diagnostic_transcripts),
        "diagnostic_capture_methods": [] if trusted_transcripts else diagnostic_methods,
        "superseded_diagnostic_capture_methods": diagnostic_methods if trusted_transcripts else [],
        "pre_final_progress_count": len(pre_final),
        "final_message_count": len(final),
        "agent_ids": sorted({str(msg.get("agent_id") or "") for msg in desktop_messages if msg.get("agent_id")}),
        "ok": bool(trusted_transcripts) and len(pre_final) >= 3 and bool(final),
        "basis_event_ids": [str(event.get("event_id")) for event in trusted_transcripts] + [str(msg["event_id"]) for msg in pre_final + final],
    }
    record.tool_calls = {
        "items": list(tool_calls.values()),
        "all_resolved": bool(tool_calls) and all(call.get("resolved_event_id") or call.get("recovery_event_id") for call in tool_calls.values()),
        "basis_event_ids": [event_id for call in tool_calls.values() for event_id in call.get("events", [])],
    }
    verification_passed = bool(implementation["verification"]) and all(_exit_code(item) == 0 for item in implementation["verification"])
    runtime_apply_ok = any(str(item.get("apply_status") or "").lower() == "success" for item in implementation["runtime_applies"])
    record.implementation = {
        **implementation,
        "patch_intent_precedes_apply": _first_seq(events, "PatchIntentProposed") < _first_seq(events, "PatchAppliedByRuntime") if implementation["runtime_applies"] else bool(implementation["patch_intents"]),
        "runtime_apply_ok": runtime_apply_ok,
        "verification_passed": verification_passed,
        "ok": bool(implementation["patch_intents"]) and runtime_apply_ok and verification_passed,
    }
    record.evidence = {"event_count": len(events), "source_event_hash": record.source_event_hash}
    record.projection_state = {"projections": projections}
    return record


def _first_seq(events: list[JSON], event_type: str) -> int:
    for event in events:
        if event.get("event_type") == event_type:
            return int(event.get("seq", 0) or 0)
    return 10**9


def _exit_code(item: JSON) -> int:
    try:
        return int(item.get("exit_code", 1))
    except (TypeError, ValueError):
        return 1
