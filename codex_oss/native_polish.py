"""Native-polish smoke checks for OSS subagent product readiness."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

JSON = dict[str, Any]


def run_native_polish_smoke(
    project_root: str,
    *,
    base_url: str = "http://127.0.0.1:4000/v1",
    auth: str = "sk-local-codex-bridge",
    timeout: float = 120,
) -> JSON:
    """Run a bounded live smoke for the user-facing OSS subagent surface.

    This is deliberately public-repo-safe: it creates a temporary scratch target,
    proves raw direct writes fail closed, then proves the runtime implementation
    lane can validate, apply, and verify a patch in isolation without mutating
    the main workspace.
    """
    root = Path(project_root)
    base_url = base_url.rstrip("/")
    checks: list[JSON] = []

    checks.append(_check_implementer_installed(root))
    if checks[-1]["status"] != "PASS":
        return _summary(root, checks)

    _check_bridge_health(base_url, auth, timeout=10, checks=checks)
    if checks[-1]["status"] != "PASS":
        return _summary(root, checks)

    scratch = root / "tests" / "codex_oss_native_polish_smoke_test.py"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    original = 'VALUE = "native"\n\n\ndef describe():\n    return VALUE\n'
    scratch.write_text(original, encoding="utf-8")

    try:
        _check_raw_write_demotes(base_url, auth, timeout, checks)
        _check_a5_implementation_runtime(root, scratch, original, base_url, auth, timeout, checks)
    finally:
        try:
            scratch.unlink()
        except FileNotFoundError:
            pass

    return _summary(root, checks)


def _check_implementer_installed(root: Path) -> JSON:
    path = root / ".codex" / "agents" / "oss-deepseek-implementer.toml"
    if not path.exists():
        return {
            "name": "implementer_agent_installed",
            "status": "FAIL",
            "message": "oss_deepseek_implementer is not installed in .codex/agents",
            "fix": "Run `codex-oss install --force`, then restart/reload Codex so the subagent registry refreshes.",
        }
    text = path.read_text(encoding="utf-8")
    required = [
        'model_provider = "opencode_bridge"',
        'model = "mission-a5-deepseek"',
        "MissionV1 A4/A5",
        "runtime owns patch construction",
    ]
    missing = [needle for needle in required if needle not in text]
    if missing:
        return {
            "name": "implementer_agent_installed",
            "status": "FAIL",
            "message": "oss_deepseek_implementer is installed but stale",
            "details": {"missing": missing},
            "fix": "Run `codex-oss install --force`, then restart/reload Codex so the subagent registry refreshes.",
        }
    return {
        "name": "implementer_agent_installed",
        "status": "PASS",
        "message": "oss_deepseek_implementer is installed and points at mission-a5-deepseek",
    }


def _check_bridge_health(base_url: str, auth: str, *, timeout: float, checks: list[JSON]) -> None:
    health_url = base_url.removesuffix("/v1") + "/health"
    try:
        req = urllib.request.Request(health_url, headers={"Authorization": f"Bearer {auth}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        checks.append({
            "name": "bridge_running",
            "status": "FAIL",
            "message": f"Bridge is not reachable at {health_url}: {exc}",
            "fix": "Run `codex-oss up --daemon` and retry.",
        })
        return
    if health.get("ok"):
        checks.append({
            "name": "bridge_running",
            "status": "PASS",
            "message": "Bridge is running",
            "details": {
                "version": health.get("version"),
                "project_root": health.get("project_root") or health.get("cwd"),
            },
        })
    else:
        checks.append({
            "name": "bridge_running",
            "status": "FAIL",
            "message": "Bridge health endpoint did not report ok=true",
            "details": health,
        })


def _check_raw_write_demotes(base_url: str, auth: str, timeout: float, checks: list[JSON]) -> None:
    body = {
        "model": "oss_deepseek_pro",
        "stream": False,
        "input": [{
            "role": "user",
            "content": (
                "OSS_HANDOFF_JSON:\n"
                "{\"schema_version\":1,\"role\":\"Bounded writer smoke\","
                "\"goal\":\"Append a native polish marker\","
                "\"task_type\":\"bounded_write\","
                "\"owned_paths\":[\"tests/codex_oss_native_polish_smoke_test.py\"],"
                "\"read_only_paths\":[\"tests/codex_oss_native_polish_smoke_test.py\"],"
                "\"forbidden_actions\":[\"Do not edit other files\"],"
                "\"verification_steps\":[\"Read back tests/codex_oss_native_polish_smoke_test.py\"],"
                "\"deliverable_fields\":[\"file changed\",\"verification\",\"confidence\",\"caveats\"],"
                "\"completion_rule\":\"stop after verification\","
                "\"escalation_rule\":\"stop on scope drift\"}\n\n"
                "Append SHOULD_NOT_APPEAR to tests/codex_oss_native_polish_smoke_test.py."
            ),
        }],
        "tools": [{
            "type": "function",
            "name": "exec_command",
            "description": "Run a shell command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }],
    }
    try:
        payload = _post_response(base_url, auth, body, timeout)
        text = _extract_response_text(payload)
    except Exception as exc:
        checks.append({
            "name": "raw_write_demotes",
            "status": "FAIL",
            "message": f"Raw write demotion request failed: {exc}",
        })
        return
    ok = "DETERMINISTIC_LEGACY_WRITE_DEMOTED" in text and "MissionV1 A4/A5/A6" in text
    checks.append({
        "name": "raw_write_demotes",
        "status": "PASS" if ok else "FAIL",
        "message": "Raw direct write was demoted before model prose could become terminal" if ok else "Raw direct write did not demote cleanly",
        "details": {"excerpt": text[:500]},
    })


def _check_a5_implementation_runtime(
    root: Path,
    scratch: Path,
    original: str,
    base_url: str,
    auth: str,
    timeout: float,
    checks: list[JSON],
) -> None:
    rel = scratch.relative_to(root).as_posix()
    mission_id = f"mission_native_polish_smoke_{int(time.time())}"
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": "A5",
        "mode": "bounded_implementation",
        "objective": f"Add a tiny test-style helper to {rel} and verify syntax without mutating the main workspace.",
        "risk_tier": "low",
        "write_allowed": True,
        "allowed_roots": [],
        "allowed_paths": [rel],
        "owned_paths": [rel],
        "read_only_paths": [rel],
        "forbidden_roots": [".env", ".git"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": 60,
        "stop_conditions": ["valid_patch", "deadline_reached"],
        "report_schema": "implementation_report.v1",
        "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
        "max_files_changed": 1,
        "max_patch_bytes": 12000,
        "apply_mode": "isolated_worktree",
        "verification_policy": {
            "allowed_commands": [["python3", rel]],
            "max_commands": 1,
            "timeout_seconds": 20,
        },
        "objective_spec": {
            "schema_version": "objective_spec.v1",
            "objective_type": "implementation_test_only",
            "target": {
                "test_file": rel,
                "required_changed_files": [rel],
                "source_files": [],
                "required_test_names": ["test_native_polish_smoke"],
            },
            "required_outputs": ["changed_test_file"],
            "required_evidence_shapes": ["test_definition"],
            "completion_criteria": ["required_test_present"],
        },
    }
    intent = {
        "patch_intent_version": "1.0",
        "status": "PROPOSED",
        "summary": "Add a tiny isolated native-polish smoke helper.",
        "edits": [{
            "operation": "insert_after",
            "path": rel,
            "anchor": "def describe():\n    return VALUE",
            "content": "\n\n\ndef test_native_polish_smoke():\n    assert describe() == \"native\"\n",
            "reason": "Exercise runtime-owned diff construction, isolated apply, and verification.",
        }],
        "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "scratch-only"},
        "verification_plan": [{"command": ["python3", rel], "reason": "Run the changed scratch file as a syntax smoke."}],
        "evidence_refs": [f"file:{rel}#extract:describe"],
        "caveats": ["Scratch-only patch intent."],
    }
    body = {
        "model": "mission-a5-deepseek",
        "stream": False,
        "input": [{
            "role": "user",
            "content": (
                "<OSS_HANDOFF_JSON>\n"
                + json.dumps(mission)
                + "\n</OSS_HANDOFF_JSON>\n\n<OSS_PATCH_INTENT_JSON>\n"
                + json.dumps(intent)
                + "\n</OSS_PATCH_INTENT_JSON>"
            ),
        }],
    }
    try:
        payload = _post_response(base_url, auth, body, timeout)
        text = _extract_response_text(payload)
    except Exception as exc:
        checks.append({
            "name": "a5_isolated_implementation",
            "status": "FAIL",
            "message": f"A5 runtime implementation request failed: {exc}",
        })
        return

    artifact_dir = root / ".codex-oss" / "missions" / mission_id
    report = _read_json(artifact_dir / "report.json")
    verification = _read_json(artifact_dir / "verification.json")
    visible = artifact_dir / "visible_commentary.jsonl"
    summary = artifact_dir / "summary.md"
    main_unchanged = scratch.read_text(encoding="utf-8") == original
    verified = (
        payload.get("status") == "completed"
        and "Status: VERIFIED" in text
        and isinstance(report, dict)
        and report.get("status") == "VERIFIED"
        and report.get("main_workspace_mutated") is False
        and report.get("runtime_built_diff") is True
        and isinstance(verification, list)
        and bool(verification)
        and verification[0].get("exit_code") == 0
        and visible.exists()
        and summary.exists()
        and main_unchanged
    )
    checks.append({
        "name": "a5_isolated_implementation",
        "status": "PASS" if verified else "FAIL",
        "message": "A5 runtime built, applied, verified, and reported an isolated patch" if verified else "A5 runtime implementation did not satisfy the native-polish contract",
        "details": {
            "mission_id": mission_id,
            "status": report.get("status") if isinstance(report, dict) else "missing_report",
            "main_workspace_unchanged": main_unchanged,
            "verification_exit": verification[0].get("exit_code") if isinstance(verification, list) and verification else None,
            "visible_commentary_path": str(visible.relative_to(root)) if visible.exists() else "",
            "summary_path": str(summary.relative_to(root)) if summary.exists() else "",
        },
    })


def _post_response(base_url: str, auth: str, body: JSON, timeout: float) -> JSON:
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {auth}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_response_text(payload: JSON) -> str:
    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _summary(root: Path, checks: list[JSON]) -> JSON:
    failed = [check for check in checks if check.get("status") != "PASS"]
    return {
        "native_polish_smoke_version": "1.0",
        "project_root": str(root),
        "ok": not failed,
        "passed": sum(1 for check in checks if check.get("status") == "PASS"),
        "failed": len(failed),
        "checks": checks,
    }
