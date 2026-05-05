#!/usr/bin/env python3
"""
responses_chat_proxy_v6.py

A small, dependency-free OpenAI Responses API -> OpenAI-compatible Chat Completions
bridge with true upstream streaming, designed for Codex custom model providers that need to call OpenCode Go OSS
models such as deepseek-v4-pro and kimi-k2.6. v6 also handles accidental GPT model aliases so
the bridge does not silently forward gpt-5.x requests to OpenCode Go.

What this proxy does:
- Accepts POST /v1/responses from Codex.
- Converts Responses input items to Chat Completions messages.
- Converts Responses function tools to Chat Completions function tools.
- Drops unsupported hosted/MCP/namespace tool types by default.
- Tracks response state and repairs orphan function_call_output turns.
- Preserves provider reasoning_content in stored assistant messages.
- Preserves valid completed assistant->tool exchanges instead of truncating context.
- Emits normal JSON or live Responses SSE events back to Codex.
- Streams upstream Chat Completions SSE live and converts deltas into Responses SSE.
- Assembles streamed tool calls/content/reasoning into stored assistant state.
- Forwards GET /v1/models to the upstream provider.
- Optionally passes GPT-family model requests through to OpenAI Responses API or aliases them to an OSS model.

Intended upstream:
  https://opencode.ai/zen/go/v1/chat/completions

Required env:
  OPENCODE_GO_API_KEY

Recommended env:
  PROXY_API_KEY or LITELLM_MASTER_KEY   # key Codex sends to this local proxy
  PROXY_PORT=4000
  PROXY_STATE_DB=/tmp/opencode_responses_proxy_state.sqlite3

Optional GPT-family model handling:
  GPT_MODEL_STRATEGY=error|oss|openai   # default: error
  GPT_MODEL_OSS_FALLBACK=deepseek-v4-pro
  OPENAI_API_KEY                        # required only for GPT_MODEL_STRATEGY=openai
  OPENAI_BASE_URL=https://api.openai.com/v1

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

SINGLE_TOOL_GUARD_TEXT = (
    "For this coding-agent session, call at most one tool per assistant response. "
    "After receiving a tool result, continue with either a final answer or one next tool call. "
    "Do not make parallel tool calls."
)


def is_single_tool_guard(msg: JSON) -> bool:
    return msg.get("role") == "system" and as_text(msg.get("content", "")) == SINGLE_TOOL_GUARD_TEXT


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

    # Many OpenAI-compatible Chat Completions providers do not support the
    # newer Responses/Chat "developer" role. Treat it as a system message
    # rather than forwarding an unsupported role upstream.
    if role == "developer":
        role = "system"

    out: JSON = {"role": role}
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


class ClientDisconnected(Exception):
    """Raised when Codex closes the SSE connection before we finish writing."""
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


def is_gpt_model(model: str) -> bool:
    m = (model or "").lower().strip()
    if m.startswith("openai/"):
        m = m.split("/", 1)[1]
    return m.startswith("gpt-") or m.startswith("gpt_")


class UnsupportedBridgeModel(Exception):
    def __init__(self, model: str, message: Optional[str] = None):
        self.model = model
        super().__init__(message or f"Model {model!r} is not served by the OpenCode Go bridge")


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
        self.gpt_model_strategy = os.getenv("GPT_MODEL_STRATEGY", "error").strip().lower()
        self.gpt_oss_fallback = os.getenv("GPT_MODEL_OSS_FALLBACK", "deepseek-v4-pro").strip()
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.openai_responses_url = f"{self.openai_base}/responses"
        self.timeout = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "900"))
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
        self.upstream_streaming = os.getenv("UPSTREAM_STREAM", "1") != "0"

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

    def route_gpt_model_for_chat_bridge(self, model_alias: str) -> Optional[str]:
        """Return an OSS upstream model for a GPT alias, or raise/error according to strategy.

        This solves the Codex gotcha where setting `model_provider=opencode_bridge`
        as the session-wide provider can cause Codex to send gpt-5.x helper/orchestrator
        requests to the same bridge. OpenCode Go cannot serve those models directly.
        """
        if not is_gpt_model(model_alias):
            return None

        # Explicit MODEL_MAP_JSON wins. This lets callers define precise aliases such as:
        #   {"gpt-5.5":"deepseek-v4-pro","gpt-5.4-mini":"deepseek-v4-flash"}
        if model_alias in self.model_map:
            return map_model(model_alias, self.model_map)

        if self.gpt_model_strategy in ("oss", "alias", "alias_to_oss", "opencode"):
            fallback = map_model(self.gpt_oss_fallback, self.model_map)
            self.log("gpt_alias_routed_to_oss", model_alias=model_alias, upstream=fallback)
            return fallback

        if self.gpt_model_strategy in ("openai", "passthrough", "pass_through"):
            # Handled before Chat payload preparation. If it reaches here, fail loudly.
            raise UnsupportedBridgeModel(model_alias, f"GPT model {model_alias!r} must be handled by OpenAI passthrough, not OpenCode chat conversion")

        raise UnsupportedBridgeModel(
            model_alias,
            "Codex sent a GPT-family model to the OpenCode Go bridge. "
            "Do not set model_provider=opencode_bridge as the parent session provider, "
            "or set GPT_MODEL_STRATEGY=oss for compatibility testing, "
            "or set GPT_MODEL_STRATEGY=openai with OPENAI_API_KEY for passthrough."
        )

    def should_passthrough_openai(self, model_alias: str) -> bool:
        return is_gpt_model(model_alias) and self.gpt_model_strategy in ("openai", "passthrough", "pass_through")

    def call_openai_responses(self, body: JSON) -> Tuple[int, bytes, str]:
        if not self.openai_key:
            raise UpstreamError(500, "OPENAI_API_KEY is not set but GPT_MODEL_STRATEGY=openai was requested")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.openai_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/6.0",
            "Accept": "application/json",
        }
        req = urllib.request.Request(self.openai_responses_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), resp.headers.get_content_type()
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            raise UpstreamError(e.code, body_bytes.decode("utf-8", errors="replace"), {k: v for k, v in e.headers.items()})
        except urllib.error.URLError as e:
            raise UpstreamError(502, f"OpenAI passthrough network error: {e}")

    def openai_responses_stream(self, body: JSON):
        if not self.openai_key:
            raise UpstreamError(500, "OPENAI_API_KEY is not set but GPT_MODEL_STRATEGY=openai was requested")
        stream_body = dict(body)
        stream_body["stream"] = True
        data = json.dumps(stream_body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.openai_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/6.0",
            "Accept": "text/event-stream, application/json",
        }
        req = urllib.request.Request(self.openai_responses_url, data=data, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            raise UpstreamError(e.code, body_bytes.decode("utf-8", errors="replace"), {k: v for k, v in e.headers.items()})
        except urllib.error.URLError as e:
            raise UpstreamError(502, f"OpenAI passthrough network error: {e}")

    def call_upstream_chat(self, payload: JSON) -> JSON:
        if not self.upstream_key:
            raise UpstreamError(500, "OPENCODE_GO_API_KEY is not set")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/6.0",
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

    def iter_upstream_chat_stream(self, payload: JSON):
        """
        Yield parsed upstream Chat Completions streaming chunks.

        Yields tuples:
          ("chunk", parsed_json)
          ("complete", parsed_json) when upstream returns normal JSON despite stream=True
          ("done", None) when [DONE] is seen or the SSE body ends cleanly
        """
        if not self.upstream_key:
            raise UpstreamError(500, "OPENCODE_GO_API_KEY is not set")

        stream_payload = dict(payload)
        stream_payload["stream"] = True
        data = json.dumps(stream_payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/6.0",
            "Accept": "text/event-stream, application/json",
        }

        req = urllib.request.Request(self.upstream_chat_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "application/json" in ctype and "event-stream" not in ctype:
                    body = resp.read().decode("utf-8", errors="replace")
                    yield ("complete", json.loads(body))
                    return

                data_lines: List[str] = []

                def flush_event() -> Optional[str]:
                    nonlocal data_lines
                    if not data_lines:
                        return None
                    value = "\n".join(data_lines).strip()
                    data_lines = []
                    return value

                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line == "":
                        value = flush_event()
                        if value is None:
                            continue
                        if value == "[DONE]":
                            yield ("done", None)
                            return
                        try:
                            yield ("chunk", json.loads(value))
                        except json.JSONDecodeError:
                            self.log("upstream_stream_bad_json", data=value[:500])
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    # Ignore event:/id:/comment lines from upstream.

                value = flush_event()
                if value and value != "[DONE]":
                    try:
                        yield ("chunk", json.loads(value))
                    except json.JSONDecodeError:
                        self.log("upstream_stream_bad_json", data=value[:500])
                yield ("done", None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            hdrs = {k: v for k, v in e.headers.items()}
            raise UpstreamError(e.code, body, hdrs)
        except urllib.error.URLError as e:
            raise UpstreamError(502, f"network error: {e}")

    def forward_models(self) -> Tuple[int, bytes, str]:
        if not self.upstream_key:
            return 500, b'{"error":{"message":"OPENCODE_GO_API_KEY is not set"}}', "application/json"
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "User-Agent": "codex-opencode-go-responses-proxy/6.0",
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
        gpt_routed = self.route_gpt_model_for_chat_bridge(model_alias)
        model_upstream = gpt_routed if gpt_routed else map_model(model_alias, self.model_map)

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
            # Preserve the previous upstream model if Codex omits model consistency;
            # keep GPT alias routing consistent when using compatibility mode.
            routed_prev = self.route_gpt_model_for_chat_bridge(model_alias or prev_state.model_alias)
            model_upstream = routed_prev if routed_prev else map_model(model_alias or prev_state.model_alias, self.model_map)

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
        if self.force_single_tool and converted_tools and not any(is_single_tool_guard(m) for m in base_messages):
            guard = {"role": "system", "content": SINGLE_TOOL_GUARD_TEXT}
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

    def build_response_shell(
        self,
        body: JSON,
        model_alias: str,
        response_id: Optional[str] = None,
        created_at: Optional[int] = None,
        status: str = "in_progress",
        output: Optional[List[JSON]] = None,
        error: Optional[JSON] = None,
    ) -> JSON:
        return {
            "id": response_id or new_id("resp"),
            "object": "response",
            "created_at": created_at or now(),
            "status": status,
            "error": error,
            "incomplete_details": None,
            "instructions": body.get("instructions"),
            "model": model_alias,
            "output": output or [],
            "parallel_tool_calls": False,
            "previous_response_id": body.get("previous_response_id"),
            "store": False,
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            "truncation": body.get("truncation", "disabled"),
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "metadata": body.get("metadata") or {},
        }

    def build_response_object(
        self,
        body: JSON,
        chat_resp: JSON,
        base_messages: List[JSON],
        model_alias: str,
        model_upstream: str,
        reverse_name_map: Dict[str, str],
        response_id: Optional[str] = None,
        created_at: Optional[int] = None,
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

        response_id = response_id or new_id("resp")
        created_at = created_at or now()
        all_messages = repair_chat_history(base_messages, None) + [assistant_msg]
        pending_ids = [x["codex"]["call_id"] for x in tool_calls_out]

        self.state.put(
            StoredResponse(
                response_id=response_id,
                model_alias=model_alias,
                model_upstream=model_upstream,
                messages=all_messages,
                pending_call_ids=pending_ids,
                created_at=created_at,
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
            "created_at": created_at,
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



@dataclass
class ToolStreamState:
    index: int
    item_id: str
    call_id: str
    raw_name_parts: List[str]
    args_parts: List[str]
    output_index: int = -1
    added: bool = False
    done: bool = False

    @property
    def raw_name(self) -> str:
        return "".join(self.raw_name_parts).strip()

    @property
    def arguments(self) -> str:
        return "".join(self.args_parts)


class ChatStreamAssembler:
    """Convert live Chat Completions SSE chunks into Responses SSE events."""

    def __init__(
        self,
        handler: "Handler",
        body: JSON,
        base_messages: List[JSON],
        model_alias: str,
        model_upstream: str,
        reverse_name_map: Dict[str, str],
        response_id: str,
        created_at: int,
    ):
        self.handler = handler
        self.body = body
        self.base_messages = base_messages
        self.model_alias = model_alias
        self.model_upstream = model_upstream
        self.reverse_name_map = reverse_name_map
        self.response_id = response_id
        self.created_at = created_at
        self.output_order: List[Tuple[str, Any]] = []
        self.next_output_index = 0

        self.text_started = False
        self.text_done = False
        self.text_item_id = new_id("msg")
        self.text_parts: List[str] = []

        self.reasoning_parts: List[str] = []
        self.thinking_blocks: List[Any] = []
        self.tool_states: Dict[int, ToolStreamState] = {}
        self.usage: JSON = {}
        self.last_progress = time.time()

    def _write_sse(self, event: str, data: Any) -> None:
        self.handler._write_sse(event, data)
        self.last_progress = time.time()

    def maybe_progress(self, note: str = "upstream_stream") -> None:
        interval = float(os.getenv("SSE_VISIBLE_PROGRESS_SECONDS", "8"))
        if time.time() - self.last_progress >= interval:
            self.handler._write_in_progress(self.response_id, self.model_alias, self.created_at, self.body, note)
            self.last_progress = time.time()

    def _assign_output_index(self, kind: str, key: Any) -> int:
        idx = self.next_output_index
        self.next_output_index += 1
        self.output_order.append((kind, key))
        return idx

    def ensure_text_item(self) -> int:
        if self.text_started:
            # Find existing output index.
            for i, (kind, key) in enumerate(self.output_order):
                if kind == "message" and key == "text":
                    return i
        idx = self._assign_output_index("message", "text")
        self.text_started = True
        item = {
            "type": "message",
            "id": self.text_item_id,
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self._write_sse("response.output_item.added", {"type": "response.output_item.added", "output_index": idx, "item": item})
        self._write_sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "output_index": idx,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
                "item_id": self.text_item_id,
            },
        )
        return idx

    def on_content_delta(self, text: str) -> None:
        if not text:
            return
        idx = self.ensure_text_item()
        chunk_size = int(os.getenv("SSE_CHUNK_SIZE", "256"))
        for start in range(0, len(text), chunk_size):
            delta = text[start : start + chunk_size]
            self.text_parts.append(delta)
            self._write_sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": idx,
                    "content_index": 0,
                    "delta": delta,
                    "item_id": self.text_item_id,
                },
            )

    def on_reasoning_delta(self, text: str) -> None:
        if text:
            self.reasoning_parts.append(text)
        # Do not expose raw reasoning text. Emit occasional progress so Codex does not
        # treat a long hidden-thinking phase as a dead stream.
        self.maybe_progress("hidden_reasoning")

    def _get_tool_state(self, index: int) -> ToolStreamState:
        if index not in self.tool_states:
            self.tool_states[index] = ToolStreamState(
                index=index,
                item_id=new_id("fc"),
                call_id=new_id("call"),
                raw_name_parts=[],
                args_parts=[],
            )
        return self.tool_states[index]

    def _ensure_tool_added(self, st: ToolStreamState, force: bool = False) -> None:
        if st.added:
            return
        if not st.raw_name and not force:
            return
        raw_name = st.raw_name or "tool"
        name = restore_tool_name(raw_name, self.reverse_name_map)
        st.output_index = self._assign_output_index("function_call", st.index)
        item = {
            "type": "function_call",
            "id": st.item_id,
            "call_id": st.call_id,
            "name": name,
            "arguments": "",
            "status": "in_progress",
        }
        self._write_sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": st.output_index, "item": item},
        )
        st.added = True

    def _emit_args_delta(self, st: ToolStreamState, delta: str) -> None:
        if not delta:
            return
        self._ensure_tool_added(st, force=bool(st.raw_name))
        if not st.added:
            # Buffer until name arrives.
            return
        arg_chunk_size = int(os.getenv("SSE_FUNCTION_ARGS_CHUNK_SIZE", "512"))
        for start in range(0, len(delta), arg_chunk_size):
            part = delta[start : start + arg_chunk_size]
            self._write_sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": st.output_index,
                    "item_id": st.item_id,
                    "delta": part,
                },
            )

    def on_tool_call_delta(self, tc: JSON) -> None:
        try:
            idx = int(tc.get("index", 0))
        except Exception:
            idx = 0
        st = self._get_tool_state(idx)
        if tc.get("id") and st.call_id.startswith("call_"):
            # Prefer upstream tool_call id if we have not already exposed a generated id.
            if not st.added:
                st.call_id = str(tc["id"])
        fn = tc.get("function") or {}
        name_part = fn.get("name") or tc.get("name")
        if name_part:
            st.raw_name_parts.append(str(name_part))
            self._ensure_tool_added(st, force=False)
            # If arguments arrived before the name, flush them now as one delta stream.
            if st.added and st.arguments:
                already_emitted_key = "_already_emitted_chars"
                emitted = getattr(st, already_emitted_key, 0)
                remaining = st.arguments[emitted:]
                if remaining:
                    self._emit_args_delta(st, remaining)
                    setattr(st, already_emitted_key, len(st.arguments))

        args_delta = fn.get("arguments", tc.get("arguments"))
        if args_delta is not None:
            args_text = args_delta if isinstance(args_delta, str) else json_dumps(args_delta)
            before = len(st.arguments)
            st.args_parts.append(args_text)
            if st.added:
                self._emit_args_delta(st, args_text)
                setattr(st, "_already_emitted_chars", len(st.arguments))
            else:
                # Preserve for later flush when the name arrives. Nothing has been
                # emitted yet, so keep the emitted counter at zero.
                if not hasattr(st, "_already_emitted_chars"):
                    setattr(st, "_already_emitted_chars", 0)

    def on_chunk(self, chunk: JSON) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            # Provider-specific hidden reasoning fields.
            for key in ("reasoning_content", "reasoning", "thinking"):
                if delta.get(key):
                    self.on_reasoning_delta(as_text(delta.get(key)))
            if delta.get("thinking_blocks"):
                self.thinking_blocks.append(delta.get("thinking_blocks"))
                self.maybe_progress("hidden_thinking_blocks")
            if delta.get("content") is not None:
                self.on_content_delta(as_text(delta.get("content")))
            for tc in delta.get("tool_calls") or []:
                if isinstance(tc, dict):
                    self.on_tool_call_delta(tc)
            if choice.get("finish_reason"):
                self.maybe_progress(f"finish:{choice.get('finish_reason')}")

    def _final_message_item(self) -> Optional[JSON]:
        if not self.text_started:
            return None
        text = "".join(self.text_parts)
        return {
            "type": "message",
            "id": self.text_item_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }

    def _final_function_item(self, st: ToolStreamState) -> JSON:
        raw_name = st.raw_name or "tool"
        name = restore_tool_name(raw_name, self.reverse_name_map)
        return {
            "type": "function_call",
            "id": st.item_id,
            "call_id": st.call_id,
            "name": name,
            "arguments": st.arguments,
            "status": "completed",
        }

    def finalize(self) -> JSON:
        # Force-add and finish any tool calls that only became complete at stream end.
        for idx in sorted(self.tool_states):
            st = self.tool_states[idx]
            self._ensure_tool_added(st, force=True)
            if not st.done:
                args = st.arguments
                emitted = getattr(st, "_already_emitted_chars", 0)
                if emitted < len(args):
                    self._emit_args_delta(st, args[emitted:])
                    setattr(st, "_already_emitted_chars", len(args))
                self._write_sse(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": st.output_index,
                        "item_id": st.item_id,
                        "arguments": args,
                    },
                )
                self._write_sse(
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": st.output_index, "item": self._final_function_item(st)},
                )
                st.done = True

        if self.text_started and not self.text_done:
            text = "".join(self.text_parts)
            idx = next(i for i, (kind, key) in enumerate(self.output_order) if kind == "message" and key == "text")
            self._write_sse(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "output_index": idx,
                    "content_index": 0,
                    "text": text,
                    "item_id": self.text_item_id,
                },
            )
            part = {"type": "output_text", "text": text, "annotations": []}
            self._write_sse(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "output_index": idx,
                    "content_index": 0,
                    "part": part,
                    "item_id": self.text_item_id,
                },
            )
            self._write_sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": idx, "item": self._final_message_item()},
            )
            self.text_done = True

        output_by_index: Dict[int, JSON] = {}
        for idx, (kind, key) in enumerate(self.output_order):
            if kind == "message":
                item = self._final_message_item()
                if item:
                    output_by_index[idx] = item
            elif kind == "function_call":
                st = self.tool_states[int(key)]
                output_by_index[idx] = self._final_function_item(st)
        output = [output_by_index[i] for i in sorted(output_by_index)]

        content = "".join(self.text_parts)
        reasoning_content = "".join(self.reasoning_parts)
        assistant_msg: JSON = {"role": "assistant", "content": content or ""}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if self.thinking_blocks:
            assistant_msg["thinking_blocks"] = self.thinking_blocks

        replay_tool_calls: List[JSON] = []
        for idx in sorted(self.tool_states):
            st = self.tool_states[idx]
            replay_tool_calls.append(
                {
                    "id": st.call_id,
                    "type": "function",
                    "function": {"name": st.raw_name or "tool", "arguments": st.arguments},
                }
            )
        if replay_tool_calls:
            assistant_msg["tool_calls"] = replay_tool_calls

        all_messages = repair_chat_history(self.base_messages, None) + [assistant_msg]
        APP.state.put(
            StoredResponse(
                response_id=self.response_id,
                model_alias=self.model_alias,
                model_upstream=self.model_upstream,
                messages=all_messages,
                pending_call_ids=[tc["id"] for tc in replay_tool_calls],
                created_at=self.created_at,
            )
        )

        usage = self.usage or {}
        resp_obj = APP.build_response_shell(
            self.body,
            self.model_alias,
            response_id=self.response_id,
            created_at=self.created_at,
            status="completed",
            output=output,
        )
        resp_obj["usage"] = {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
            "total_tokens": usage.get("total_tokens", 0),
        }
        return resp_obj

class Handler(BaseHTTPRequestHandler):
    server_version = "ResponsesChatProxy/5.0"

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
            raw_model_alias = str(body.get("model") or "")
            if APP.should_passthrough_openai(raw_model_alias):
                APP.log("openai_passthrough", model_alias=raw_model_alias, stream=bool(body.get("stream")))
                if body.get("stream"):
                    self._forward_openai_sse(body)
                else:
                    status, data, ctype = APP.call_openai_responses(body)
                    self._send_raw(status, data, ctype)
                return

            payload, base_messages, model_alias, model_upstream, reverse_name_map = APP.prepare_chat_payload(body)
            APP.log(
                "request",
                model_alias=model_alias,
                model_upstream=model_upstream,
                messages=len(payload.get("messages", [])),
                tools=len(payload.get("tools", [])),
                stream=bool(body.get("stream")),
            )

            if body.get("stream"):
                self._send_sse_with_upstream(body, payload, base_messages, model_alias, model_upstream, reverse_name_map)
            else:
                chat_resp, model_used = self._call_upstream_with_fallback(payload, model_alias, model_upstream)
                resp_obj = APP.build_response_object(body, chat_resp, base_messages, model_alias, model_used, reverse_name_map)
                self._send_json(200, resp_obj)

            # Opportunistic cleanup after successful requests.
            if time.time() % 10 < 1:
                APP.state.cleanup()

        except UnsupportedBridgeModel as e:
            APP.log("unsupported_bridge_model", model=e.model, error=str(e))
            self._send_error_obj(400, str(e), "unsupported_bridge_model")
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

    def _call_upstream_with_fallback(self, payload: JSON, model_alias: str, model_upstream: str) -> Tuple[JSON, str]:
        try:
            return APP.call_upstream_chat(payload), model_upstream
        except UpstreamError as first_err:
            fallbacks = APP.fallback_model_map.get(model_upstream) or APP.fallback_model_map.get(model_alias) or []
            if first_err.status in (408, 409, 429, 500, 502, 503, 504):
                for fb in fallbacks:
                    fb_payload = dict(payload)
                    fb_payload["model"] = map_model(str(fb), APP.model_map)
                    try:
                        APP.log("fallback_attempt", from_model=model_upstream, to_model=fb_payload["model"], status=first_err.status)
                        return APP.call_upstream_chat(fb_payload), fb_payload["model"]
                    except UpstreamError as fb_err:
                        APP.log("fallback_failed", model=fb_payload["model"], status=fb_err.status, body=fb_err.body[:300])
                        first_err = fb_err
            raise first_err

    def _send_sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # This is a finite SSE response. Default to closing the TCP connection after
        # [DONE] so each Codex tool turn starts with a fresh connection instead of
        # relying on HTTP keep-alive reuse across many write-operation turns.
        self.send_header("Connection", os.getenv("SSE_CONNECTION", "close"))
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_raw(self, status: int, data: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Connection", os.getenv("SSE_CONNECTION", "close"))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def _forward_openai_sse(self, body: JSON) -> None:
        try:
            upstream = APP.openai_responses_stream(body)
        except UpstreamError as e:
            APP.log("openai_passthrough_error", status=e.status, body=e.body[:500])
            status = e.status if 400 <= e.status < 600 else 502
            try:
                parsed = json.loads(e.body)
            except Exception:
                parsed = {"error": {"message": e.body, "type": "openai_passthrough_error"}}
            self._send_json(status, parsed)
            return

        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.headers.get("Content-Type") or "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", os.getenv("SSE_CONNECTION", "close"))
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            with upstream:
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    self._safe_write(chunk)
        except ClientDisconnected:
            APP.log("client_disconnected", phase="openai_passthrough")
        finally:
            self.close_connection = True

    def _safe_write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise ClientDisconnected(str(e)) from e

    def _write_sse_comment(self, text: str = "keepalive") -> None:
        self._safe_write(f": {text} {now()}\n\n".encode("utf-8"))

    def _write_in_progress(self, response_id: str, model_alias: str, created_at: int, body: JSON, note: str = "upstream_wait") -> None:
        # Codex appears to reset its stream timer more reliably on real SSE events
        # than on comment-only heartbeats. Emit a legal progress event with the same
        # response id while the upstream Chat Completions request is still running.
        shell = APP.build_response_shell(
            body,
            model_alias,
            response_id=response_id,
            created_at=created_at,
            status="in_progress",
            output=[],
        )
        meta = dict(shell.get("metadata") or {})
        meta["proxy_note"] = note
        shell["metadata"] = meta
        self._write_sse("response.in_progress", {"type": "response.in_progress", "response": shell})

    def _send_sse_with_upstream(
        self,
        body: JSON,
        payload: JSON,
        base_messages: List[JSON],
        model_alias: str,
        model_upstream: str,
        reverse_name_map: Dict[str, str],
    ) -> None:
        response_id = new_id("resp")
        created_at = now()
        shell = APP.build_response_shell(body, model_alias, response_id=response_id, created_at=created_at, status="in_progress")

        self._send_sse_headers()
        self._write_sse("response.created", {"type": "response.created", "response": shell})

        # If upstream streaming is disabled, fall back to the v4 fake-stream path.
        if not APP.upstream_streaming:
            return self._send_sse_with_upstream_buffered(body, payload, base_messages, model_alias, model_upstream, reverse_name_map, response_id, created_at)

        event_q: "queue.Queue[Tuple[str, Any, Optional[str]]]" = queue.Queue(maxsize=int(os.getenv("STREAM_QUEUE_MAXSIZE", "256")))
        stop_event = threading.Event()

        def worker() -> None:
            attempts: List[Tuple[str, JSON]] = [(model_upstream, dict(payload))]
            for fb in APP.fallback_model_map.get(model_upstream, []) + APP.fallback_model_map.get(model_alias, []):
                fb_model = map_model(str(fb), APP.model_map)
                if fb_model != model_upstream:
                    fb_payload = dict(payload)
                    fb_payload["model"] = fb_model
                    attempts.append((fb_model, fb_payload))

            last_err: Optional[UpstreamError] = None
            for attempt_idx, (attempt_model, attempt_payload) in enumerate(attempts):
                yielded_any = False
                try:
                    APP.log("upstream_stream_start", model=attempt_model, response_id=response_id, attempt=attempt_idx + 1)
                    for kind, value in APP.iter_upstream_chat_stream(attempt_payload):
                        if stop_event.is_set():
                            return
                        if kind == "chunk":
                            yielded_any = True
                            event_q.put(("chunk", value, attempt_model))
                        elif kind == "complete":
                            event_q.put(("complete", value, attempt_model))
                            return
                        elif kind == "done":
                            event_q.put(("done", None, attempt_model))
                            return
                    event_q.put(("done", None, attempt_model))
                    return
                except UpstreamError as e:
                    last_err = e
                    transient = e.status in (408, 409, 429, 500, 502, 503, 504)
                    if yielded_any or not transient or attempt_idx >= len(attempts) - 1:
                        event_q.put(("error", e, attempt_model))
                        return
                    APP.log("stream_fallback_attempt", from_model=attempt_model, status=e.status, body=e.body[:300])
                    continue
                except Exception as e:
                    event_q.put(("error", e, attempt_model))
                    return
            if last_err:
                event_q.put(("error", last_err, model_upstream))

        threading.Thread(target=worker, daemon=True).start()

        assembler = ChatStreamAssembler(
            self,
            body,
            base_messages,
            model_alias,
            model_upstream,
            reverse_name_map,
            response_id,
            created_at,
        )
        heartbeat_s = float(os.getenv("SSE_UPSTREAM_HEARTBEAT_SECONDS", "5"))
        actual_model_used = model_upstream

        while True:
            try:
                kind, value, used_model = event_q.get(timeout=heartbeat_s)
            except queue.Empty:
                try:
                    if os.getenv("SSE_HEARTBEAT_EVENT", "1") != "0":
                        self._write_in_progress(response_id, model_alias, created_at, body, "upstream_stream_wait")
                    if os.getenv("SSE_HEARTBEAT_COMMENT", "0") == "1":
                        self._write_sse_comment("upstream_stream_wait")
                except ClientDisconnected:
                    stop_event.set()
                    APP.log("client_disconnected", response_id=response_id, phase="upstream_stream_wait")
                    return
                continue

            if used_model:
                actual_model_used = used_model
                assembler.model_upstream = used_model

            try:
                if kind == "chunk":
                    assembler.on_chunk(value)
                    continue
                if kind == "complete":
                    # Upstream ignored stream=True and returned a normal Chat Completion.
                    resp_obj = APP.build_response_object(
                        body,
                        value,
                        base_messages,
                        model_alias,
                        actual_model_used,
                        reverse_name_map,
                        response_id=response_id,
                        created_at=created_at,
                    )
                    self._emit_sse_items_and_completed(resp_obj)
                    return
                if kind == "done":
                    resp_obj = assembler.finalize()
                    completed_response = resp_obj
                    if os.getenv("SSE_COMPACT_COMPLETED_FOR_TOOL_CALLS", "0") == "1":
                        completed_response = self._compact_completed_response(resp_obj)
                    self._write_sse("response.completed", {"type": "response.completed", "response": completed_response})
                    self._safe_write(b"data: [DONE]\n\n")
                    self.close_connection = True
                    return
                if kind == "error":
                    raise value
            except ClientDisconnected:
                stop_event.set()
                APP.log("client_disconnected", response_id=response_id, phase=f"stream_{kind}")
                return
            except Exception as e:
                # Headers are committed; surface failure as Responses SSE.
                try:
                    if isinstance(e, UpstreamError):
                        message = e.body[:1000]
                        typ = "upstream_error"
                        APP.log("upstream_error", status=e.status, body=e.body[:500])
                    else:
                        message = f"Proxy internal error: {e}"
                        typ = "internal_error"
                        APP.log("proxy_stream_error", error=str(e), trace="".join(traceback.format_exception(type(e), e, e.__traceback__)))
                    failed = APP.build_response_shell(
                        body,
                        model_alias,
                        response_id=response_id,
                        created_at=created_at,
                        status="failed",
                        error={"message": message, "type": typ, "code": typ},
                    )
                    self._write_sse("response.failed", {"type": "response.failed", "response": failed})
                    self._safe_write(b"data: [DONE]\n\n")
                except ClientDisconnected:
                    APP.log("client_disconnected", response_id=response_id, phase="stream_error_emit")
                self.close_connection = True
                return

    def _send_sse_with_upstream_buffered(
        self,
        body: JSON,
        payload: JSON,
        base_messages: List[JSON],
        model_alias: str,
        model_upstream: str,
        reverse_name_map: Dict[str, str],
        response_id: str,
        created_at: int,
    ) -> None:
        result_q: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                chat_resp, model_used = self._call_upstream_with_fallback(payload, model_alias, model_upstream)
                resp_obj = APP.build_response_object(
                    body,
                    chat_resp,
                    base_messages,
                    model_alias,
                    model_used,
                    reverse_name_map,
                    response_id=response_id,
                    created_at=created_at,
                )
                result_q.put(("ok", resp_obj))
            except Exception as e:
                result_q.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()
        heartbeat_s = float(os.getenv("SSE_UPSTREAM_HEARTBEAT_SECONDS", "5"))
        while True:
            try:
                kind, value = result_q.get(timeout=heartbeat_s)
                break
            except queue.Empty:
                try:
                    if os.getenv("SSE_HEARTBEAT_EVENT", "1") != "0":
                        self._write_in_progress(response_id, model_alias, created_at, body, "upstream_wait")
                    if os.getenv("SSE_HEARTBEAT_COMMENT", "0") == "1":
                        self._write_sse_comment("upstream_wait")
                except ClientDisconnected:
                    APP.log("client_disconnected", response_id=response_id, phase="upstream_wait")
                    return

        if kind == "ok":
            try:
                self._emit_sse_items_and_completed(value)
            except ClientDisconnected:
                APP.log("client_disconnected", response_id=response_id, phase="emit_completed")
            return
        err = value
        try:
            if isinstance(err, UpstreamError):
                message = err.body[:1000]
                typ = "upstream_error"
                APP.log("upstream_error", status=err.status, body=err.body[:500])
            else:
                message = f"Proxy internal error: {err}"
                typ = "internal_error"
                APP.log("proxy_crash", error=str(err), trace="".join(traceback.format_exception(type(err), err, err.__traceback__)))
            failed = APP.build_response_shell(body, model_alias, response_id=response_id, created_at=created_at, status="failed", error={"message": message, "type": typ, "code": typ})
            self._write_sse("response.failed", {"type": "response.failed", "response": failed})
            self._safe_write(b"data: [DONE]\n\n")
        except ClientDisconnected:
            APP.log("client_disconnected", response_id=response_id, phase="emit_failed")
        self.close_connection = True

    def _write_sse(self, event: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self._safe_write(f"event: {event}\n".encode("utf-8") + f"data: {payload}\n\n".encode("utf-8"))

    def _compact_completed_response(self, resp_obj: JSON) -> JSON:
        compact = dict(resp_obj)
        compact_output = []
        for item in resp_obj.get("output", []):
            if item.get("type") == "function_call":
                tiny = dict(item)
                tiny["arguments"] = ""
                compact_output.append(tiny)
            else:
                compact_output.append(item)
        compact["output"] = compact_output
        return compact

    def _send_sse(self, resp_obj: JSON) -> None:
        self._send_sse_headers()
        self._write_sse("response.created", {"type": "response.created", "response": {**resp_obj, "output": []}})
        self._emit_sse_items_and_completed(resp_obj)

    def _emit_sse_items_and_completed(self, resp_obj: JSON) -> None:
        for idx, item in enumerate(resp_obj.get("output", [])):
            item_type = item.get("type")
            item_for_added = item

            # For large write operations the function-call arguments can contain a
            # whole patch or shell script. Sending that full JSON blob in
            # output_item.added, delta, done, output_item.done, and response.completed
            # creates huge SSE frames. Real Responses streams usually accumulate
            # arguments through delta events and only finalize at done. Keep added
            # lightweight and chunk argument deltas.
            if item_type == "function_call":
                item_for_added = dict(item)
                item_for_added["arguments"] = ""
                item_for_added["status"] = "in_progress"

            self._write_sse(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": idx, "item": item_for_added},
            )

            if item_type == "message":
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

            elif item_type == "function_call":
                args = item.get("arguments", "")
                if not isinstance(args, str):
                    args = json_dumps(args)
                arg_chunk_size = int(os.getenv("SSE_FUNCTION_ARGS_CHUNK_SIZE", "1024"))
                APP.log(
                    "emit_function_call",
                    response_id=resp_obj.get("id"),
                    output_index=idx,
                    item_id=item.get("id"),
                    call_id=item.get("call_id"),
                    name=item.get("name"),
                    args_chars=len(args),
                    arg_chunk_size=arg_chunk_size,
                )
                for start in range(0, len(args), arg_chunk_size):
                    delta = args[start : start + arg_chunk_size]
                    self._write_sse(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": idx,
                            "item_id": item.get("id"),
                            "delta": delta,
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

        completed_response = resp_obj
        if os.getenv("SSE_COMPACT_COMPLETED_FOR_TOOL_CALLS", "0") == "1":
            completed_response = self._compact_completed_response(resp_obj)

        self._write_sse("response.completed", {"type": "response.completed", "response": completed_response})
        self._safe_write(b"data: [DONE]\n\n")
        self.close_connection = True


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

    assert is_gpt_model("gpt-5.5")
    assert is_gpt_model("openai/gpt-5.4-mini")
    assert not is_gpt_model("ocg-deepseek-v4-pro")

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

    shutdown_started = threading.Event()

    def shutdown(signum, frame):
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        try:
            sys.stderr.write("shutting down\n")
            sys.stderr.flush()
        except Exception:
            pass
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Responses->Chat proxy listening on http://{args.host}:{args.port}/v1", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
