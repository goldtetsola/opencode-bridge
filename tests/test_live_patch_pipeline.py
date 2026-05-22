#!/usr/bin/env python3
"""Live A4/A5 patch-mediated implementation smoke.

Skipped by default. Run with LIVE_A4A5_MODE=1 and OPENCODE_GO_API_KEY set.
The test starts a local bridge, asks a runtime-backed OSS model for a tiny
fixture-only patch, and verifies A5 applies only in isolation.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

BRIDGE_PORT = 4016
AUTH = "sk-local-live-a4a5"
ROOT = os.path.dirname(os.path.dirname(__file__))
HTTP_TARGET = "tests/fixtures/a4_http_target.py"
HTTP_TARGET_ORIGINAL = 'VALUE = "original"\n\n\ndef describe():\n    return VALUE\n'
LOG_DIR = os.path.join(ROOT, "tmp", "live-a4a5")


def wait_for_bridge():
    for _ in range(40):
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
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def implementation_mission(tier: str, mode: str, write_allowed: bool) -> dict:
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": f"mission_live_{tier.lower()}_patch",
        "tier": tier,
        "mode": mode,
        "objective": (
            "Patch tests/fixtures/a4_http_target.py only. Add a small function named "
            "live_runtime_patch_marker that returns the string 'live'. Do not modify "
            "existing behavior. Prefer DesiredStateV1 with a python_function_exists "
            "assertion for live_runtime_patch_marker and return_value='live'."
        ),
        "risk_tier": "low",
        "write_allowed": write_allowed,
        "allowed_roots": [],
        "allowed_paths": [HTTP_TARGET],
        "owned_paths": [HTTP_TARGET],
        "read_only_paths": [HTTP_TARGET],
        "forbidden_roots": [".env", ".git", ".codex-oss/"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": 120,
        "stop_conditions": ["valid_patch", "deadline_reached"],
        "report_schema": "patch_validation_report.v1" if tier == "A4" else "implementation_report.v1",
        "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
        "max_files_changed": 1,
        "max_patch_bytes": 12000,
        "verification_policy": {
            "allowed_commands": [["python3", HTTP_TARGET]],
            "max_commands": 1,
            "timeout_seconds": 20,
        },
    }
    if tier == "A5":
        mission["apply_mode"] = "isolated_worktree"
    return mission


def response_text(response: dict) -> str:
    try:
        return response["output"][0]["content"][0]["text"]
    except Exception:
        return json.dumps(response, indent=2)[:4000]


def assert_fixture_unchanged():
    with open(os.path.join(ROOT, HTTP_TARGET), encoding="utf-8") as handle:
        assert handle.read() == HTTP_TARGET_ORIGINAL


def run_case(model: str, mission: dict) -> str:
    response = call_bridge({
        "model": model,
        "stream": False,
        "input": [{
            "role": "user",
            "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
        }],
    })
    assert response.get("status") == "completed", response
    return response_text(response)


def main():
    if os.getenv("LIVE_A4A5_MODE") != "1":
        print("SKIP: set LIVE_A4A5_MODE=1 and OPENCODE_GO_API_KEY to run live A4/A5 patch smoke")
        return
    if not os.getenv("OPENCODE_GO_API_KEY") and not os.getenv("UPSTREAM_API_KEY"):
        print("SKIP: OPENCODE_GO_API_KEY/UPSTREAM_API_KEY is not set")
        return

    assert_fixture_unchanged()
    env = {
        **os.environ,
        "LITELLM_MASTER_KEY": AUTH,
        "PROXY_PORT": str(BRIDGE_PORT),
        "GPT_MODEL_STRATEGY": "oss",
        "CONTINUATION_TOOLS": "none",
        "REQUEST_DEADLINE_SECONDS": os.getenv("LIVE_A4A5_DEADLINE_SECONDS", "120"),
        "UPSTREAM_TIMEOUT_SECONDS": os.getenv("LIVE_A4A5_UPSTREAM_TIMEOUT_SECONDS", "120"),
    }
    os.makedirs(LOG_DIR, exist_ok=True)
    stdout_path = os.path.join(LOG_DIR, "bridge.out.log")
    stderr_path = os.path.join(LOG_DIR, "bridge.err.log")
    stdout_handle = open(stdout_path, "w", encoding="utf-8")
    stderr_handle = open(stderr_path, "w", encoding="utf-8")
    bridge_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bridge.py")],
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=True,
    )
    try:
        wait_for_bridge()
        a4_model = os.getenv("LIVE_A4A5_A4_MODEL", "mission-a4-kimi")
        a5_model = os.getenv("LIVE_A4A5_A5_MODEL", "mission-a5-kimi")
        a4_text = run_case(a4_model, implementation_mission("A4", "patch_proposal", False))
        assert "OSS_PATCH_VALIDATION_BEGIN" in a4_text, a4_text[:4000]
        assert "Status: VALID" in a4_text, a4_text[:4000]
        assert_fixture_unchanged()
        print(f"  PASS: live A4 {a4_model} produced a validated patch proposal")

        a5_text = run_case(a5_model, implementation_mission("A5", "bounded_implementation", True))
        assert "OSS_IMPLEMENTATION_REPORT_BEGIN" in a5_text, a5_text[:4000]
        assert "Status: VERIFIED" in a5_text, a5_text[:4000]
        assert "Main workspace mutated: false" in a5_text, a5_text[:4000]
        assert_fixture_unchanged()
        print(f"  PASS: live A5 {a5_model} verified patch in isolation only")
        print("PASS: live A4/A5 patch-mediated implementation smoke")
    finally:
        bridge_proc.terminate()
        try:
            bridge_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(bridge_proc.pid, signal.SIGKILL)
            bridge_proc.wait(timeout=10)
        stdout_handle.close()
        stderr_handle.close()


if __name__ == "__main__":
    main()
