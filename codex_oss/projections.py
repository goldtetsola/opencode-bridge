"""Compatibility projections generated from RunRecord state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_oss.mission_event_log import MissionEventLog, hash_json
from codex_oss.run_record import build_run_record

JSON = dict[str, Any]


def write_run_record_projections(project_root: str | Path, run_id: str, *, mission_id: str = "") -> JSON:
    root = Path(project_root)
    events = MissionEventLog.for_project(root, run_id, mission_id=mission_id or run_id).read_events(verify=True)
    record = build_run_record(events).to_dict()
    resolved_mission_id = str(mission_id or record.get("mission_id") or run_id)
    mission_dir = root / ".codex-oss" / "missions" / resolved_mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    run_hash = str(record["run_record_hash"])
    projection_paths = {
        "run_record": mission_dir / "run_record.json",
        "mission_summary": mission_dir / "mission_summary.json",
    }
    _write_json(projection_paths["run_record"], record)
    summary = {
        "schema_version": "mission_summary.v1",
        "compatibility_projection": True,
        "source_run_id": run_id,
        "source_run_record_hash": run_hash,
        "mission_id": resolved_mission_id,
        "status": (record.get("final_status") or {}).get("status", ""),
        "confidence": (record.get("final_status") or {}).get("confidence", ""),
        "desktop_observation": record.get("desktop_observation", {}),
        "tool_calls": record.get("tool_calls", {}),
        "implementation": record.get("implementation", {}),
    }
    _write_json(projection_paths["mission_summary"], summary)
    return {
        "schema_version": "projection_writer_result.v1",
        "run_id": run_id,
        "mission_id": resolved_mission_id,
        "source_run_record_hash": run_hash,
        "compatibility_projection": True,
        "paths": {key: str(path) for key, path in projection_paths.items()},
    }


def check_projection_drift(project_root: str | Path, run_id: str, *, mission_id: str = "") -> JSON:
    root = Path(project_root)
    events = MissionEventLog.for_project(root, run_id, mission_id=mission_id or run_id).read_events(verify=True)
    current = build_run_record(events).to_dict()
    current_hash = str(current["run_record_hash"])
    resolved_mission_id = str(mission_id or current.get("mission_id") or run_id)
    mission_dir = root / ".codex-oss" / "missions" / resolved_mission_id
    drifted: list[str] = []
    for path in [mission_dir / "mission_summary.json"]:
        payload = _read_json(path)
        if payload and payload.get("source_run_record_hash") != current_hash:
            drifted.append(str(path))
    return {
        "schema_version": "projection_drift.v1",
        "ok": not drifted,
        "run_id": run_id,
        "mission_id": resolved_mission_id,
        "source_run_record_hash": current_hash,
        "drifted_paths": drifted,
    }


def _read_json(path: Path) -> JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
