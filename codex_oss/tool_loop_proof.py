"""Certification-facing tool-loop/recovery proof recorder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_oss.mission_event_bridge import ensure_mission_admitted_from_artifacts
from codex_oss.tool_call_adoption import build_adopted_probe, build_not_adopted_probe, persist_adoption_probes

JSON = dict[str, Any]


def record_tool_loop_proof(
    *,
    project_root: str,
    mission_id: str,
    call_id: str,
    tool_name: str,
    status: str,
    response_id: str = "response_desktop_observed",
    item_id: str = "",
    evidence_source: str = "",
) -> JSON:
    """Persist adopted/recovered Desktop pending-call evidence for a mission."""
    root = Path(project_root).resolve()
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    event_log = ensure_mission_admitted_from_artifacts(root, mission_id)
    normalized = status.replace("_", "-").lower()
    resolved_item = item_id or f"fc_{call_id}"
    if normalized == "adopted":
        probe = build_adopted_probe(response_id, resolved_item, call_id, tool_name, 1, 0)
        adoption_status = "PASS"
        recovery_status = ""
        recovery_used = False
    elif normalized in {"recovered", "fail-closed"}:
        reason = "desktop_pending_tool_call_recovered" if normalized == "recovered" else "desktop_pending_tool_call_fail_closed"
        probe = build_not_adopted_probe(response_id, resolved_item, call_id, tool_name, 1, 1, reason)
        adoption_status = "RECOVERED"
        recovery_status = "RECOVERED" if normalized == "recovered" else "FAIL_CLOSED"
        recovery_used = True
    else:
        return _write_failure(mission_dir, mission_id, f"unsupported_tool_loop_status:{status}")

    probe["evidence_source"] = evidence_source
    persist_adoption_probes(str(mission_dir), [probe], None)
    adoption = {
        "schema_version": "adoption_or_recovery.v1",
        "mission_id": mission_id,
        "route_class": "desktop_tool_loop",
        "status": adoption_status,
        "reason": "adoption_observed" if not recovery_used else "recovered_by_runtime",
        "pending_tool_calls_emitted": 1,
        "runtime_recovery_used": recovery_used,
        "artifacts": ["tool_call_adoption_probes.json"],
        "evidence_source": evidence_source,
    }
    _write_json(mission_dir / "adoption_or_recovery.json", adoption)
    _append_tool_loop_events(
        event_log=event_log,
        call_id=call_id,
        tool_name=tool_name,
        response_id=response_id,
        adopted=normalized == "adopted",
        recovered=normalized == "recovered",
        fail_closed=normalized == "fail-closed",
    )
    recovery: JSON = {}
    if recovery_status:
        recovery = {
            "schema_version": "recovery_proof.v1",
            "mission_id": mission_id,
            "status": recovery_status,
            "injected_failure": normalized == "fail-closed",
            "reason": probe.get("recovery_reason", ""),
            "call_id": call_id,
            "tool_name": tool_name,
            "evidence_source": evidence_source,
        }
        _write_json(mission_dir / "recovery_proof.json", recovery)
    report = {
        "schema_version": "tool_loop_recovery_proof.v1",
        "ok": True,
        "mission_id": mission_id,
        "status": adoption_status,
        "recovery_status": recovery_status or "NOT_APPLICABLE",
        "call_id": call_id,
        "tool_name": tool_name,
        "adoption_or_recovery_path": str(mission_dir / "adoption_or_recovery.json"),
        "tool_call_adoption_probes_path": str(mission_dir / "tool_call_adoption_probes.json"),
        "recovery_proof_path": str(mission_dir / "recovery_proof.json") if recovery else "",
        "evidence_source": evidence_source,
    }
    _write_json(mission_dir / "tool_loop_recovery_proof.json", report)
    return report


def _write_failure(mission_dir: Path, mission_id: str, reason: str) -> JSON:
    report = {
        "schema_version": "tool_loop_recovery_proof.v1",
        "ok": False,
        "mission_id": mission_id,
        "reasons": [reason],
    }
    _write_json(mission_dir / "tool_loop_recovery_proof.json", report)
    return report


def _write_json(path: Path, payload: JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_tool_loop_events(
    *,
    event_log: Any,
    call_id: str,
    tool_name: str,
    response_id: str,
    adopted: bool,
    recovered: bool,
    fail_closed: bool,
) -> None:
    existing_types = {(event.get("event_type"), (event.get("payload") or {}).get("call_id")) for event in event_log.read_events(verify=True)}
    if ("ToolCallEmitted", call_id) not in existing_types:
        event_log.append(
            "ToolCallEmitted",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments_hash": "sha256:desktop_observed",
                "tool_class": "read",
                "response_id": response_id,
            },
            source_kind="runtime",
            authority="runtime_authoritative",
        )
    if adopted and ("DesktopToolCallResolved", call_id) not in existing_types:
        event_log.append(
            "DesktopToolCallResolved",
            {
                "call_id": call_id,
                "output_hash": "sha256:desktop_observed",
                "consumer_kind": "codex_desktop_spawned",
                "adopted": True,
            },
            source_kind="desktop_app",
            authority="desktop_observed",
        )
    if (recovered or fail_closed) and ("RuntimeRecoveryRecorded", call_id) not in existing_types:
        event_log.append(
            "RuntimeRecoveryRecorded",
            {
                "call_id": call_id,
                "recovery_class": "desktop_pending_tool_call",
                "status": "recovered" if recovered else "fail_closed",
                "reason": "desktop_pending_tool_call_recovered" if recovered else "desktop_pending_tool_call_fail_closed",
                "fail_closed": fail_closed,
            },
            source_kind="runtime",
            authority="runtime_authoritative",
        )
