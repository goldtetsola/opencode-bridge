"""DecisionTraceV1 helpers for explainable runtime policy decisions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def append_decision(
    mission: Any,
    *,
    decision_type: str,
    result: str,
    policy: str,
    reason: str,
    source_module: str,
    input_payload: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    trace = getattr(mission, "decision_trace", None)
    if not isinstance(trace, list):
        trace = []
        setattr(mission, "decision_trace", trace)
    entry = {
        "decision_type": decision_type,
        "result": result,
        "policy": policy,
        "reason": reason,
        "source_module": source_module,
        "input": dict(input_payload or {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        entry.update(dict(extra))
    trace.append(entry)


def write_decision_trace(project_root: str, mission_id: str, mission: Any) -> str | None:
    trace = getattr(mission, "decision_trace", None)
    if not isinstance(trace, list):
        trace = []
        setattr(mission, "decision_trace", trace)
    artifact_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, "decision_trace.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "decision_trace_version": "1.0",
                "mission_id": mission_id,
                "decisions": trace,
            },
            handle,
            indent=2,
            sort_keys=True,
        )
    return path


def read_decision_trace(project_root: str, mission_id: str) -> dict[str, Any] | None:
    path = os.path.join(project_root, ".codex-oss", "missions", mission_id, "decision_trace.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
