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


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            UPSTREAM_REQUESTS.append(json.loads(self.rfile.read(length)))
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
        print("PASS: MissionV1 HTTP boundary suite")
    finally:
        bridge_proc.terminate()
        bridge_proc.wait()
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
