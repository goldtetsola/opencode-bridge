#!/usr/bin/env python3
"""Live A5 workspace-apply pilot in a temporary project.

Skipped by default. This intentionally mutates only a temporary project root;
the repository containing this test remains unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = os.path.dirname(os.path.dirname(__file__))
AUTH = "sk-local-live-workspace-pilot"
BRIDGE_PORT = int(os.getenv("LIVE_A5_WORKSPACE_PORT", "4120"))


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_project() -> str:
    root = tempfile.mkdtemp(prefix="oss_live_workspace_")
    write(os.path.join(root, "tests/test_workspace_live.py"), "def test_existing_workspace_live():\n    assert True\n")
    return root


def wait_for_bridge() -> None:
    for _ in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/health")
            req.add_header("Authorization", f"Bearer {AUTH}")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if json.loads(resp.read()).get("ok"):
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Bridge failed to start")


def call_bridge(body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {AUTH}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=int(os.getenv("LIVE_A5_WORKSPACE_CLIENT_TIMEOUT", "240"))) as resp:
        return json.loads(resp.read())


def response_text(response: dict) -> str:
    try:
        return response["output"][0]["content"][0]["text"]
    except Exception:
        return json.dumps(response, indent=2)[:4000]


def mission(mission_id: str, verification_command: list[str]) -> dict:
    target = "tests/test_workspace_live.py"
    return {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": "A5",
        "mode": "bounded_implementation",
        "objective": (
            "Patch tests/test_workspace_live.py only. Prefer DesiredStateV1, not a raw diff. "
            "Use assertion type python_function_exists with path tests/test_workspace_live.py, "
            "function_name test_workspace_apply_marker, and body containing assert True. "
            "The runtime should apply to the workspace under explicit workspace_apply_policy."
        ),
        "risk_tier": "low",
        "write_allowed": True,
        "allowed_roots": [],
        "allowed_paths": [target],
        "owned_paths": [target],
        "read_only_paths": [target],
        "forbidden_roots": [".env", ".git", ".codex-oss/"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": int(os.getenv("LIVE_A5_WORKSPACE_MISSION_SECONDS", "120")),
        "stop_conditions": ["valid_patch", "deadline_reached"],
        "report_schema": "implementation_report.v1",
        "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
        "max_files_changed": 1,
        "max_patch_bytes": 12000,
        "apply_mode": "workspace",
        "workspace_apply_policy": {"allow_direct_workspace_apply": True},
        "verification_policy": {
            "allowed_commands": [verification_command],
            "max_commands": 1,
            "timeout_seconds": 20,
        },
        "objective_spec": {
            "schema_version": "objective_spec.v1",
            "objective_type": "implementation_test_only",
            "target": {
                "test_file": target,
                "source_files": [],
                "required_test_names": ["test_workspace_apply_marker"],
            },
            "required_outputs": ["changed_test_file"],
            "required_evidence_shapes": ["test_definition"],
            "completion_criteria": ["required_test_present", "workspace_apply_policy"],
        },
    }


def run_case(model: str, mission_payload: dict) -> str:
    response = call_bridge({
        "model": model,
        "stream": False,
        "input": [{
            "role": "user",
            "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission_payload) + "\n</OSS_HANDOFF_JSON>",
        }],
    })
    assert response.get("status") == "completed", response
    return response_text(response)


def main() -> None:
    if os.getenv("LIVE_A5_WORKSPACE_PILOT") != "1":
        print("SKIP: set LIVE_A5_WORKSPACE_PILOT=1 and OPENCODE_GO_API_KEY to run live workspace pilot")
        return
    if not os.getenv("OPENCODE_GO_API_KEY") and not os.getenv("UPSTREAM_API_KEY"):
        print("SKIP: OPENCODE_GO_API_KEY/UPSTREAM_API_KEY is not set")
        return

    project_root = make_project()
    log_dir = os.path.join(ROOT, "tmp", "live-a5-workspace-pilot")
    os.makedirs(log_dir, exist_ok=True)
    stdout_handle = open(os.path.join(log_dir, "bridge.out.log"), "w", encoding="utf-8")
    stderr_handle = open(os.path.join(log_dir, "bridge.err.log"), "w", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "LITELLM_MASTER_KEY": AUTH,
        "PROXY_PORT": str(BRIDGE_PORT),
        "GPT_MODEL_STRATEGY": "oss",
        "CONTINUATION_TOOLS": "none",
        "REQUEST_DEADLINE_SECONDS": os.getenv("LIVE_A5_WORKSPACE_DEADLINE_SECONDS", "180"),
        "UPSTREAM_TIMEOUT_SECONDS": os.getenv("LIVE_A5_WORKSPACE_UPSTREAM_TIMEOUT_SECONDS", "180"),
    }
    bridge_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bridge.py")],
        cwd=project_root,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=True,
    )
    try:
        wait_for_bridge()
        model = os.getenv("LIVE_A5_WORKSPACE_MODEL", "mission-a5-kimi")
        target_path = os.path.join(project_root, "tests/test_workspace_live.py")
        original = open(target_path, encoding="utf-8").read()

        verified = run_case(model, mission("mission_live_workspace_verified", ["python3", "-m", "py_compile", "tests/test_workspace_live.py"]))
        assert "Status: VERIFIED" in verified, verified[:4000]
        assert "Main workspace mutated: true" in verified, verified[:4000]
        assert "test_workspace_apply_marker" in open(target_path, encoding="utf-8").read()
        print("  PASS: workspace apply verified in temporary project")

        write(target_path, original)
        rolled_back = run_case(model, mission("mission_live_workspace_rollback", ["python3", "-c", "raise SystemExit(1)"]))
        assert "Status: VERIFICATION_FAILED" in rolled_back, rolled_back[:4000]
        assert "Main workspace mutated: false" in rolled_back, rolled_back[:4000]
        assert open(target_path, encoding="utf-8").read() == original
        print("  PASS: workspace apply rolls back after failed verification")
        print("PASS: live A5 workspace apply pilot")
    finally:
        bridge_proc.terminate()
        try:
            bridge_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(bridge_proc.pid, signal.SIGKILL)
            bridge_proc.wait(timeout=10)
        stdout_handle.close()
        stderr_handle.close()
        shutil.rmtree(project_root)


if __name__ == "__main__":
    main()
