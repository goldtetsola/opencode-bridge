"""Tranche 0 probe for Codex Desktop child pre-final text rendering.

This is intentionally outside MissionV1 and the OSS bridge runtime. It answers
one question: can the Desktop spawned-child surface render ordinary assistant
text before the final message?
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

JSON = dict[str, Any]

PROBE_SCHEMA_VERSION = "desktop_pre_final_text_probe_result.v1"
PROBE_MODEL_PROVIDER = "desktop_pre_final_text_probe"
PROBE_MODEL_ALIAS = "desktop-pre-final-text-probe"
PROGRESS_LINES = ["PROGRESS_ONE", "PROGRESS_TWO", "PROGRESS_THREE"]
FINAL_LINE = "FINAL_DONE"
VALID_PROBE_STATUSES = {"pass", "fail", "flaky", "setup_failed", "unknown"}


def provider_config(*, port: int = 43211, host: str = "127.0.0.1") -> str:
    """Return a Codex model provider config snippet for the fake server."""
    return f"""[model_providers.{PROBE_MODEL_PROVIDER}]
name = "Desktop pre-final text probe"
base_url = "http://{host}:{port}/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 30000

[model_providers.{PROBE_MODEL_PROVIDER}.auth]
command = "echo"
args = ["sk-local-desktop-render-probe"]
timeout_ms = 1000
"""


def agent_prompt() -> str:
    return (
        "Use model provider desktop_pre_final_text_probe and model "
        "desktop-pre-final-text-probe. Do not use tools. Report exactly what "
        "you receive from the provider."
    )


def write_probe_result(
    *,
    output_path: str | Path,
    probe_status: str,
    observed_progress_before_final: bool | None = None,
    observed_final: bool | None = None,
    notes: str = "",
    runs: list[JSON] | None = None,
) -> JSON:
    status = normalize_probe_status(probe_status)
    result = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "probe_name": "desktop_pre_final_text_probe",
        "probe_status": status,
        "desktop_render_surface": {
            "probe_required": True,
            "probe_status": status if status in {"pass", "fail", "flaky"} else "unknown",
            "claim_policy": claim_policy_for_probe(status),
        },
        "observed_progress_before_final": observed_progress_before_final,
        "observed_final": observed_final,
        "progress_markers": PROGRESS_LINES,
        "final_marker": FINAL_LINE,
        "notes": notes,
        "runs": runs or [],
        "recorded_at": time.time(),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["path"] = str(path)
    return result


def normalize_probe_status(status: str) -> str:
    lowered = str(status or "unknown").strip().lower()
    return lowered if lowered in VALID_PROBE_STATUSES else "unknown"


def claim_policy_for_probe(status: str) -> JSON:
    normalized = normalize_probe_status(status)
    if normalized == "pass":
        return {
            "desktop_live_commentary_claim_allowed": True,
            "reason": "Desktop rendered pre-final child assistant text in the probe.",
        }
    if normalized == "setup_failed":
        return {
            "desktop_live_commentary_claim_allowed": False,
            "reason": "Probe setup failed; rerun before classifying the Desktop render surface.",
        }
    if normalized == "flaky":
        return {
            "desktop_live_commentary_claim_allowed": False,
            "reason": "Pre-final child text rendering was inconsistent; treat as best-effort only.",
        }
    if normalized == "fail":
        return {
            "desktop_live_commentary_claim_allowed": False,
            "reason": "Desktop did not render pre-final child assistant text; Desktop live-commentary claims are disallowed.",
        }
    return {
        "desktop_live_commentary_claim_allowed": False,
        "reason": "Desktop render surface is unknown; run the pre-final text probe first.",
    }


def run_self_test(*, host: str = "127.0.0.1", port: int = 0, delay_seconds: float = 0.01) -> JSON:
    """Start the fake provider locally and verify its SSE order."""
    server = ThreadingHTTPServer((host, port), _handler_factory(delay_seconds))
    actual_port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"model": PROBE_MODEL_ALIAS, "input": "probe", "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{actual_port}/v1/responses",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-local-desktop-render-probe"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            sse_text = response.read().decode("utf-8", errors="replace")
    finally:
        server.shutdown()
        server.server_close()
    events = parse_sse_events(sse_text)
    deltas = [event["data"].get("delta", "") for event in events if event["event"] == "response.output_text.delta"]
    text = "".join(str(delta) for delta in deltas)
    expected_order = PROGRESS_LINES + [FINAL_LINE]
    positions = [text.find(marker) for marker in expected_order]
    order_ok = all(position >= 0 for position in positions) and positions == sorted(positions)
    completed = any(event["event"] == "response.completed" for event in events)
    done = "[DONE]" in sse_text
    return {
        "schema_version": "desktop_pre_final_text_probe_self_test.v1",
        "ok": bool(order_ok and completed and done),
        "port": actual_port,
        "delta_text": text,
        "progress_markers": PROGRESS_LINES,
        "final_marker": FINAL_LINE,
        "events_seen": [event["event"] for event in events],
        "order_ok": order_ok,
        "response_completed": completed,
        "done_seen": done,
    }


def serve_forever(*, host: str = "127.0.0.1", port: int = 43211, delay_seconds: float = 1.0) -> None:
    server = ThreadingHTTPServer((host, port), _handler_factory(delay_seconds))
    print(f"desktop_pre_final_text_probe listening on http://{host}:{port}/v1/responses", flush=True)
    print("Spawn a child/subagent with the desktop_pre_final_text_probe provider, then observe the Desktop UI.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def parse_sse_events(sse_text: str) -> list[JSON]:
    events: list[JSON] = []
    for block in str(sse_text or "").split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = {"raw": data}
        events.append({"event": event_name, "data": parsed})
    return events


def _handler_factory(delay_seconds: float):
    class DesktopPreFinalTextProbeHandler(BaseHTTPRequestHandler):
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
            _emit_probe_stream(self, delay_seconds=delay_seconds)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return DesktopPreFinalTextProbeHandler


def _emit_probe_stream(handler: BaseHTTPRequestHandler, *, delay_seconds: float) -> None:
    response_id = "resp_desktop_pre_final_probe"
    item_id = "msg_desktop_pre_final_probe"
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
    for marker in [*PROGRESS_LINES, FINAL_LINE]:
        delta = marker + "\n"
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


def _write_sse(handler: BaseHTTPRequestHandler, event: str, payload: JSON) -> None:
    handler.wfile.write(f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8"))
    handler.wfile.flush()
