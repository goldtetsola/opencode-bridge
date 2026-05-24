#!/usr/bin/env python3
"""MissionV1 HTTP boundary test for the managed OSS Agent Runtime."""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

BRIDGE_PORT = 4006
FAKE_UPSTREAM_PORT = 9006
AUTH = "sk-local-codex-bridge"
UPSTREAM_REQUESTS = []
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
HTTP_TARGET = "tests/fixtures/a4_http_target.py"
HTTP_TARGET_ORIGINAL = 'VALUE = "original"\n\n\ndef describe():\n    return VALUE\n'


def fake_chat_response(content: str) -> bytes:
    return json.dumps({
        "id": "chatcmpl-mission-v1",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "kimi-k2.6",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }).encode()


def http_target_patch_intent() -> str:
    return json.dumps({
        "patch_intent_version": "1.0",
        "status": "PROPOSED",
        "summary": "Add a small isolated helper to the HTTP fixture.",
        "edits": [
            {
                "operation": "insert_after",
                "path": HTTP_TARGET,
                "anchor": "def describe():\n    return VALUE",
                "content": "\n\n\ndef added_by_runtime_patch():\n    return \"isolated\"\n",
                "reason": "Exercise runtime-owned diff construction through the HTTP bridge.",
            }
        ],
        "risk_assessment": {
            "risk_tier": "low",
            "critical_paths_touched": False,
            "blast_radius": "fixture-only",
        },
        "verification_plan": [{
            "command": ["python3", HTTP_TARGET],
            "reason": "Run the changed fixture as a syntax smoke.",
        }],
        "evidence_refs": [f"file:{HTTP_TARGET}#extract:describe"],
        "caveats": ["Fixture-only patch intent."],
    })


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = {}
        if length:
            request = json.loads(self.rfile.read(length))
            UPSTREAM_REQUESTS.append(request)
        serialized = json.dumps(request)
        if "PatchIntentV1" in serialized:
            content = http_target_patch_intent()
        else:
            content = (
                '{"action_type":"final_report","report":'
                '{"oss_report_version":"1.0","mission_id":"mission_http_test",'
                '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                '"commands_run":[],"findings":[],"uncertainties":[],'
                '"caveats":["http boundary smoke"],'
                '"escalation_recommendation":"GPT-5.5 review required",'
                '"missing_fields":[]}}'
            )
        data = fake_chat_response(content)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_fake_upstream():
    server = ReusableTCPServer(("127.0.0.1", FAKE_UPSTREAM_PORT), FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def wait_for_bridge():
    for _ in range(20):
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_mission_v1_returns_terminal_report():
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": "mission_http_test",
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": "Return a valid report through the HTTP bridge.",
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": ["codex_oss/"],
        "allowed_paths": [],
        "allow_broad_read_scope": True,
        "tool_budget": 3,
        "time_budget_seconds": 30,
        "allowed_tool_classes": ["read"],
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": [
            "files_inspected",
            "commands_run",
            "findings",
            "uncertainties",
            "confidence",
            "caveats",
            "escalation_recommendation",
        ],
    }
    body = {
        "model": "ocg-kimi-k2.6",
        "stream": False,
        "input": [
            {
                "role": "user",
                "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
            }
        ],
    }
    response = call_bridge(body)
    assert response.get("status") == "completed", response
    text = response["output"][0]["content"][0]["text"]
    assert "OSS_REPORT_BEGIN" in text, text
    assert "Status: PARTIAL" in text, text
    assert "http boundary smoke" in text, text
    print("  PASS: MissionV1 HTTP boundary returns terminal report")


def test_runtime_alias_maps_model_and_disables_provider_tools():
    UPSTREAM_REQUESTS.clear()
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": "mission_http_test",
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": "Return a valid report through the runtime alias.",
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": ["codex_oss/"],
        "allowed_paths": [],
        "allow_broad_read_scope": True,
        "tool_budget": 3,
        "time_budget_seconds": 30,
        "allowed_tool_classes": ["read"],
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": [
            "files_inspected",
            "commands_run",
            "findings",
            "uncertainties",
            "confidence",
            "caveats",
            "escalation_recommendation",
        ],
    }
    body = {
        "model": "mission-a3-kimi",
        "stream": False,
        "tools": [{"type": "function", "name": "should_not_reach_upstream"}],
        "input": [
            {
                "role": "user",
                "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
            }
        ],
    }
    response = call_bridge(body)
    assert response.get("status") == "completed", response
    text = response["output"][0]["content"][0]["text"]
    assert "OSS_REPORT_BEGIN" in text, text
    assert "Status: PARTIAL" in text, text
    assert UPSTREAM_REQUESTS, "runtime alias did not call fake upstream"
    assert UPSTREAM_REQUESTS[-1]["model"] == "kimi-k2.6", UPSTREAM_REQUESTS[-1]
    assert UPSTREAM_REQUESTS[-1]["tools"] == [], UPSTREAM_REQUESTS[-1]
    print("  PASS: runtime alias maps to reasoning model and disables provider tools")


def test_runtime_alias_without_mission_fails_closed():
    UPSTREAM_REQUESTS.clear()
    body = {
        "model": "mission-a3-kimi",
        "stream": False,
        "input": [{"role": "user", "content": "Investigate README.md"}],
    }
    response = call_bridge(body)
    assert response.get("status") == "completed", response
    text = response["output"][0]["content"][0]["text"]
    assert "Status: FAILED" in text, text
    assert "requires exactly one OSS_HANDOFF_JSON MissionV1" in text, text
    assert UPSTREAM_REQUESTS == [], UPSTREAM_REQUESTS
    print("  PASS: runtime alias without MissionV1 fails closed")


def test_cli_mission_run_uses_runtime_bridge():
    UPSTREAM_REQUESTS.clear()
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": "mission_http_test",
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": "Return a valid report through CLI delegation.",
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": ["codex_oss/"],
        "allowed_paths": [],
        "allow_broad_read_scope": True,
        "tool_budget": 3,
        "time_budget_seconds": 30,
        "allowed_tool_classes": ["read"],
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": [
            "files_inspected",
            "commands_run",
            "findings",
            "uncertainties",
            "confidence",
            "caveats",
            "escalation_recommendation",
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(mission, handle)
        mission_path = handle.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "bin", "codex-oss"),
                "mission",
                "run",
                mission_path,
                "--model",
                "mission-a3-kimi",
                "--port",
                str(BRIDGE_PORT),
            ],
            env={**os.environ, "LITELLM_MASTER_KEY": AUTH},
            text=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        os.unlink(mission_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OSS_REPORT_BEGIN" in result.stdout, result.stdout
    assert "Status: PARTIAL" in result.stdout, result.stdout
    assert UPSTREAM_REQUESTS, "CLI mission did not reach fake upstream through bridge"
    assert UPSTREAM_REQUESTS[-1]["model"] == "kimi-k2.6", UPSTREAM_REQUESTS[-1]
    assert UPSTREAM_REQUESTS[-1]["tools"] == [], UPSTREAM_REQUESTS[-1]
    print("  PASS: CLI mission run uses runtime bridge alias end-to-end")


def test_cli_mission_handoff_preserves_sibling_runtime_blocks():
    from codex_oss.cli import _mission_handoff_from_text

    mission = implementation_mission("A4", "patch_proposal", False)
    text = (
        "<OSS_HANDOFF_JSON>\n"
        + json.dumps(mission)
        + "\n</OSS_HANDOFF_JSON>\n"
        + "<OSS_PATCH_INTENT_JSON>\n"
        + http_target_patch_intent()
        + "\n</OSS_PATCH_INTENT_JSON>\n"
    )

    handoff_text, parsed = _mission_handoff_from_text(text)
    assert parsed["mission_id"] == "mission_http_a4", parsed
    assert handoff_text.count("<OSS_HANDOFF_JSON>") == 1, handoff_text
    assert "<OSS_PATCH_INTENT_JSON>" in handoff_text, handoff_text
    assert "added_by_runtime_patch" in handoff_text, handoff_text
    print("  PASS: CLI mission handoff preserves sibling runtime blocks")


def implementation_mission(tier: str, mode: str, write_allowed: bool) -> dict:
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": f"mission_http_{tier.lower()}",
        "tier": tier,
        "mode": mode,
        "objective": "Exercise patch-mediated implementation through the HTTP runtime bridge.",
        "risk_tier": "low",
        "write_allowed": write_allowed,
        "allowed_roots": [],
        "allowed_paths": [HTTP_TARGET],
        "owned_paths": [HTTP_TARGET],
        "read_only_paths": [HTTP_TARGET],
        "forbidden_roots": [".env", ".git", ".codex-oss/"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": 30,
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


def test_a4_runtime_alias_returns_patch_validation_report():
    UPSTREAM_REQUESTS.clear()
    with open(os.path.join(ROOT, HTTP_TARGET), encoding="utf-8") as handle:
        assert handle.read() == HTTP_TARGET_ORIGINAL
    mission = implementation_mission("A4", "patch_proposal", False)
    body = {
        "model": "mission-a4-kimi",
        "stream": False,
        "tools": [{"type": "function", "name": "should_not_reach_upstream"}],
        "input": [
            {
                "role": "user",
                "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
            }
        ],
    }
    response = call_bridge(body)
    assert response.get("status") == "completed", response
    text = response["output"][0]["content"][0]["text"]
    assert "OSS_PATCH_VALIDATION_BEGIN" in text, text
    assert "Status: VALID" in text, text
    assert HTTP_TARGET in text, text
    assert UPSTREAM_REQUESTS, "A4 runtime alias did not call fake upstream"
    assert UPSTREAM_REQUESTS[-1]["model"] == "kimi-k2.6", UPSTREAM_REQUESTS[-1]
    assert UPSTREAM_REQUESTS[-1]["tools"] == [], UPSTREAM_REQUESTS[-1]
    with open(os.path.join(ROOT, HTTP_TARGET), encoding="utf-8") as handle:
        assert handle.read() == HTTP_TARGET_ORIGINAL
    print("  PASS: A4 runtime alias returns validated patch report without workspace mutation")


def test_a5_runtime_alias_applies_patch_in_isolation_only():
    UPSTREAM_REQUESTS.clear()
    with open(os.path.join(ROOT, HTTP_TARGET), encoding="utf-8") as handle:
        assert handle.read() == HTTP_TARGET_ORIGINAL
    mission = implementation_mission("A5", "bounded_implementation", True)
    body = {
        "model": "mission-a5-kimi",
        "stream": False,
        "input": [
            {
                "role": "user",
                "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
            }
        ],
    }
    response = call_bridge(body)
    assert response.get("status") == "completed", response
    text = response["output"][0]["content"][0]["text"]
    assert "OSS_IMPLEMENTATION_REPORT_BEGIN" in text, text
    assert "Status: VERIFIED" in text, text
    assert "Main workspace mutated: false" in text, text
    assert UPSTREAM_REQUESTS, "A5 runtime alias did not call fake upstream"
    assert UPSTREAM_REQUESTS[-1]["model"] == "kimi-k2.6", UPSTREAM_REQUESTS[-1]
    assert UPSTREAM_REQUESTS[-1]["tools"] == [], UPSTREAM_REQUESTS[-1]
    with open(os.path.join(ROOT, HTTP_TARGET), encoding="utf-8") as handle:
        assert handle.read() == HTTP_TARGET_ORIGINAL
    print("  PASS: A5 runtime alias applies in isolation and leaves main workspace unchanged")


def main():
    print("MissionV1 HTTP boundary test")
    print("============================")
    os.environ["OPENCODE_GO_API_KEY"] = "sk-test"
    os.environ["LITELLM_MASTER_KEY"] = AUTH
    os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"
    os.environ["PROXY_PORT"] = str(BRIDGE_PORT)
    os.environ["UPSTREAM_BASE"] = f"http://127.0.0.1:{FAKE_UPSTREAM_PORT}/v1"
    os.environ["GPT_MODEL_STRATEGY"] = "oss"
    os.environ["CONTINUATION_TOOLS"] = "none"
    os.environ["REQUEST_DEADLINE_SECONDS"] = "30"

    upstream = start_fake_upstream()
    bridge_proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bridge.py")],
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_bridge()
        test_mission_v1_returns_terminal_report()
        test_runtime_alias_maps_model_and_disables_provider_tools()
        test_runtime_alias_without_mission_fails_closed()
        test_cli_mission_run_uses_runtime_bridge()
        test_cli_mission_handoff_preserves_sibling_runtime_blocks()
        test_a4_runtime_alias_returns_patch_validation_report()
        test_a5_runtime_alias_applies_patch_in_isolation_only()
        print("PASS: MissionV1 HTTP boundary suite")
    finally:
        bridge_proc.terminate()
        bridge_proc.wait()
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
