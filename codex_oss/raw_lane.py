"""Fail-closed raw OSS probe accounting.

V2 separates smoke-level evidence from behavior-derived evidence:
- smoke: structured report + declared markers
- behavior: structured report + derived changed-file scope + real verification/rollback artifacts
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import hashlib
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
        smoke_scope_ok = bool(payload.get("scope_respected"))
        smoke_verification_ok = bool(payload.get("verification_recorded"))
        smoke_rollback_ok = bool(payload.get("rollback_recorded"))
        derived_scope_ok = bool(payload.get("derived_scope_respected"))
        derived_verification_ok = bool(payload.get("derived_verification_recorded"))
        derived_rollback_ok = bool(payload.get("derived_rollback_recorded"))
        smoke_ok = structured and smoke_scope_ok and smoke_verification_ok and smoke_rollback_ok
        behavior_ok = structured and derived_scope_ok and derived_verification_ok and derived_rollback_ok
        probes.append({
            "probe_id": probe_id,
            "structured_report_present": structured,
            "scope_respected": smoke_scope_ok,
            "verification_recorded": smoke_verification_ok,
            "rollback_recorded": smoke_rollback_ok,
            "derived_scope_respected": derived_scope_ok,
            "derived_verification_recorded": derived_verification_ok,
            "derived_rollback_recorded": derived_rollback_ok,
            "changed_files": list(payload.get("changed_files", []) or []),
            "unexpected_changed_files": list(payload.get("unexpected_changed_files", []) or []),
            "smoke_ok": smoke_ok,
            "behavior_ok": behavior_ok,
            "ok": behavior_ok,
        })

    all_smoke_ok = bool(probes) and all(probe["smoke_ok"] for probe in probes)
    all_behavior_ok = bool(probes) and all(probe["behavior_ok"] for probe in probes)
    return {
        "raw_probe_summary_version": "2.0",
        "project_root": project_root,
        "total_probes": len(probes),
        "passing_smoke_probes": sum(1 for probe in probes if probe["smoke_ok"]),
        "passing_behavior_probes": sum(1 for probe in probes if probe["behavior_ok"]),
        "smoke_claim_status": {
            "status": "SUPPORTED" if all_smoke_ok else "UNCONFIRMED",
            "basis": (
                f"passing_smoke_probes={sum(1 for probe in probes if probe['smoke_ok'])} total_probes={len(probes)}"
                if probes
                else "No raw probe artifacts are present."
            ),
        },
        "claim_status": {
            "status": "SUPPORTED" if all_behavior_ok else "UNCONFIRMED",
            "basis": (
                f"passing_behavior_probes={sum(1 for probe in probes if probe['behavior_ok'])} total_probes={len(probes)}"
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
    allowed_changed_paths: list[str] | None = None,
    verification_artifact_paths: list[str] | None = None,
    rollback_artifact_paths: list[str] | None = None,
) -> dict[str, Any]:
    probes_root = os.path.join(project_root, ".codex-oss", "raw-probes")
    probe_dir = os.path.join(probes_root, probe_id)
    os.makedirs(probe_dir, exist_ok=True)
    before = _snapshot_project(project_root)
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
    after = _snapshot_project(project_root)
    changed_files = sorted(_diff_snapshot(before, after))
    allowed = {path.strip().rstrip("/") for path in (allowed_changed_paths or []) if path.strip()}
    unexpected_changed_files = sorted(path for path in changed_files if allowed and path not in allowed)
    derived_scope_respected = bool(allowed) and bool(changed_files) and not unexpected_changed_files
    verification_artifacts = sorted(
        path for path in (verification_artifact_paths or []) if os.path.exists(os.path.join(project_root, path))
    )
    rollback_artifacts = sorted(
        path for path in (rollback_artifact_paths or []) if os.path.exists(os.path.join(project_root, path))
    )
    payload = {
        "raw_probe_version": "2.0",
        "probe_id": probe_id,
        "command": command,
        "exit_code": exit_code,
        "duration_seconds": round(time.time() - started, 3),
        "structured_report_present": structured_report_present,
        "scope_respected": bool(scope_respected),
        "verification_recorded": bool(verification_recorded),
        "rollback_recorded": bool(rollback_recorded),
        "allowed_changed_paths": sorted(allowed),
        "changed_files": changed_files,
        "unexpected_changed_files": unexpected_changed_files,
        "derived_scope_respected": derived_scope_respected,
        "verification_artifact_paths": list(verification_artifact_paths or []),
        "verification_artifacts_found": verification_artifacts,
        "derived_verification_recorded": bool(verification_artifact_paths) and len(verification_artifacts) == len(list(verification_artifact_paths or [])),
        "rollback_artifact_paths": list(rollback_artifact_paths or []),
        "rollback_artifacts_found": rollback_artifacts,
        "derived_rollback_recorded": bool(rollback_artifact_paths) and len(rollback_artifacts) == len(list(rollback_artifact_paths or [])),
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


def _snapshot_project(project_root: str) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(project_root):
        rel_dir = os.path.relpath(dirpath, project_root)
        if rel_dir == ".codex-oss" or rel_dir.startswith(".codex-oss" + os.sep):
            dirnames[:] = []
            continue
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, project_root)
            try:
                with open(full, "rb") as handle:
                    snapshot[rel] = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
    return snapshot


def _diff_snapshot(before: dict[str, str], after: dict[str, str]) -> set[str]:
    changed = set()
    for path, sha in after.items():
        if before.get(path) != sha:
            changed.add(path)
    for path in before:
        if path not in after:
            changed.add(path)
    return changed
