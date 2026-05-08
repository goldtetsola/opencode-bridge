"""Fail-closed raw OSS probe accounting."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any


def summarize_raw_probes(project_root: str) -> dict[str, Any]:
    probes_root = os.path.join(project_root, ".codex-oss", "raw-probes")
    if not os.path.isdir(probes_root):
        return {
            "raw_probe_summary_version": "1.0",
            "project_root": project_root,
            "total_probes": 0,
            "claim_status": {
                "status": "UNCONFIRMED",
                "basis": "No raw probe artifacts are present.",
            },
            "probes": [],
        }

    probes: list[dict[str, Any]] = []
    for probe_id in sorted(name for name in os.listdir(probes_root) if os.path.isdir(os.path.join(probes_root, name))):
        probe_dir = os.path.join(probes_root, probe_id)
        payload = _read_json_if_exists(os.path.join(probe_dir, "result.json")) or {}
        structured = bool(payload.get("structured_report_present"))
        scope_ok = bool(payload.get("scope_respected"))
        verification_ok = bool(payload.get("verification_recorded"))
        rollback_ok = bool(payload.get("rollback_recorded"))
        probes.append({
            "probe_id": probe_id,
            "structured_report_present": structured,
            "scope_respected": scope_ok,
            "verification_recorded": verification_ok,
            "rollback_recorded": rollback_ok,
            "ok": structured and scope_ok and verification_ok and rollback_ok,
        })

    all_ok = bool(probes) and all(probe["ok"] for probe in probes)
    return {
        "raw_probe_summary_version": "1.0",
        "project_root": project_root,
        "total_probes": len(probes),
        "passing_probes": sum(1 for probe in probes if probe["ok"]),
        "claim_status": {
            "status": "SUPPORTED" if all_ok else "UNCONFIRMED",
            "basis": (
                f"passing_probes={sum(1 for probe in probes if probe['ok'])} total_probes={len(probes)}"
                if probes
                else "No raw probe artifacts are present."
            ),
        },
        "probes": probes,
    }


def run_raw_probe(
    project_root: str,
    probe_id: str,
    command: list[str],
    scope_respected: bool = False,
    verification_recorded: bool = False,
    rollback_recorded: bool = False,
) -> dict[str, Any]:
    probes_root = os.path.join(project_root, ".codex-oss", "raw-probes")
    probe_dir = os.path.join(probes_root, probe_id)
    os.makedirs(probe_dir, exist_ok=True)
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = "timeout"
    except FileNotFoundError:
        exit_code = -1
        stdout = ""
        stderr = "command not found"

    structured_report_present = "OSS_REPORT_BEGIN" in stdout and "OSS_REPORT_END" in stdout
    payload = {
        "raw_probe_version": "1.0",
        "probe_id": probe_id,
        "command": command,
        "exit_code": exit_code,
        "duration_seconds": round(time.time() - started, 3),
        "structured_report_present": structured_report_present,
        "scope_respected": bool(scope_respected),
        "verification_recorded": bool(verification_recorded),
        "rollback_recorded": bool(rollback_recorded),
        "stdout_preview": stdout[:4000],
        "stderr_preview": stderr[:4000],
    }
    with open(os.path.join(probe_dir, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return payload


def _read_json_if_exists(path: str) -> Any:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
