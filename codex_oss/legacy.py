"""Legacy mission segregation helpers."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from .audit import audit_mission


def archive_legacy_missions(project_root: str, apply: bool = False) -> dict[str, Any]:
    missions_root = os.path.join(project_root, ".codex-oss", "missions")
    legacy_root = os.path.join(project_root, ".codex-oss", "missions-legacy")
    os.makedirs(missions_root, exist_ok=True)
    plan: list[dict[str, Any]] = []

    for mission_id in sorted(name for name in os.listdir(missions_root) if os.path.isdir(os.path.join(missions_root, name))):
        mission_dir = os.path.join(missions_root, mission_id)
        mission = _read_json_if_exists(os.path.join(mission_dir, "mission.json")) or {}
        reason = _legacy_reason(project_root, mission_id, mission)
        if not reason:
            continue
        destination = os.path.join(legacy_root, mission_id)
        plan.append({
            "mission_id": mission_id,
            "reason": reason,
            "source_dir": mission_dir,
            "destination_dir": destination,
        })
        if apply:
            os.makedirs(legacy_root, exist_ok=True)
            if os.path.exists(destination):
                shutil.rmtree(destination)
            shutil.move(mission_dir, destination)

    return {
        "archive_version": "1.0",
        "project_root": project_root,
        "apply": bool(apply),
        "archived_count": len(plan) if apply else 0,
        "planned_count": len(plan),
        "planned": plan,
    }


def _legacy_reason(project_root: str, mission_id: str, mission: dict[str, Any]) -> str:
    tier = str(mission.get("tier", "") or "")
    if not mission:
        return "missing mission.json"
    if tier not in {"A2", "A3", "A4", "A5", "A6"}:
        return f"non-current tier: {tier or 'unknown'}"
    if mission.get("promotion_proof") or mission.get("operational_burnin"):
        return ""
    audited = audit_mission(project_root, mission_id)
    if not audited.get("ok"):
        return "current-contract audit failed"
    return ""


def _read_json_if_exists(path: str) -> Any:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
