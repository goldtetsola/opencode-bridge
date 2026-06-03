"""Probe Codex app-server as a live progress surface.

This is the "second door" for native-feeling progress. Unlike the Desktop
spawned-child renderer probe, app-server exposes structured JSON-RPC
notifications such as item/agentMessage/delta. This helper verifies whether a
local Codex app-server turn can stream ordinary assistant text from the
controlled fake provider.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from codex_oss.desktop_pre_final_text_probe import (
    FINAL_LINE,
    PROBE_MODEL_ALIAS,
    PROBE_MODEL_PROVIDER,
    PROGRESS_LINES,
    _handler_factory,
)


APP_SERVER_PROBE_SCHEMA_VERSION = "app_server_pre_final_text_probe_result.v1"


def app_server_config_args(port: int) -> list[str]:
    base_url = f"http://127.0.0.1:{port}/v1"
    return [
        "-c",
        f'model_provider="{PROBE_MODEL_PROVIDER}"',
        "-c",
        f'model="{PROBE_MODEL_ALIAS}"',
        "-c",
        f'model_providers.{PROBE_MODEL_PROVIDER}.name="Desktop pre-final text probe"',
        "-c",
        f'model_providers.{PROBE_MODEL_PROVIDER}.base_url="{base_url}"',
        "-c",
        f'model_providers.{PROBE_MODEL_PROVIDER}.wire_api="responses"',
        "-c",
        f"model_providers.{PROBE_MODEL_PROVIDER}.request_max_retries=0",
        "-c",
        f"model_providers.{PROBE_MODEL_PROVIDER}.stream_max_retries=0",
        "-c",
        f"model_providers.{PROBE_MODEL_PROVIDER}.stream_idle_timeout_ms=30000",
        "-c",
        f'model_providers.{PROBE_MODEL_PROVIDER}.auth.command="echo"',
        "-c",
        f'model_providers.{PROBE_MODEL_PROVIDER}.auth.args=["sk-local-desktop-render-probe"]',
        "-c",
        f"model_providers.{PROBE_MODEL_PROVIDER}.auth.timeout_ms=1000",
    ]


class JsonRpcStdioClient:
    def __init__(self, args: list[str], cwd: Path):
        self.proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        self.messages: list[dict[str, Any]] = []
        self.stderr = ""
        self._responses: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._err_reader.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                msg = {"parse_error": stripped[:500]}
            self.messages.append(msg)
            if "id" in msg:
                self._responses.put(msg)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for chunk in self.proc.stderr:
            self.stderr += chunk

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        payload = {"method": method, "id": req_id, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        deferred: list[dict[str, Any]] = []
        try:
            while time.time() < deadline:
                try:
                    msg = self._responses.get(timeout=max(0.05, min(0.5, deadline - time.time())))
                except queue.Empty:
                    continue
                if msg.get("id") == req_id:
                    for item in deferred:
                        self._responses.put(item)
                    return msg
                deferred.append(msg)
            return {"id": req_id, "timeout": True, "method": method}
        finally:
            for item in deferred:
                self._responses.put(item)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def run_app_server_pre_final_text_probe(
    *,
    cwd: str | Path,
    port: int = 0,
    delay_seconds: float = 0.25,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    cwd_path = Path(cwd)
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(delay_seconds))
    actual_port = int(server.server_address[1])
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    client = JsonRpcStdioClient(
        ["codex", "app-server", "--listen", "stdio://", *app_server_config_args(actual_port)],
        cwd_path,
    )
    try:
        init = client.send(
            "initialize",
            {
                "clientInfo": {
                    "name": "opencode_bridge_app_server_probe",
                    "title": "OpenCode Bridge App Server Probe",
                    "version": "0.0.1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized")
        thread = client.send(
            "thread/start",
            {
                "cwd": str(cwd_path),
                "model": PROBE_MODEL_ALIAS,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "access": {"type": "fullAccess"}},
            },
        )
        thread_id = (((thread.get("result") or {}).get("thread") or {}).get("id"))
        turn = {}
        if thread_id:
            turn = client.send(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": "Run the desktop pre-final text probe. Do not use tools. Return exactly the provider stream.",
                        }
                    ],
                    "cwd": str(cwd_path),
                    "model": PROBE_MODEL_ALIAS,
                    "effort": "low",
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "access": {"type": "fullAccess"}},
                },
            )
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                delta_text = "".join(
                    str((m.get("params") or {}).get("delta") or "")
                    for m in client.messages
                    if m.get("method") == "item/agentMessage/delta"
                )
                if all(marker in delta_text for marker in [*PROGRESS_LINES, FINAL_LINE]):
                    break
                time.sleep(0.1)

        notifications = [m for m in client.messages if m.get("method")]
        delta_text = "".join(
            str((m.get("params") or {}).get("delta") or "")
            for m in notifications
            if m.get("method") == "item/agentMessage/delta"
        )
        progress_before_final = (
            delta_text.find(PROGRESS_LINES[0]) != -1
            and delta_text.find(PROGRESS_LINES[1]) != -1
            and delta_text.find(PROGRESS_LINES[2]) != -1
            and delta_text.find(FINAL_LINE) != -1
            and delta_text.find(PROGRESS_LINES[0]) < delta_text.find(FINAL_LINE)
            and delta_text.find(PROGRESS_LINES[1]) < delta_text.find(FINAL_LINE)
            and delta_text.find(PROGRESS_LINES[2]) < delta_text.find(FINAL_LINE)
        )
        return {
            "schema_version": APP_SERVER_PROBE_SCHEMA_VERSION,
            "probe_status": "pass" if progress_before_final else "fail",
            "surface": "codex_app_server",
            "provider": PROBE_MODEL_PROVIDER,
            "model": PROBE_MODEL_ALIAS,
            "port": actual_port,
            "init_ok": bool(init.get("result")),
            "thread_ok": bool(thread_id),
            "turn_ok": bool(turn.get("result")),
            "observed_agent_message_delta": bool(delta_text),
            "observed_progress_before_final": progress_before_final,
            "delta_text": delta_text,
            "methods": [m.get("method") for m in notifications],
            "item_types": [
                ((m.get("params") or {}).get("item") or {}).get("type")
                for m in notifications
                if m.get("method") in {"item/started", "item/completed"}
            ],
            "errors": [m.get("error") for m in client.messages if m.get("error")],
            "stderr_excerpt": client.stderr[:2000],
            "recorded_at": time.time(),
        }
    finally:
        client.close()
        server.shutdown()
        server.server_close()


def write_app_server_probe_result(result: dict[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return path
