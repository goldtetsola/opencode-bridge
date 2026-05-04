#!/usr/bin/env python3
"""
responses_chat_proxy_v2.py

A small, dependency-free OpenAI Responses API -> OpenAI-compatible Chat Completions
bridge, designed for Codex custom model providers that need to call OpenCode Go OSS
models such as deepseek-v4-pro and kimi-k2.6.

What this proxy does:
- Accepts POST /v1/responses from Codex.
- Converts Responses input items to Chat Completions messages.
- Converts Responses function tools to Chat Completions function tools.
- Drops unsupported hosted/MCP/namespace tool types by default.
- Tracks response state and repairs orphan function_call_output turns.
- Preserves provider reasoning_content in stored assistant messages.
- Preserves valid completed assistant->tool exchanges instead of truncating context.
- Emits normal JSON or SSE-like Responses events back to Codex.
- Forwards GET /v1/models to the upstream provider.

Intended upstream:
  https://opencode.ai/zen/go/v1/chat/completions

Required env:
  OPENCODE_GO_API_KEY

Recommended env:
  PROXY_API_KEY or LITELLM_MASTER_KEY   # key Codex sends to this local proxy
  PROXY_PORT=4000
  PROXY_STATE_DB=/tmp/opencode_responses_proxy_state.sqlite3

Codex config example:
  [model_providers.litellm_opencode_go]
  name = "OpenCode Go Responses Proxy"
  base_url = "http://127.0.0.1:4000/v1"
  env_key = "LITELLM_MASTER_KEY"
  wire_api = "responses"

Security:
  Bind this to localhost only. Do not expose it publicly.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple

JSON = Dict[str, Any]

DEFAULT_MODEL_MAP = {
    "ocg-deepseek-v4-pro": "deepseek-v4-pro",
    "ocg-deepseek-v4-flash": "deepseek-v4-flash",
    "ocg-kimi-k2.6": "kimi-k2.6",
    "ocg-kimi-k2.5": "kimi-k2.5",
    "ocg-qwen3.6-plus": "qwen3.6-plus",
    "ocg-qwen3.5-plus": "qwen3.5-plus",
    "ocg-glm-5.1": "glm-5.1",
    "ocg-glm-5": "glm-5",
    "ocg-minimax-m2.7": "minimax-m2.7",
    "ocg-minimax-m2.5": "minimax-m2.5",
    # Also accept direct OpenCode-style aliases.
    "opencode-go/deepseek-v4-pro": "deepseek-v4-pro",
    "opencode-go/deepseek-v4-flash": "deepseek-v4-flash",
    "opencode-go/kimi-k2.6": "kimi-k2.6",
    "opencode-go/kimi-k2.5": "kimi-k2.5",
    "opencode-go/qwen3.6-plus": "qwen3.6-plus",
    "opencode-go/qwen3.5-plus": "qwen3.5-plus",
    "opencode-go/glm-5.1": "glm-5.1",
    "opencode-go/glm-5": "glm-5",
}

DROP_TOOL_TYPES = {
    "image_generation",
    "image_generation_call",
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use_preview",
    "mcp",
    "namespace",
    "custom",  # Chat Completions function tooling cannot represent free-text custom tools safely.
}

VALID_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def now() -> int:
    return int(time.time())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def as_text(value: Any) -> str:
    """Convert Responses content/output fields into a string safe for Chat messages."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                typ = item.get("type")
                if typ in ("input_text", "output_text", "text"):
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
                elif "content" in item:
                    parts.append(as_text(item.get("content")))
                else:
                    # Preserve non-text items in a compact, visible form.
                    parts.append(json_dumps(item))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p != "")
    if isinstance(value, dict):
        if "text" in value:
            return str(value["text"])
        if "content" in value:
            return as_text(value["content"])
        return json_dumps(value)
    return str(value)


def normalize_message_for_chat(msg: JSON) -> Optional[JSON]:
    """Keep only Chat Completions fields that upstream providers are likely to accept."""
    role = msg.get("role")
    if role not in ("system", "developer", "user", "assistant", "tool"):
        return None

    out: JSON = {"role": role}
    # Map developer role to system for providers that don't support it (DeepSeek, Kimi, etc.)
    if out["role"] == "developer":
        out["role"] = "system"
    if role == "tool":
        tool_call_id = msg.get("tool_call_id")
        if not tool_call_id:
            return None
        out["tool_call_id"] = str(tool_call_id)
        out["content"] = as_text(msg.get("content", msg.get("output", "")))
        return out

    content = msg.get("content")
    if content is None:
        # DeepSeek requires assistant tool-call messages to have non-null content.
        content = "" if role == "assistant" else ""
    out["content"] = as_text(content)

    if role == "assistant":
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        # Preserve provider-specific reasoning fields if the upstream gave them to us.
        # DeepSeek V4 thinking/tool-call flows require this field to be replayed.
        if msg.get("reasoning_content"):
            out["reasoning_content"] = msg["reasoning_content"]
        if msg.get("thinking_blocks"):
            out["thinking_blocks"] = msg["thinking_blocks"]

    return out


def tool_output_item_to_chat_tool(item: JSON) -> Optional[JSON]:
    call_id = item.get("call_id") or item.get("tool_call_id")
    if not call_id:
        return None
    return {
        "role": "tool",
        "tool_call_id": str(call_id),
        "content": as_text(item.get("output", item.get("content", ""))),
    }


def extract_request_messages_and_tool_outputs(body: JSON) -> Tuple[List[JSON], List[JSON]]:
    """
    Convert the incoming Responses request into:
      - new Chat messages from instructions/input
      - tool output messages extracted from function_call_output items
    """
    messages: List[JSON] = []
    tool_outputs: List[JSON] = []

    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": as_text(instructions)})

    inp = body.get("input", "")
    if isinstance(inp, str):
        if inp.strip():
            messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                messages.append({"role": "user", "content": as_text(item)})
                continue

            typ = item.get("type")
            role = item.get("role")

            if typ == "function_call_output":
                tool_msg = tool_output_item_to_chat_tool(item)
                if tool_msg:
                    tool_outputs.append(tool_msg)
                continue

            if typ == "message" or role in ("system", "developer", "user", "assistant", "tool"):
                # Responses message item: {"type":"message","role":"user","content":[...]}
                chat_msg = normalize_message_for_chat({
                    "role": role or item.get("role", "user"),
                    "content": item.get("content", item.get("text", "")),
                    "tool_call_id": item.get("tool_call_id"),
                    "tool_calls": item.get("tool_calls"),
                    "reasoning_content": item.get("reasoning_content"),
                    "thinking_blocks": item.get("thinking_blocks"),
                })
                if chat_msg:
                    if chat_msg["role"] == "tool":
                        tool_outputs.append(chat_msg)
                    else:
                        messages.append(chat_msg)
                continue

            if typ in ("input_text", "output_text", "text"):
                messages.append({"role": "user", "content": as_text(item)})
                continue

            # Ignore Responses output items that should not be replayed as user messages.
            if typ in ("function_call", "reasoning"):
                continue

            # Conservative fallback: visible user text rather than data loss.
            messages.append({"role": "user", "content": as_text(item)})
    elif inp:
        messages.append({"role": "user", "content": as_text(inp)})

    return messages, tool_outputs


@dataclass
class StoredResponse:
    response_id: str
    model_alias: str
    model_upstream: str
    messages: List[JSON]
    pending_call_ids: List[str]
    created_at: int


class StateStore:
    """SQLite-backed state store so proxy restarts do not immediately destroy Codex tool turns."""

    def __init__(self, path: str, ttl_seconds: int = 6 * 3600, max_responses: int = 2000):
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.max_responses = max_responses
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                model_alias TEXT NOT NULL,
                model_upstream TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                pending_call_ids_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS call_index (
                call_id TEXT PRIMARY KEY,
                response_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.db.commit()

    def cleanup(self) -> None:
        cutoff = now() - self.ttl_seconds
        with self.lock:
            self.db.execute("DELETE FROM call_index WHERE created_at < ?", (cutoff,))
            self.db.execute("DELETE FROM responses WHERE created_at < ?", (cutoff,))
            # Keep the latest N responses.
            rows = self.db.execute(
                "SELECT response_id FROM responses ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (self.max_responses,),
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                self.db.executemany("DELETE FROM responses WHERE response_id = ?", [(i,) for i in ids])
                self.db.executemany("DELETE FROM call_index WHERE response_id = ?", [(i,) for i in ids])
            self.db.commit()

    def put(self, state: StoredResponse) -> None:
        with self.lock:
            self.db.execute(
                """
                INSERT OR REPLACE INTO responses
                (response_id, model_alias, model_upstream, messages_json, pending_call_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state.response_id,
                    state.model_alias,
                    state.model_upstream,
                    json_dumps(state.messages),
                    json_dumps(state.pending_call_ids),
                    state.created_at,
                ),
            )
            for call_id in state.pending_call_ids:
                self.db.execute(
                    "INSERT OR REPLACE INTO call_index (call_id, response_id, created_at) VALUES (?, ?, ?)",
                    (call_id, state.response_id, state.created_at),
                )
            self.db.commit()

    def get(self, response_id: str) -> Optional[StoredResponse]:
        with self.lock:
            row = self.db.execute(
                "SELECT response_id, model_alias, model_upstream, messages_json, pending_call_ids_json, created_at FROM responses WHERE response_id = ?",
                (response_id,),
            ).fetchone()
        if not row:
            return None
        return StoredResponse(
            response_id=row[0],
            model_alias=row[1],
            model_upstream=row[2],
            messages=json.loads(row[3]),
            pending_call_ids=json.loads(row[4]),
            created_at=int(row[5]),
        )

    def find_by_call_ids(self, call_ids: Iterable[str]) -> Optional[StoredResponse]:
        ids = [str(x) for x in call_ids if x]
        if not ids:
            return None
        with self.lock:
            rows = self.db.execute(
                f"SELECT response_id, COUNT(*) AS c FROM call_index WHERE call_id IN ({','.join(['?'] * len(ids))}) GROUP BY response_id ORDER BY c DESC, MAX(created_at) DESC LIMIT 1",
                ids,
            ).fetchall()
        if not rows:
            return None
        return self.get(rows[0][0])


class HistoryRepairError(Exception):
    pass


def tool_call_ids(assistant_msg: JSON) -> List[str]:
    ids: List[str] = []
    for tc in assistant_msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            if tc.get("id"):
                ids.append(str(tc["id"]))
            elif tc.get("call_id"):
                ids.append(str(tc["call_id"]))
    return ids


def repair_chat_history(messages: List[JSON], current_tool_outputs: Optional[List[JSON]] = None) -> List[JSON]:
    """
    Return the longest valid Chat Completions history.

    Rules enforced:
    - Orphan tool messages are dropped.
    - Each assistant message with tool_calls is included only if all of its tool_calls
      are immediately satisfied by following tool messages, or by current_tool_outputs.
    - If an assistant has unsatisfied tool_calls and current outputs satisfy it, we append
      those outputs and stop; any later invalid/nested history is ignored.
    - Earlier complete assistant->tool exchanges are preserved.

    This is the fix for the truncation issue: we do not throw away all prior context,
    only invalid or incomplete tails.
    """
    pending_now: Dict[str, JSON] = {}
    for t in current_tool_outputs or []:
        if t.get("role") == "tool" and t.get("tool_call_id"):
            pending_now[str(t["tool_call_id"])] = normalize_message_for_chat(t) or t

    repaired: List[JSON] = []
    i = 0

    while i < len(messages):
        raw = messages[i]
        msg = normalize_message_for_chat(raw)
        if not msg:
            i += 1
            continue

        role = msg["role"]

        if role == "tool":
            # A tool message without its immediately preceding assistant tool-call message
            # is invalid in Chat Completions. Drop it.
            i += 1
            continue

        if role != "assistant" or not msg.get("tool_calls"):
            repaired.append(msg)
            i += 1
            continue

        ids = tool_call_ids(msg)
        if not ids:
            repaired.append(msg)
            i += 1
            continue

        # Collect contiguous tool messages immediately after this assistant.
        following_tools: Dict[str, JSON] = {}
        j = i + 1
        while j < len(messages):
            next_msg = normalize_message_for_chat(messages[j])
            if not next_msg or next_msg.get("role") != "tool":
                break
            tid = str(next_msg.get("tool_call_id", ""))
            if tid:
                following_tools[tid] = next_msg
            j += 1

        combined = dict(following_tools)
        combined.update(pending_now)

        if all(tid in combined for tid in ids):
            # Include assistant, then exactly one tool message for each tool_call in order.
            repaired.append(msg)
            for tid in ids:
                repaired.append(combined[tid])
            # If we used current tool outputs to complete this assistant, this is the
            # conversation point Codex is asking us to continue from. Stop here.
            if any(tid in pending_now for tid in ids):
                return repaired
            i = j
            continue

        # This assistant has unsatisfied tool calls. If the current request supplied
        # only a subset of this assistant's tool outputs, fail loudly: sending a
        # partial assistant->tool exchange upstream will be rejected by strict
        # providers such as DeepSeek. In normal Codex flows this should be rare
        # because parallel tool calls are discouraged.
        if pending_now and any(tid in pending_now for tid in ids):
            missing = [tid for tid in ids if tid not in combined]
            raise HistoryRepairError(
                "Partial function_call_output set for assistant tool_calls. Missing: "
                + ", ".join(missing)
            )

        # Otherwise it is a stale incomplete tail. Keeping it would make the next
        # provider request invalid, so preserve the valid prefix and stop.
        return repaired

    # If Codex sent tool outputs but we never found their matching assistant in history,
    # fail loudly rather than sending orphan tool messages upstream.
    if pending_now:
        missing = sorted(pending_now.keys())
        raise HistoryRepairError(
            "Could not match function_call_output item(s) to a stored assistant tool_call: "
            + ", ".join(missing)
        )

    return repaired


def merge_new_user_messages(base: List[JSON], new_messages: List[JSON]) -> List[JSON]:
    out = list(base)
    for msg in new_messages:
        norm = normalize_message_for_chat(msg)
        if norm and norm["role"] != "tool":
            out.append(norm)
    return out


def sanitize_tool_name(name: str) -> str:
    if VALID_FUNCTION_NAME.match(name):
        return name
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]
    return cleaned or "tool"


def convert_responses_tools(tools: Any) -> Tuple[List[JSON], Dict[str, str]]:
    """
    Convert Responses-style tool definitions into Chat Completions function tools.
    Returns (tools, reverse_name_map): sanitized_name -> original_name.
    """
    converted: List[JSON] = []
    reverse_name_map: Dict[str, str] = {}

    if not isinstance(tools, list):
        return converted, reverse_name_map

    used_names: set[str] = set()

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        typ = tool.get("type")

        # Already a nested Chat Completions function tool.
        if typ == "function" and isinstance(tool.get("function"), dict):
            fn = dict(tool["function"])
            original = str(fn.get("name") or "tool")
            sanitized = sanitize_tool_name(original)
            base = sanitized
            n = 2
            while sanitized in used_names:
                sanitized = f"{base[:56]}_{n}"
                n += 1
            used_names.add(sanitized)
            reverse_name_map[sanitized] = original
            fn["name"] = sanitized
            fn.setdefault("description", "")
            fn.setdefault("parameters", {"type": "object", "properties": {}})
            converted.append({"type": "function", "function": fn})
            continue

        # Responses-style function tool:
        # {"type":"function","name":"...","description":"...","parameters":{...}}
        if typ == "function" and tool.get("name"):
            original = str(tool["name"])
            sanitized = sanitize_tool_name(original)
            base = sanitized
            n = 2
            while sanitized in used_names:
                sanitized = f"{base[:56]}_{n}"
                n += 1
            used_names.add(sanitized)
            reverse_name_map[sanitized] = original
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": sanitized,
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
            )
            continue

        if typ in DROP_TOOL_TYPES:
            continue

        # Some Codex tools can appear as flat objects with name/parameters but no type.
        # Treat these as function tools only when a name and object parameters exist.
        if tool.get("name") and isinstance(tool.get("parameters"), dict):
            original = str(tool["name"])
            sanitized = sanitize_tool_name(original)
            base = sanitized
            n = 2
            while sanitized in used_names:
                sanitized = f"{base[:56]}_{n}"
                n += 1
            used_names.add(sanitized)
            reverse_name_map[sanitized] = original
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": sanitized,
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters"),
                    },
                }
            )

    return converted, reverse_name_map


def restore_tool_name(name: str, reverse_name_map: Dict[str, str]) -> str:
    return reverse_name_map.get(name, name)


def map_model(model: str, model_map: Dict[str, str]) -> str:
    if model in model_map:
        return model_map[model]
    if model.startswith("opencode-go/"):
        return model.split("/", 1)[1]
    if model.startswith("ocg-"):
        # Best effort: ocg-deepseek-v4-pro -> deepseek-v4-pro
        return model[4:]
    return model


def is_deepseek(model: str) -> bool:
    return "deepseek" in model.lower()


class UpstreamError(Exception):
    def __init__(self, status: int, body: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(f"upstream error {status}: {body[:1000]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


class ProxyApp:
    def __init__(self):
        self.upstream_base = os.getenv("UPSTREAM_BASE", "https://opencode.ai/zen/go/v1").rstrip("/")
        self.upstream_chat_url = f"{self.upstream_base}/chat/completions"
        self.upstream_models_url = f"{self.upstream_base}/models"
        self.upstream_key = os.getenv("OPENCODE_GO_API_KEY", "")
        self.proxy_key = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""
        self.timeout = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "240"))
        self.max_retries = int(os.getenv("UPSTREAM_RETRIES", "2"))
        self.state = StateStore(
            os.getenv("PROXY_STATE_DB", "/tmp/opencode_responses_proxy_state.sqlite3"),
            ttl_seconds=int(os.getenv("PROXY_STATE_TTL_SECONDS", str(6 * 3600))),
            max_responses=int(os.getenv("PROXY_STATE_MAX_RESPONSES", "2000")),
        )
        env_map = os.getenv("MODEL_MAP_JSON")
        self.model_map = dict(DEFAULT_MODEL_MAP)
        if env_map:
            self.model_map.update(json.loads(env_map))
        self.force_single_tool = os.getenv("FORCE_SINGLE_TOOL_INSTRUCTIONS", "1") != "0"
        self.log_path = os.getenv("PROXY_LOG_PATH", "")
        self.strip_tools = os.getenv("STRIP_TOOLS", "0") == "1"
        # Optional: {"deepseek-v4-pro": ["kimi-k2.6", "deepseek-v4-flash"]}
        # Useful for provider capacity errors. It will not bypass a hard account-wide
        # OpenCode Go quota, but it can route around model-specific congestion.
        self.fallback_model_map = json.loads(os.getenv("FALLBACK_MODEL_MAP_JSON", "{}") or "{}")

    def log(self, msg: str, **fields: Any) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        if fields:
            line += " " + json_dumps(fields)
        print(line, file=sys.stderr, flush=True)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def auth_ok(self, auth_header: str) -> bool:
        if not self.proxy_key:
            return True
        if not auth_header.lower().startswith("bearer "):
            return False
        return auth_header.split(" ", 1)[1].strip() == self.proxy_key

    def call_upstream_chat(self, payload: JSON) -> JSON:
        if not self.upstream_key:
            raise UpstreamError(500, "OPENCODE_GO_API_KEY is not set")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/2.0",
            "Accept": "application/json",
        }

        last_err: Optional[UpstreamError] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.upstream_chat_url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                hdrs = {k: v for k, v in e.headers.items()}
                last_err = UpstreamError(e.code, body, hdrs)

                # Retry only transient errors/rate limits.
                if e.code not in (408, 409, 429, 500, 502, 503, 504) or attempt >= self.max_retries:
                    break
                retry_after = hdrs.get("Retry-After")
                if retry_after:
                    try:
                        sleep_s = min(float(retry_after), 60.0)
                    except ValueError:
                        sleep_s = min(2 ** attempt, 30.0)
                else:
                    sleep_s = min(2 ** attempt, 30.0)
                self.log("upstream_retry", status=e.code, attempt=attempt + 1, sleep_s=sleep_s)
                time.sleep(sleep_s)
            except urllib.error.URLError as e:
                last_err = UpstreamError(502, f"network error: {e}")
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** attempt, 30.0))

        assert last_err is not None
        raise last_err

    def forward_models(self) -> Tuple[int, bytes, str]:
        if not self.upstream_key:
            return 500, b'{"error":{"message":"OPENCODE_GO_API_KEY is not set"}}', "application/json"
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "User-Agent": "codex-opencode-go-responses-proxy/2.0",
            "Accept": "application/json",
        }
        req = urllib.request.Request(self.upstream_models_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), resp.headers.get_content_type()
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get_content_type()

    def prepare_chat_payload(self, body: JSON) -> Tuple[JSON, List[JSON], str, str, Dict[str, str]]:
        model_alias = str(body.get("model") or "ocg-deepseek-v4-pro")
        model_upstream = map_model(model_alias, self.model_map)

        new_messages, current_tool_outputs = extract_request_messages_and_tool_outputs(body)
        prev_id = body.get("previous_response_id")

        base_messages: List[JSON] = []

        if current_tool_outputs:
            prev_state = None
            if prev_id:
                prev_state = self.state.get(str(prev_id))
            if not prev_state:
                prev_state = self.state.find_by_call_ids([m.get("tool_call_id") for m in current_tool_outputs])
            if not prev_state:
                raise HistoryRepairError(
                    "Received function_call_output but could not find a stored response by previous_response_id or call_id"
                )

            # Repair and continue from the completed assistant->tool exchange.
            base_messages = repair_chat_history(prev_state.messages, current_tool_outputs)
            base_messages = merge_new_user_messages(base_messages, new_messages)
            # Preserve the previous upstream model if Codex omits model consistency.
            model_upstream = map_model(model_alias or prev_state.model_alias, self.model_map)

        elif prev_id:
            prev_state = self.state.get(str(prev_id))
            if prev_state:
                base_messages = repair_chat_history(prev_state.messages, None)
                base_messages = merge_new_user_messages(base_messages, new_messages)
            else:
                base_messages = new_messages
        else:
            base_messages = new_messages

        if not base_messages:
            base_messages = [{"role": "user", "content": ""}]

        # Tool definitions.
        converted_tools, reverse_name_map = convert_responses_tools(body.get("tools", []))
        if self.strip_tools:
            converted_tools = []
            reverse_name_map = {}

        # Add a system guard to discourage parallel tool calls. This is safer than relying
        # on tool_choice/parallel_tool_calls, which some DeepSeek endpoints reject.
        if self.force_single_tool and converted_tools:
            guard = {
                "role": "system",
                "content": (
                    "For this coding-agent session, call at most one tool per assistant response. "
                    "After receiving a tool result, continue with either a final answer or one next tool call. "
                    "Do not make parallel tool calls."
                ),
            }
            # Place after original system/developer messages but before user content when possible.
            insert_at = 0
            while insert_at < len(base_messages) and base_messages[insert_at].get("role") in ("system", "developer"):
                insert_at += 1
            base_messages = base_messages[:insert_at] + [guard] + base_messages[insert_at:]

        payload: JSON = {
            "model": model_upstream,
            "messages": base_messages,
            "stream": False,
        }

        if converted_tools:
            payload["tools"] = converted_tools

        # Pass only conservative generation parameters.
        for src, dst in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("max_output_tokens", "max_tokens"),
            ("max_tokens", "max_tokens"),
            ("presence_penalty", "presence_penalty"),
            ("frequency_penalty", "frequency_penalty"),
        ):
            if src in body and body[src] is not None:
                payload[dst] = body[src]

        # DeepSeek V4 thinking mode rejects tool_choice; many providers also dislike
        # Responses-only params. Do not forward tool_choice/parallel_tool_calls/store/include.
        return payload, base_messages, model_alias, model_upstream, reverse_name_map

    def build_response_object(
        self,
        body: JSON,
        chat_resp: JSON,
        base_messages: List[JSON],
        model_alias: str,
        model_upstream: str,
        reverse_name_map: Dict[str, str],
    ) -> JSON:
        choice = (chat_resp.get("choices") or [{}])[0]
        upstream_msg = choice.get("message") or {}

        content = as_text(upstream_msg.get("content", ""))
        reasoning_content = upstream_msg.get("reasoning_content") or upstream_msg.get("reasoning")
        thinking_blocks = upstream_msg.get("thinking_blocks")

        tool_calls_in = upstream_msg.get("tool_calls") or []
        tool_calls_out: List[JSON] = []

        for tc in tool_calls_in:
            if not isinstance(tc, dict):
                continue
            tc_id = str(tc.get("id") or tc.get("call_id") or new_id("call"))
            fn = tc.get("function") or {}
            raw_name = str(fn.get("name") or tc.get("name") or "tool")
            name = restore_tool_name(raw_name, reverse_name_map)
            args = fn.get("arguments", tc.get("arguments", "{}"))
            if not isinstance(args, str):
                args = json_dumps(args)
            # Store chat-format name as returned by the provider for replay; expose original name to Codex.
            replay_tc = {
                "id": tc_id,
                "type": "function",
                "function": {"name": raw_name, "arguments": args},
            }
            tool_calls_out.append({"replay": replay_tc, "codex": {"id": new_id("fc"), "call_id": tc_id, "name": name, "arguments": args}})

        assistant_msg: JSON = {"role": "assistant", "content": content or ""}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            assistant_msg["thinking_blocks"] = thinking_blocks
        if tool_calls_out:
            assistant_msg["tool_calls"] = [x["replay"] for x in tool_calls_out]

        response_id = new_id("resp")
        all_messages = repair_chat_history(base_messages, None) + [assistant_msg]
        pending_ids = [x["codex"]["call_id"] for x in tool_calls_out]

        self.state.put(
            StoredResponse(
                response_id=response_id,
                model_alias=model_alias,
                model_upstream=model_upstream,
                messages=all_messages,
                pending_call_ids=pending_ids,
                created_at=now(),
            )
        )

        output: List[JSON] = []
        # Do not expose raw reasoning_content. Keep it in private proxy state only.
        # If Codex wants a reasoning item shape, provide an empty summary-only item.
        if reasoning_content and os.getenv("EXPOSE_EMPTY_REASONING_ITEM", "1") != "0":
            output.append({"type": "reasoning", "id": new_id("rs"), "summary": []})

        for x in tool_calls_out:
            fc = x["codex"]
            output.append(
                {
                    "type": "function_call",
                    "id": fc["id"],
                    "call_id": fc["call_id"],
                    "name": fc["name"],
                    "arguments": fc["arguments"],
                    "status": "completed",
                }
            )

        if content:
            output.append(
                {
                    "type": "message",
                    "id": new_id("msg"),
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }
            )

        usage = chat_resp.get("usage") or {}
        resp_obj: JSON = {
            "id": response_id,
            "object": "response",
            "created_at": now(),
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": body.get("instructions"),
            "model": model_alias,
            "output": output,
            "parallel_tool_calls": False,
            "previous_response_id": body.get("previous_response_id"),
            "store": False,
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            "truncation": body.get("truncation", "disabled"),
            "usage": {
                "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
                "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
                "total_tokens": usage.get("total_tokens", 0),
            },
            "metadata": body.get("metadata") or {},
        }
        return resp_obj


APP = ProxyApp()


class Handler(BaseHTTPRequestHandler):
    server_version = "ResponsesChatProxy/2.0"

    def _send_json(self, status: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error_obj(self, status: int, message: str, typ: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": typ, "code": typ}})

    def do_GET(self) -> None:  # noqa: N802
        if not APP.auth_ok(self.headers.get("Authorization", "")):
            self._send_error_obj(401, "Unauthorized", "unauthorized")
            return

        if self.path.rstrip("/") in ("/v1/models", "/models"):
            status, data, ctype = APP.forward_models()
            self._send_bytes(status, data, ctype)
            return

        if self.path.rstrip("/") in ("/health", "/v1/health"):
            self._send_json(200, {"ok": True, "service": "responses-chat-proxy", "time": now()})
            return

        self._send_error_obj(404, f"Unknown path: {self.path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        if not APP.auth_ok(self.headers.get("Authorization", "")):
            self._send_error_obj(401, "Unauthorized", "unauthorized")
            return

        if self.path.rstrip("/") not in ("/v1/responses", "/responses"):
            self._send_error_obj(404, f"Unknown path: {self.path}", "not_found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send_error_obj(400, f"Invalid JSON: {e}")
            return

        try:
            payload, base_messages, model_alias, model_upstream, reverse_name_map = APP.prepare_chat_payload(body)
            APP.log(
                "request",
                model_alias=model_alias,
                model_upstream=model_upstream,
                messages=len(payload.get("messages", [])),
                tools=len(payload.get("tools", [])),
                stream=bool(body.get("stream")),
            )
            try:
                chat_resp = APP.call_upstream_chat(payload)
                model_used = model_upstream
            except UpstreamError as first_err:
                fallbacks = APP.fallback_model_map.get(model_upstream) or APP.fallback_model_map.get(model_alias) or []
                chat_resp = None
                model_used = model_upstream
                if first_err.status in (408, 409, 429, 500, 502, 503, 504):
                    for fb in fallbacks:
                        fb_payload = dict(payload)
                        fb_payload["model"] = map_model(str(fb), APP.model_map)
                        try:
                            APP.log("fallback_attempt", from_model=model_upstream, to_model=fb_payload["model"], status=first_err.status)
                            chat_resp = APP.call_upstream_chat(fb_payload)
                            model_used = fb_payload["model"]
                            break
                        except UpstreamError as fb_err:
                            APP.log("fallback_failed", model=fb_payload["model"], status=fb_err.status, body=fb_err.body[:300])
                            first_err = fb_err
                if chat_resp is None:
                    raise first_err

            resp_obj = APP.build_response_object(body, chat_resp, base_messages, model_alias, model_used, reverse_name_map)

            if body.get("stream"):
                self._send_sse(resp_obj)
            else:
                self._send_json(200, resp_obj)

            # Opportunistic cleanup after successful requests.
            if time.time() % 10 < 1:
                APP.state.cleanup()

        except HistoryRepairError as e:
            APP.log("history_repair_error", error=str(e))
            self._send_error_obj(400, str(e))
        except UpstreamError as e:
            APP.log("upstream_error", status=e.status, body=e.body[:500])
            status = e.status if 400 <= e.status < 600 else 502
            # Preserve upstream rate-limit details for Codex/user visibility.
            try:
                parsed = json.loads(e.body)
            except Exception:
                parsed = {"error": {"message": e.body, "type": "upstream_error"}}
            self._send_json(status, parsed)
        except Exception as e:
            APP.log("proxy_crash", error=str(e), trace=traceback.format_exc())
            self._send_error_obj(500, f"Proxy internal error: {e}", "internal_error")

    def _write_sse(self, event: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_sse(self, resp_obj: JSON) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        self._write_sse("response.created", {"type": "response.created", "response": {**resp_obj, "output": []}})

        for idx, item in enumerate(resp_obj.get("output", [])):
            self._write_sse(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": idx, "item": item},
            )

            if item.get("type") == "message":
                content = item.get("content") or []
                if content:
                    part = content[0]
                    self._write_sse(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "output_index": idx,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                            "item_id": item.get("id"),
                        },
                    )
                    text = as_text(part.get("text", ""))
                    # Chunk fake streaming so clients that expect deltas see deltas.
                    chunk_size = int(os.getenv("SSE_CHUNK_SIZE", "256"))
                    for start in range(0, len(text), chunk_size):
                        delta = text[start : start + chunk_size]
                        self._write_sse(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "output_index": idx,
                                "content_index": 0,
                                "delta": delta,
                                "item_id": item.get("id"),
                            },
                        )
                    self._write_sse(
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "output_index": idx,
                            "content_index": 0,
                            "text": text,
                            "item_id": item.get("id"),
                        },
                    )
                    self._write_sse(
                        "response.content_part.done",
                        {
                            "type": "response.content_part.done",
                            "output_index": idx,
                            "content_index": 0,
                            "part": part,
                            "item_id": item.get("id"),
                        },
                    )

            elif item.get("type") == "function_call":
                args = item.get("arguments", "")
                self._write_sse(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": idx,
                        "item_id": item.get("id"),
                        "delta": args,
                    },
                )
                self._write_sse(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": idx,
                        "item_id": item.get("id"),
                        "arguments": args,
                    },
                )

            self._write_sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": idx, "item": item},
            )

        self._write_sse("response.completed", {"type": "response.completed", "response": resp_obj})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def run_self_test() -> None:
    # Completed exchange followed by pending exchange. Current tool output should
    # preserve the completed prefix and complete only the pending assistant.
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "List files"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "think1",
            "tool_calls": [{"id": "call_ls", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_ls", "content": "README.md\nDESIGN.md"},
        {"role": "assistant", "content": "Repo has README and DESIGN."},
        {"role": "user", "content": "Read README"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "think2",
            "tool_calls": [{"id": "call_read", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        },
        # Invalid nested tail that should be ignored.
        {"role": "assistant", "content": "bad nested assistant"},
    ]
    current = [{"role": "tool", "tool_call_id": "call_read", "content": "README contents"}]
    repaired = repair_chat_history(history, current)
    assert repaired[0]["role"] == "system"
    assert any(m.get("content") == "Repo has README and DESIGN." for m in repaired), repaired
    assert repaired[-1]["role"] == "tool" and repaired[-1]["tool_call_id"] == "call_read"
    assert any(m.get("reasoning_content") == "think2" for m in repaired), repaired
    assert not any(m.get("content") == "bad nested assistant" for m in repaired), repaired

    # Orphan tool output should fail.
    try:
        repair_chat_history([{"role": "user", "content": "x"}], [{"role": "tool", "tool_call_id": "missing", "content": "x"}])
        raise AssertionError("Expected orphan tool output to fail")
    except HistoryRepairError:
        pass

    # Multiple tool calls must all be satisfied.
    history2 = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "one", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "two", "arguments": "{}"}},
            ],
        }
    ]
    try:
        repair_chat_history(history2, [{"role": "tool", "tool_call_id": "a", "content": "A"}])
        raise AssertionError("Expected incomplete multiple tool outputs to stop/raise")
    except HistoryRepairError:
        # The repair function reaches final pending_now unmatched check.
        pass

    print("self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("PROXY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PROXY_PORT", "4000")))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    if not os.getenv("OPENCODE_GO_API_KEY"):
        print("warning: OPENCODE_GO_API_KEY is not set; upstream calls will fail", file=sys.stderr)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: binding to a non-localhost host; do not expose this proxy publicly", file=sys.stderr)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    def shutdown(signum, frame):
        print("shutting down", file=sys.stderr)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Responses->Chat proxy listening on http://{args.host}:{args.port}/v1", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
