"""Explicit run manifest parsing for aggregate parity claims."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

JSON = dict[str, Any]

REQUIRED_LANES = {"desktop_progress", "tool_loop", "recovery", "implementation"}


def load_run_manifest(path: str | Path) -> JSON:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("run_manifest_unreadable") from exc
    return validate_run_manifest(payload)


def validate_run_manifest(payload: JSON) -> JSON:
    if not isinstance(payload, dict):
        raise ValueError("run_manifest_must_be_object")
    if payload.get("schema_version") != "run_manifest.v1":
        raise ValueError("run_manifest_schema_invalid")
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("run_manifest_runs_missing")
    normalized: list[JSON] = []
    for item in runs:
        if not isinstance(item, dict):
            raise ValueError("run_manifest_run_must_be_object")
        run_id = str(item.get("run_id") or "")
        lanes = [str(lane) for lane in item.get("lanes", []) if str(lane)]
        if not run_id:
            raise ValueError("run_manifest_run_id_missing")
        if not lanes:
            raise ValueError(f"run_manifest_lanes_missing:{run_id}")
        normalized.append({"run_id": run_id, "mission_id": str(item.get("mission_id") or run_id), "lanes": lanes})
    present = {lane for item in normalized for lane in item["lanes"]}
    return {
        "schema_version": "run_manifest.v1",
        "ok": True,
        "runs": normalized,
        "coverage": {
            "lanes": sorted(present),
            "missing_lanes": sorted(REQUIRED_LANES - present),
        },
    }
