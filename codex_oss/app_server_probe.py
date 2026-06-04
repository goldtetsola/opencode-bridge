"""Probe Codex app-server as a live progress surface.

This is the "second door" for native-feeling progress. Unlike the Desktop
spawned-child renderer probe, app-server exposes structured JSON-RPC
notifications such as item/agentMessage/delta. This helper verifies whether a
local Codex app-server turn can stream ordinary assistant text from the
controlled fake provider.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import subprocess
import tempfile
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
    _write_sse,
)
from codex_oss.visible_commentary import VisibleCommentarySink


APP_SERVER_PROBE_SCHEMA_VERSION = "app_server_pre_final_text_probe_result.v1"
APP_SERVER_VISIBLE_COMMENTARY_SCHEMA_VERSION = "app_server_visible_commentary_probe_result.v1"
VISIBLE_COMMENTARY_FINAL_MARKER = "FINAL_RUNTIME_REPORT_READY"


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


def render_commentary_event_for_app_server(event: dict[str, Any]) -> str:
    """Render one public commentary event as compact assistant text."""
    title = str(event.get("title") or "").strip()
    message = str(event.get("message") or "").strip()
    phase = str(event.get("phase") or "").strip()
    prefix = f"[{phase}] " if phase else ""
    if title and message:
        return f"{prefix}{title}: {message}"
    return f"{prefix}{title or message}".strip()


def build_visible_commentary_probe_payload(
    *,
    mission_dir: str | Path,
    mission_id: str = "app_server_visible_commentary_probe",
) -> dict[str, Any]:
    """Create runtime-style visible commentary events and render stream lines."""
    mission_path = Path(mission_dir)
    streamed: list[dict[str, Any]] = []
    sink = VisibleCommentarySink(mission_id, mission_path, stream_callback=streamed.append)
    sink.emit(
        "mission_started",
        "Mission accepted",
        "I'm loading the runtime contract and checking the evidence floor first.",
        phase="PLAN",
        source="runtime",
    )
    sink.emit(
        "tool_action_started",
        "Inspecting required source",
        "I'm reading the declared project source because the coverage rule depends on it.",
        phase="NARROW",
        source="runtime",
        evidence_refs=["file:README.md#probe"],
    )
    sink.emit(
        "coverage_update",
        "Coverage updated",
        "The required read-only evidence floor is satisfied; I'm preparing the final answer.",
        phase="VERIFY",
        source="coverage",
    )
    sink.close({"status": "COMPLETE", "mission_id": mission_id})
    lines = [render_commentary_event_for_app_server(event) for event in streamed]
    lines.append(VISIBLE_COMMENTARY_FINAL_MARKER)
    return {
        "mission_id": mission_id,
        "events": streamed,
        "lines": lines,
        "visible_commentary_path": str(sink.path),
        "summary_path": str(sink.summary_path),
        "delivery_path": str(mission_path / "commentary_delivery.json"),
    }


def _emit_text_lines_stream(
    handler: BaseHTTPRequestHandler,
    *,
    lines: list[str],
    delay_seconds: float,
    response_id: str,
    item_id: str,
) -> None:
    created_at = int(time.time())
    full_text = ""
    _write_sse(handler, "response.created", {
        "type": "response.created",
        "response": {"id": response_id, "object": "response", "created_at": created_at, "status": "in_progress"},
    })
    _write_sse(handler, "response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
    })
    _write_sse(handler, "response.content_part.added", {
        "type": "response.content_part.added",
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": ""},
    })
    for line in lines:
        delta = str(line).rstrip() + "\n"
        full_text += delta
        _write_sse(handler, "response.output_text.delta", {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
        })
        time.sleep(max(0.0, float(delay_seconds)))
    _write_sse(handler, "response.output_text.done", {
        "type": "response.output_text.done",
        "output_index": 0,
        "content_index": 0,
        "text": full_text,
    })
    _write_sse(handler, "response.content_part.done", {
        "type": "response.content_part.done",
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": full_text},
    })
    _write_sse(handler, "response.output_item.done", {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": full_text}],
        },
    })
    _write_sse(handler, "response.completed", {
        "type": "response.completed",
        "response": {"id": response_id, "object": "response", "created_at": created_at, "status": "completed"},
    })
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _handler_factory_for_lines(lines: list[str], delay_seconds: float):
    model_handler = _handler_factory(delay_seconds)

    class AppServerLinesProbeHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            return model_handler.do_GET(self)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
            if self.path != "/v1/responses":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            _emit_text_lines_stream(
                self,
                lines=lines,
                delay_seconds=delay_seconds,
                response_id="resp_app_server_visible_commentary_probe",
                item_id="msg_app_server_visible_commentary_probe",
            )

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return AppServerLinesProbeHandler


def _run_app_server_text_lines_probe(
    *,
    cwd_path: Path,
    lines: list[str],
    final_marker: str,
    port: int,
    delay_seconds: float,
    timeout_seconds: float,
    prompt: str,
) -> dict[str, Any]:
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory_for_lines(lines, delay_seconds))
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
                    "name": "opencode_bridge_app_server_visible_commentary_probe",
                    "title": "OpenCode Bridge App Server Visible Commentary Probe",
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
        turn = {"result": None}
        if thread_id:
            turn = client.send(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
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
                if all(line in delta_text for line in lines) and final_marker in delta_text:
                    break
                time.sleep(0.1)

        notifications = [m for m in client.messages if m.get("method")]
        delta_text = "".join(
            str((m.get("params") or {}).get("delta") or "")
            for m in notifications
            if m.get("method") == "item/agentMessage/delta"
        )
        final_index = delta_text.find(final_marker)
        observed_lines = [line for line in lines if line in delta_text]
        progress_before_final = (
            final_index != -1
            and all(delta_text.find(line) != -1 and delta_text.find(line) < final_index for line in lines if line != final_marker)
        )
        return {
            "port": actual_port,
            "init_ok": bool(init.get("result")),
            "thread_ok": bool(thread_id),
            "turn_ok": bool(turn.get("result")),
            "observed_agent_message_delta": bool(delta_text),
            "observed_progress_before_final": progress_before_final,
            "delta_text": delta_text,
            "observed_lines": observed_lines,
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


def run_app_server_visible_commentary_probe(
    *,
    cwd: str | Path,
    port: int = 0,
    delay_seconds: float = 0.25,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    cwd_path = Path(cwd)
    with tempfile.TemporaryDirectory(prefix="app-server-visible-commentary-") as tmp:
        payload = build_visible_commentary_probe_payload(mission_dir=Path(tmp))
        lines = list(payload["lines"])
        stream_result = _run_app_server_text_lines_probe(
            cwd_path=cwd_path,
            lines=lines,
            final_marker=VISIBLE_COMMENTARY_FINAL_MARKER,
            port=port,
            delay_seconds=delay_seconds,
            timeout_seconds=timeout_seconds,
            prompt="Run the app-server visible commentary probe. Do not use tools. Return exactly the provider stream.",
        )
        commentary_lines = [line for line in lines if line != VISIBLE_COMMENTARY_FINAL_MARKER]
        final_index = stream_result["delta_text"].find(VISIBLE_COMMENTARY_FINAL_MARKER)
        commentary_before_final = (
            final_index != -1
            and all(stream_result["delta_text"].find(line) != -1 and stream_result["delta_text"].find(line) < final_index for line in commentary_lines)
        )
        return {
            "schema_version": APP_SERVER_VISIBLE_COMMENTARY_SCHEMA_VERSION,
            "probe_status": "pass" if commentary_before_final else "fail",
            "surface": "codex_app_server",
            "provider": PROBE_MODEL_PROVIDER,
            "model": PROBE_MODEL_ALIAS,
            "commentary_events_count": len(payload["events"]),
            "commentary_lines": commentary_lines,
            "final_marker": VISIBLE_COMMENTARY_FINAL_MARKER,
            "observed_commentary_before_final": commentary_before_final,
            "visible_commentary_path": payload["visible_commentary_path"],
            "summary_path": payload["summary_path"],
            **stream_result,
        }


def write_app_server_probe_result(result: dict[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return path
