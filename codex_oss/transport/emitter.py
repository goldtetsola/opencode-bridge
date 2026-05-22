"""ResponseEmitter — single transport abstraction for Responses API SSE/JSON.

Guarantees: every stream=true path emits response.completed/failed before [DONE].
Every stream=false path returns valid JSON with proper Content-Type/Length.
No other code should write directly to handler.wfile.

Extracted from bridge.py to prevent god-file sprawl.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

JSON = Dict[str, Any]


def _new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


def _now() -> int:
    return int(time.time())


class ResponseEmitter:
    """Single transport abstraction. Every POST path must use this. No raw wfile writes."""

    def __init__(self, handler, response_id: str, model_alias: str,
                 stream: bool, created_at: Optional[int] = None):
        self.handler = handler
        self.response_id = response_id
        self.model_alias = model_alias
        self.stream = stream
        self.created_at = created_at or _now()
        self._terminated = False
        self._output_index = 0
        self._sse_headers_sent = False
        self._emitted_output: List[JSON] = []

    def start(self):
        if self.stream:
            self.handler.send_response(200)
            self.handler.send_header("Content-Type", "text/event-stream")
            self.handler.send_header("Cache-Control", "no-cache")
            self.handler.send_header("Connection", "close")
            self.handler.end_headers()
            self._sse_headers_sent = True
            self._write_sse("response.created", {
                "type": "response.created",
                "response": {
                    "id": self.response_id, "object": "response",
                    "created_at": self.created_at, "status": "in_progress",
                    "error": None, "incomplete_details": None,
                    "instructions": None, "model": self.model_alias,
                    "output": [], "parallel_tool_calls": False,
                    "previous_response_id": None, "store": False,
                    "temperature": None, "top_p": None, "truncation": "disabled",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    "metadata": {},
                }
            })

    def emit_text_message(self, text: str, status: str = "completed", phase: Optional[str] = None):
        msg_id = _new_id("msg")
        idx = self._next_index()
        message_item = {"type": "message", "id": msg_id, "status": status, "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}
        if phase:
            message_item["phase"] = phase

        if self.stream:
            if not self._sse_headers_sent:
                self.start()
            item = {"type": "message", "id": msg_id, "status": "in_progress", "role": "assistant", "content": []}
            if phase:
                item["phase"] = phase
            self._write_sse("response.output_item.added", {
                "type": "response.output_item.added", "output_index": idx, "item": item})
            self._write_sse("response.content_part.added", {
                "type": "response.content_part.added", "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}, "item_id": msg_id})
            self._write_sse("response.output_text.delta", {
                "type": "response.output_text.delta", "output_index": idx, "content_index": 0, "delta": text})
            self._write_sse("response.output_text.done", {
                "type": "response.output_text.done", "output_index": idx, "content_index": 0, "text": text})
            self._write_sse("response.content_part.done", {
                "type": "response.content_part.done", "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}, "item_id": msg_id})
            self._write_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": idx, "item": message_item})
        else:
            self._json_response = {
                "id": self.response_id, "object": "response",
                "created_at": self.created_at, "status": status,
                "error": None, "incomplete_details": None,
                "instructions": None, "model": self.model_alias,
                "output": [message_item], "parallel_tool_calls": False,
                "previous_response_id": None, "store": False,
                "temperature": None, "top_p": None, "truncation": "disabled",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "metadata": {},
            }
        self._emitted_output.append(message_item)

    def emit_commentary_message(self, text: str):
        self.emit_text_message(text, phase="commentary")

    def emit_error(self, message: str, status_code: int = 400, error_type: str = "invalid_request_error"):
        if self.stream and self._sse_headers_sent:
            self._write_sse("response.failed", {
                "type": "response.failed",
                "response": {"id": self.response_id, "object": "response",
                              "status": "failed",
                              "error": {"message": message, "type": error_type, "code": error_type}}})
            self._write_sse("done", {})
        else:
            data = json.dumps({"error": {"message": message, "type": error_type, "code": error_type}}).encode("utf-8")
            self.handler.send_response(status_code)
            self.handler.send_header("Content-Type", "application/json")
            self.handler.send_header("Content-Length", str(len(data)))
            self.handler.end_headers()
            self.handler.wfile.write(data)
        self._terminated = True

    def complete(self):
        if self._terminated:
            return
        if self.stream:
            self._write_sse("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": self.response_id, "object": "response",
                    "created_at": self.created_at, "status": "completed",
                    "error": None, "incomplete_details": None,
                    "instructions": None, "model": self.model_alias,
                    "output": self._emitted_output, "parallel_tool_calls": False,
                    "previous_response_id": None, "store": False,
                    "temperature": None, "top_p": None, "truncation": "disabled",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    "metadata": {},
                }
            })
            self._write_sse("done", {})
        else:
            if hasattr(self, '_json_response'):
                data = json.dumps(self._json_response, ensure_ascii=False).encode("utf-8")
                self.handler.send_response(200)
                self.handler.send_header("Content-Type", "application/json")
                self.handler.send_header("Content-Length", str(len(data)))
                self.handler.end_headers()
                self.handler.wfile.write(data)
        self._terminated = True

    def _write_sse(self, event: str, data):
        raw = json.dumps(data, ensure_ascii=False)
        msg = f"event: {event}\ndata: {raw}\n\n".encode("utf-8")
        if event == "done":
            msg = "data: [DONE]\n\n".encode("utf-8")
        try:
            self.handler.wfile.write(msg)
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._terminated = True

    def _next_index(self):
        i = self._output_index
        self._output_index += 1
        return i
