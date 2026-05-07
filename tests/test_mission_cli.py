#!/usr/bin/env python3
"""CLI tests for explicit MissionV1 runtime delegation."""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "codex-oss"
AUTH = "sk-local-codex-bridge"
REQUESTS = []


class FakeBridge(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        REQUESTS.append(body)
        text = (
            "OSS_REPORT_BEGIN\n"
            "Status: COMPLETE\n"
            "Confidence: LOW\n"
            "Findings:\n"
            "- CLI delegation smoke passed.\n"
            "OSS_REPORT_END"
        )
        data = json.dumps({
            "id": "resp_cli_test",
            "object": "response",
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
        }).encode("utf-8")
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


def run_cmd(args, **kwargs):
    env = os.environ.copy()
    env.setdefault("LITELLM_MASTER_KEY", AUTH)
    return subprocess.run(
        [sys.executable, str(BIN)] + args,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        **kwargs,
    )


def assert_template_requires_scope():
    result = run_cmd(["mission", "template", "--objective", "Inspect code"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "requires at least one --allowed-root or --allowed-path" in result.stderr


def assert_template_outputs_mission_v1():
    result = run_cmd([
        "mission", "template",
        "--mission-id", "mission_cli_template",
        "--objective", "Inspect README",
        "--allowed-path", "README.md",
        "--tool-budget", "4",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    mission = json.loads(result.stdout)
    assert mission["schema_version"] == "oss_agent_mission.v1"
    assert mission["mission_id"] == "mission_cli_template"
    assert mission["allowed_paths"] == ["README.md"]
    assert mission["write_allowed"] is False
    assert mission["mode"] == "managed_investigation"

    a2 = run_cmd([
        "mission", "template",
        "--mission-id", "mission_cli_template_a2",
        "--objective", "Inspect README",
        "--tier", "A2",
        "--allowed-path", "README.md",
    ])
    assert a2.returncode == 0, a2.stdout + a2.stderr
    a2_mission = json.loads(a2.stdout)
    assert a2_mission["mode"] == "guided_exploration", a2_mission


def assert_run_posts_mission_to_runtime_bridge():
    REQUESTS.clear()
    server = ReusableTCPServer(("127.0.0.1", 4017), FakeBridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mission = {
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "mission_cli_run",
            "tier": "A3",
            "mode": "managed_investigation",
            "objective": "CLI smoke",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": ["README.md"],
            "tool_budget": 2,
            "time_budget_seconds": 30,
            "allowed_tool_classes": ["read"],
            "stop_conditions": ["valid_report", "deadline_reached"],
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
            result = run_cmd([
                "mission", "run", mission_path,
                "--model", "mission-a3-kimi",
                "--port", "4017",
            ])
        finally:
            os.unlink(mission_path)
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OSS_REPORT_BEGIN" in result.stdout
    assert REQUESTS, "fake bridge did not receive mission request"
    request = REQUESTS[-1]
    assert request["model"] == "mission-a3-kimi", request
    assert request["stream"] is False, request
    content = request["input"][0]["content"]
    assert "<OSS_HANDOFF_JSON>" in content, content
    assert "mission_cli_run" in content, content


def main() -> int:
    assert_template_requires_scope()
    assert_template_outputs_mission_v1()
    assert_run_posts_mission_to_runtime_bridge()
    print("PASS: MissionV1 CLI delegation suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
