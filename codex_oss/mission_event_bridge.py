"""Bridge existing mission artifacts into the event-log runtime authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_oss.mission_event_log import MissionEventLog

JSON = dict[str, Any]


def ensure_mission_admitted_from_artifacts(project_root: str | Path, mission_id: str) -> MissionEventLog:
    root = Path(project_root).resolve()
    log = MissionEventLog.for_project(root, mission_id, mission_id=mission_id)
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    if any(event.get("event_type") == "MissionAdmitted" for event in log.read_events(verify=True)):
        report = _read_json(mission_dir / "report.json")
        append_implementation_events_from_report(log, report)
        append_desktop_events_from_transcript(root, mission_id, log)
        return log
    mission = _first_json([mission_dir / "mission_canonical.json", mission_dir / "mission.json"])
    report = _read_json(mission_dir / "report.json")
    log.append(
        "MissionAdmitted",
        {
            "task_spec": mission or {"mission_id": mission_id},
            "risk_tier": str(mission.get("risk_tier") or "low"),
            "allowed_tool_classes": list(mission.get("allowed_tool_classes") or []),
            "write_allowed": bool(mission.get("write_allowed", False)),
            "model_alias": str(report.get("runtime_model_alias") or report.get("explorer_model") or ""),
            "route_class": str(mission.get("mode") or report.get("mode") or "managed_investigation"),
        },
        source_kind="runtime",
        authority="runtime_authoritative",
    )
    append_implementation_events_from_report(log, report)
    append_desktop_events_from_transcript(root, mission_id, log)
    return log


def project_root_from_mission_dir(artifact_dir: str | Path) -> Path | None:
    path = Path(artifact_dir).resolve()
    parts = path.parts
    for idx in range(len(parts) - 2):
        if parts[idx] == ".codex-oss" and parts[idx + 1] == "missions":
            return Path(*parts[:idx])
    return None


def mission_id_from_artifact_dir(artifact_dir: str | Path) -> str:
    path = Path(artifact_dir).resolve()
    parts = path.parts
    for idx in range(len(parts) - 2):
        if parts[idx] == ".codex-oss" and parts[idx + 1] == "missions":
            return parts[idx + 2]
    return path.name


def append_finalized_from_report(project_root: str | Path, mission_id: str, report: JSON) -> None:
    log = ensure_mission_admitted_from_artifacts(project_root, mission_id)
    if any(event.get("event_type") == "MissionFinalized" for event in log.read_events(verify=True)):
        return
    status = str(report.get("status") or report.get("terminal_status") or "")
    if not status:
        return
    log.append(
        "MissionFinalized",
        {
            "status": status,
            "confidence": str(report.get("confidence") or ""),
            "summary_hash": "",
            "final_text": str(report.get("final_text") or report.get("summary") or ""),
        },
        source_kind="runtime",
        authority="runtime_authoritative",
    )


def append_implementation_events_from_report(log: MissionEventLog, report: JSON) -> None:
    if not report.get("implementation_report_version") and str(report.get("status") or "").upper() != "VERIFIED":
        return
    existing = {event.get("event_type") for event in log.read_events(verify=True)}
    authority = report.get("implementation_authority") if isinstance(report.get("implementation_authority"), dict) else {}
    if "PatchIntentProposed" not in existing:
        log.append(
            "PatchIntentProposed",
            {
                "owned_paths": list(report.get("changed_owned_paths") or report.get("owned_paths") or []),
                "rationale": "legacy_implementation_report_import",
                "model_alias": str(report.get("runtime_model_alias") or ""),
                "risk_tier": str(report.get("risk_tier") or "low"),
            },
            source_kind="model",
            authority="model_narrative",
        )
    if "PatchAppliedByRuntime" not in existing and (report.get("runtime_built_diff") or report.get("patch_artifact")):
        log.append(
            "PatchAppliedByRuntime",
            {
                "owned_paths": list(report.get("changed_owned_paths") or report.get("owned_paths") or []),
                "patch_hash": str(report.get("patch_hash") or "sha256:legacy_implementation_report"),
                "apply_status": "success" if str(authority.get("patch_authority") or "") == "bridge_runtime" else "unknown",
            },
            source_kind="runtime",
            authority="runtime_authoritative",
        )
    if "VerificationRan" not in existing:
        for item in report.get("verification", []) if isinstance(report.get("verification"), list) else []:
            if not isinstance(item, dict):
                continue
            log.append(
                "VerificationRan",
                {
                    "command": item.get("command") or [],
                    "exit_code": _exit_code(item),
                    "result": "pass" if _exit_code(item) == 0 else "fail",
                    "duration_ms": int(item.get("duration_ms", 0) or 0),
                },
                source_kind="runtime",
                authority="runtime_authoritative",
            )


def append_desktop_events_from_transcript(project_root: Path, mission_id: str, log: MissionEventLog) -> None:
    if any(event.get("event_type") == "DesktopTranscriptCaptured" for event in log.read_events(verify=True)):
        return
    transcript = project_root / ".codex-oss" / "missions" / mission_id / "desktop_thread_transcript.json"
    if not transcript.exists():
        return
    from codex_oss.desktop_transcript_ingestor import ingest_desktop_transcript

    payload = _read_json(transcript)
    expected_agent_id = str(payload.get("spawned_agent_id") or payload.get("agent_id") or payload.get("thread_id") or "")
    ingest_desktop_transcript(
        project_root=project_root,
        run_id=mission_id,
        mission_id=mission_id,
        transcript_path=transcript,
        expected_agent_id=expected_agent_id,
    )


def _exit_code(item: JSON) -> int:
    try:
        return int(item.get("exit_code", 1))
    except (TypeError, ValueError):
        return 1


def _first_json(paths: list[Path]) -> JSON:
    for path in paths:
        payload = _read_json(path)
        if payload:
            return payload
    return {}


def _read_json(path: Path) -> JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
