#!/usr/bin/env python3
"""
responses_chat_proxy_v11.py

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
  UPSTREAM_API_KEY or OPENCODE_GO_API_KEY

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
import hashlib
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple

from codex_oss.handoff import (
    empty_task_envelope as _empty_task_envelope,
    structured_handoff_to_envelope as _structured_handoff_to_envelope,
)

from codex_oss.read_evidence import (
    build_bounded_finalizer_prompt,
    log_finalizer_attempt,
    parse_read_narrative_draft,
    validate_read_narrative_draft,
    build_model_authored_read_report,
    build_canonical_read_evidence,
    build_read_report_skeleton,
    persist_read_artifacts,
)

from codex_oss.visible_commentary import VisibleCommentarySink

# tool_call_adoption probes loaded on-demand via codex_oss.tool_call_adoption

JSON = Dict[str, Any]
BRIDGE_VERSION = "12.0"

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
    output_items_json: str = "[]"
    tool_exchange_count: int = 0  # v10: tracks turn count for budget enforcement
    task_max_exchanges: int = 1  # v10: per-task-class budget
    read_ledger_json: str = ""  # v10: comma-sep read paths
    command_ledger_json: str = ""  # v10: pipe-sep commands
    previous_response_id: str = ""
    pending_replay_count: int = 0
    adoption_probes_json: str = ""  # v11: JSON-serialized ToolCallAdoptionProbeV1 list


def adoption_state_machine_from_response(stored: StoredResponse) -> "ResponsesToolStateMachine":
    """Deserialize adoption state machine from StoredResponse, or create new."""
    from codex_oss.tool_call_adoption import ResponsesToolStateMachine
    sm = ResponsesToolStateMachine(stored.response_id)
    if stored.adoption_probes_json:
        try:
            data = json.loads(stored.adoption_probes_json)
            if isinstance(data, dict) and data.get("calls"):
                for call_id, call_state in data["calls"].items():
                    sm.calls[call_id] = dict(call_state)
                sm.sequence = data.get("sequence", 0)
        except Exception:
            pass
    sm.parent_response_id = stored.previous_response_id or None
    return sm


def adoption_state_machine_to_response(sm: "ResponsesToolStateMachine", stored: StoredResponse) -> None:
    """Serialize adoption state machine back to StoredResponse."""
    stored.adoption_probes_json = json.dumps({
        "calls": {
            call_id: {
                k: v for k, v in state.items()
                if k not in ("arguments_preview",)
            }
            for call_id, state in sm.calls.items()
        },
        "sequence": sm.sequence,
    }, sort_keys=True)


# ── v10: Task-class budgets ──

TASK_CLASS_BUDGETS = {
    "single_read": 2,
    "scout": 6,
    "prep_report": 8,
    "review": 6,
    "docs_support": 4,
    "bounded_write": 3,
    "bounded_test_write": 4,
    "implementation": 5,
    "proof_critical": 0,
}

DEFAULT_TASK_BUDGET = 1

TASK_CLASS_KEYWORDS = {
    "scout": ["scout", "explor", "navigation", "find", "map", "search", "inspect", "repo inspection"],
    "prep_report": ["prep", "prepare", "report", "read-only repo", "memory inspection", "prep report"],
    "review": ["review", "audit", "check", "diff"],
    "docs_support": ["docs", "documentation", "changelog", "summary", "summarize"],
    "bounded_write": ["write", "edit", "patch", "implement", "create", "add function"],
    "bounded_test_write": ["test", "add test", "test scaffolding"],
    "proof_critical": ["proof", "critical", "recovery", "finalizer", "certification", "publish"],
    "single_read": ["read", "say", "tell"],
}

SETUP_PATH_PREFIXES = [
    "/Users/",  # global skill paths
    ".codex/skills/",
]

TASK_FIELD_LABELS = (
    "ROLE", "GOAL", "TASK TYPE", "OWNED PATHS", "READ-ONLY PATHS",
    "ALLOWED PATHS", "DO NOT TOUCH", "FORBIDDEN", "PREREQUISITES",
    "RELEVANT CONVENTIONS", "VERIFICATION STEPS", "VERIFICATION",
    "DELIVERABLE", "COMPLETION RULE", "ESCALATION RULE",
)

PLACEHOLDER_PATH_MARKERS = (
    "<file", "<path", "<files", "files or dirs", "path/to/", "path_to_",
    "replace_me", "your_file", "example/path",
)


def _is_placeholder_path(path: str) -> bool:
    p = str(path or "").strip()
    if not p:
        return True
    lower = p.lower()
    if p.startswith("<") and p.endswith(">"):
        return True
    return any(marker in lower for marker in PLACEHOLDER_PATH_MARKERS)


def _clean_required_paths(paths: list) -> list:
    cleaned = []
    seen = set()
    for raw in paths or []:
        path = str(raw or "").strip()
        if not path or _is_placeholder_path(path):
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(path)
    return cleaned


def _extract_inline_file_paths(text: str) -> list:
    """Conservative fallback for unstructured direct-agent prompts."""
    candidates = []
    pattern = r"(?<![<\w/])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:md|py|txt|json|toml|ya?ml|js|jsx|ts|tsx|css|html)(?![>\w])"
    candidates.extend(re.findall(pattern, str(text or "")))
    bare_pattern = r"(?<![<\w/])(?:README|AGENTS|CHANGELOG|LICENSE)\.(?:md|txt)(?![>\w])"
    candidates.extend(re.findall(bare_pattern, str(text or ""), flags=re.IGNORECASE))
    clean = _clean_required_paths(candidates)
    positive: list[str] = []
    source = str(text or "")
    for path in clean:
        idx = source.find(path)
        if idx < 0:
            positive.append(path)
            continue
        window = source[max(0, idx - 80):idx].lower()
        neg = (
            "do not inspect" in window
            or "do not read" in window
            or "don't inspect" in window
            or "don't read" in window
            or "never inspect" in window
            or "never read" in window
            or "forbidden" in window
            or "not as task evidence" in source[idx:idx + len(path) + 120].lower()
        )
        if not neg:
            positive.append(path)
    return _clean_required_paths(positive)


def _extract_budget(messages: List[JSON]) -> int:
    """Extract task-class budget from OSS handoff text in messages."""
    handoff = ""
    for msg in messages:
        if msg.get("role") in ("system", "developer", "user") and msg.get("content"):
            handoff += " " + str(msg["content"])
    task_class = extract_task_class(handoff)
    return get_task_budget(task_class)


def extract_task_class(handoff_text: str) -> str:
    """Infer task class from OSS handoff text using keyword matching."""
    text_lower = handoff_text.lower()
    scores = {}
    for cls_name, keywords in TASK_CLASS_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[cls_name] = score
    if scores:
        return max(scores, key=scores.get)
    return "scout"


def get_task_budget(task_class: str) -> int:
    return TASK_CLASS_BUDGETS.get(task_class, DEFAULT_TASK_BUDGET)


def task_class_allows_writes(task_class: str) -> bool:
    return task_class in ("bounded_write", "bounded_test_write", "implementation")


# ── v10: OSS subagent runtime ──

@dataclass
class ReadLedger:
    paths: str = ""  # comma-separated paths read, stored in SQLite
    commands: str = ""  # commands executed

    def add_read(self, path: str) -> None:
        if path not in self.paths.split(","):
            self.paths = (self.paths + "," + path).strip(",")

    def add_command(self, cmd: str) -> None:
        if cmd not in self.commands.split("|"):
            self.commands = (self.commands + "|" + cmd).strip("|")

    def already_read(self, path: str) -> bool:
        return path in self.paths.split(",")

    def summary(self) -> str:
        parts = []
        p = [x for x in self.paths.split(",") if x]
        c = [x for x in self.commands.split("|") if x]
        if p: parts.append("Already inspected: " + ", ".join(p))
        if c: parts.append("Commands run: " + ", ".join(c))
        return "\n".join(parts)


def extract_allowed_paths(handoff_text: str) -> list:
    """Extract explicit read paths from READ-ONLY PATHS or OWNED PATHS lines."""
    paths = []
    for label in ("READ-ONLY PATHS", "OWNED PATHS", "ALLOWED PATHS"):
        section = _extract_labeled_section(handoff_text, label)
        if section:
            parts = _parse_path_list(section)
            paths.extend([p for p in parts if p and not p.lower().startswith(("no ", "none", "do not", "git ")) and len(p) > 1])
            return _clean_required_paths(paths)
    for sep in ("READ-ONLY PATHS:", "OWNED PATHS:", "ALLOWED PATHS:"):
        if sep in handoff_text:
            after = _extract_segment(handoff_text, sep)
            parts = _parse_path_list(after)
            paths.extend([p for p in parts if p and not p.lower().startswith(("no ", "none", "do not", "git ")) and len(p) > 1])
            break
    return _clean_required_paths(paths)


def required_paths_from_envelope(envelope: dict, handoff_text: str) -> list:
    """Return required source paths from the parsed handoff, falling back to legacy labels."""
    for key in ("read_only_paths", "owned_paths", "allowed_paths"):
        paths = envelope.get(key)
        if isinstance(paths, list) and paths:
            cleaned = _clean_required_paths([str(p) for p in paths if str(p).strip()])
            if cleaned:
                return cleaned
    return extract_allowed_paths(handoff_text) or _extract_inline_file_paths(handoff_text)


def extract_required_deliverables(handoff_text: str) -> list:
    """Extract required output fields from DELIVERABLE lines."""
    section = _extract_labeled_section(handoff_text, "DELIVERABLE")
    if section:
        return [line.strip().strip("- ").strip() for line in section.splitlines() if line.strip()]

    fields = []
    capturing = False
    for line in handoff_text.split("\n"):
        line = line.strip()
        if "DELIVERABLE:" in line.upper():
            capturing = True
            after = line.split(":", 1)[1] if ":" in line else ""
            if after.strip():
                fields.append(after.strip())
            continue
        if capturing and line and not line.startswith(("#", "//", "- ")):
            # Stop at the next section marker
            if any(line.upper().startswith(kw) for kw in ("COMPLETION", "ESCALAT", "OWNED", "READ-ONLY", "DO NOT", "VERIFICA", "PREREQ", "RELEVANT", "RULES", "RETURN")):
                capturing = False
                continue
            fields.append(line.strip().lstrip("- ").strip())
    return fields


def normalize_tool_args(args, tool_name: str) -> tuple:
    """Extract normalized path and command from tool call arguments."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return (args, str(args)) if tool_name in ("rtk_read", "read") else (None, str(args))

    if not isinstance(args, dict):
        return (None, str(args))

    if tool_name in ("rtk_read", "read", "cat"):
        for key in ("path", "file_path", "filepath", "file", "filename", "target"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return (val.strip(), val.strip())
        # Check nested
        for nested_key in ("args", "input", "request"):
            nested = args.get(nested_key)
            if isinstance(nested, dict):
                p, _ = normalize_tool_args(nested, tool_name)
                if p:
                    return (p, str(nested))

    if any(token in tool_name.lower() for token in ("write", "edit", "patch", "apply_patch", "create")):
        for key in ("path", "file_path", "filepath", "file", "filename", "target"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return (val.strip(), val.strip())

    if tool_name in ("exec_command", "rtk_git", "git", "rtk_exec"):
        for key in ("command", "args", "cmd", "arguments"):
            val = args.get(key)
            if val:
                command = str(val) if isinstance(val, str) else json.dumps(val)
                write_op = _append_redirection_from_shell_command(command)
                write_path = write_op[0] if write_op else None
                return (_read_path_from_shell_command(command) or write_path, command)

    return (None, json.dumps(args))


def _shell_parts(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _strip_shell_redirection_parts(parts: list[str]) -> list[str]:
    """Drop shell redirection tokens so read-path normalization stays semantic."""
    cleaned: list[str] = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part in ("|", "||", "&&", ";"):
            break
        if part in (">", ">>", "<", "2>", "2>>", "1>", "1>>"):
            skip_next = True
            continue
        if re.match(r"^\d?>&\d+$", part) or re.match(r"^\d?>.*$", part) or re.match(r"^\d?<.*$", part):
            continue
        cleaned.append(part)
    return cleaned


def _read_path_from_shell_command(command: str) -> Optional[str]:
    if re.search(r"<[^>]*(?:file|files|dir|dirs|path|paths)[^>]*>", command, flags=re.IGNORECASE):
        return None
    parts = _strip_shell_redirection_parts(_shell_parts(command))
    if len(parts) >= 3 and parts[0] == "rtk" and parts[1] == "read":
        path = " ".join(parts[2:])
    elif len(parts) >= 2 and parts[0] == "cat":
        path = " ".join(parts[1:])
    elif len(parts) >= 4 and parts[0] == "rtk" and parts[1] == "grep":
        path = " ".join(parts[3:])
    elif len(parts) >= 3 and parts[0] == "rg":
        path = " ".join(parts[2:])
    elif len(parts) >= 3 and parts[0] == "grep":
        path = " ".join(parts[2:])
    elif len(parts) >= 3 and parts[0] == "sed" and parts[-1] != "-n":
        path = parts[-1]
    else:
        return None
    cwd = os.getcwd()
    if os.path.isabs(path) and path.startswith(cwd + os.sep):
        return os.path.relpath(path, cwd)
    return path


def _append_redirection_from_shell_command(command: str) -> Optional[tuple[str, str]]:
    """Extract a simple literal append operation from shell."""
    parts = _shell_parts(command)
    if "&&" in parts:
        parts = parts[:parts.index("&&")]
    if ";" in parts:
        parts = parts[:parts.index(";")]
    if len(parts) < 4 or parts[0] not in ("echo", "printf"):
        return None
    if ">>" not in parts:
        return None
    idx = parts.index(">>")
    if idx < 2 or idx + 1 >= len(parts):
        return None
    content = " ".join(parts[1:idx])
    path = parts[idx + 1]
    if not content or not path:
        return None
    if parts[0] == "printf":
        content = content.encode("utf-8").decode("unicode_escape")
        if content.endswith("\n"):
            return path, content
    return path, content + "\n"


def _shell_command_is_read_like(command: str) -> bool:
    parts = _shell_parts(command)
    if not parts:
        return False
    if parts[0] == "rtk" and len(parts) >= 2 and parts[1] in ("read", "grep", "find", "ls"):
        return True
    return parts[0] in ("cat", "rg", "grep", "sed", "find", "ls")


def _search_pattern_from_shell_command(command: str) -> str:
    parts = _strip_shell_redirection_parts(_shell_parts(command))
    if len(parts) >= 4 and parts[0] == "rtk" and parts[1] == "grep":
        return parts[2]
    if len(parts) >= 3 and parts[0] in ("rg", "grep"):
        idx = 1
        while idx < len(parts) and parts[idx].startswith("-"):
            idx += 1
        return parts[idx] if idx < len(parts) - 1 else ""
    if len(parts) >= 4 and parts[0] == "sed":
        match = re.search(r"/([^/]+)/p", " ".join(parts[1:-1]))
        return match.group(1) if match else ""
    return ""


def _verification_markers_from_handoff(handoff_text: str) -> list[str]:
    markers = []
    for token in re.findall(r"\b[A-Z][A-Z0-9_]{5,}\b", str(handoff_text or "")):
        if token in {"OSS_HANDOFF_JSON"}:
            continue
        if token not in markers:
            markers.append(token)
    return markers


def effective_tool_kind(tool_name: str, tool_args: str, current_kind: ToolKind) -> ToolKind:
    """Classify shell wrappers around read/search commands as read-like for closure."""
    if current_kind != "shell":
        return current_kind
    _, command = normalize_tool_args(tool_args, tool_name)
    if command and _append_redirection_from_shell_command(command):
        return "write"
    if command and _shell_command_is_read_like(command):
        return "read"
    return current_kind


def inject_evidence_ledger(messages: list, ledger: ReadLedger, required_paths: list,
                            required_fields: list, budget_remaining: int) -> list:
    """Inject progress context into the continuation payload."""
    remaining = [p for p in required_paths if not ledger.already_read(p)]
    summary = ledger.summary()
    lines = ["[EVIDENCE LEDGER]"]

    if summary:
        lines.append(summary)
    if remaining:
        lines.append(f"Still required: {', '.join(remaining)}")
    if required_fields:
        lines.append(f"Report must include: {', '.join(required_fields)}")
    lines.append(f"Tool budget remaining: {budget_remaining}")
    lines.append("Do not reread complete files unless the prior result was incomplete.")

    evidence_text = "\n".join(lines)

    # Insert after the last system message or at the beginning
    out = list(messages)
    insert_at = len(out)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") in ("system", "developer"):
            insert_at = i + 1
            break

    out.insert(insert_at, {"role": "system", "content": evidence_text})
    return out


def _extract_read_paths_from_history(messages: list) -> set:
    """Extract file paths already read from conversation history."""
    paths = set()
    for msg in messages:
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            if name in ("rtk_read", "read", "cat", "exec_command", "rtk_exec"):
                args = func.get("arguments", "{}")
                path, _ = normalize_tool_args(args, name)
                if path:
                    paths.add(path)
        # Also check codex-format tool calls
        codex_tc = msg.get("codex")
        if codex_tc and isinstance(codex_tc, dict):
            codex_name = codex_tc.get("name", "")
            if codex_name in ("rtk_read", "read", "cat", "exec_command", "rtk_exec"):
                path, _ = normalize_tool_args(codex_tc.get("arguments", "{}"), codex_name)
                if path:
                    paths.add(path)
    return paths


def _extract_completed_read_paths_from_history(messages: list) -> set:
    """Extract read-like paths whose assistant tool calls have matching tool results."""
    pending: Dict[str, str] = {}
    completed = set()
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            name = func.get("name", "")
            args = func.get("arguments", "{}")
            path, _ = normalize_tool_args(args, name)
            kind = effective_tool_kind(name, args, classify_tool_call_name(name))
            if path and kind == "read":
                call_id = tc.get("id") or tc.get("codex", {}).get("call_id")
                if call_id:
                    pending[str(call_id)] = path
        codex_tc = msg.get("codex")
        if codex_tc and isinstance(codex_tc, dict):
            name = codex_tc.get("name", "")
            args = codex_tc.get("arguments", "{}")
            path, _ = normalize_tool_args(args, name)
            kind = effective_tool_kind(name, args, classify_tool_call_name(name))
            call_id = codex_tc.get("call_id") or codex_tc.get("id")
            if path and kind == "read" and call_id:
                pending[str(call_id)] = path

        tool_call_id = msg.get("tool_call_id") or msg.get("call_id")
        if msg.get("role") in ("tool", "function") or msg.get("type") in ("function_call_output", "tool_result"):
            path = pending.get(str(tool_call_id or ""))
            if path:
                content = as_text(msg.get("content", msg.get("output", "")))
                if not tool_output_indicates_failure(content):
                    completed.add(path)
    return completed


def _count_tool_result_messages(messages: list) -> int:
    count = 0
    for msg in messages:
        role = msg.get("role")
        if role in ("tool", "function"):
            count += 1
            continue
        if msg.get("type") in ("function_call_output", "tool_result"):
            count += 1
    return count


def _extract_handoff_text(messages: list) -> str:
    """Extract the most likely current OSS handoff, not the whole history."""
    def _is_instruction_dump(text: str) -> bool:
        t = str(text or "")
        return (
            t.lstrip().startswith("# AGENTS.md instructions")
            or "<INSTRUCTIONS>" in t
            or "--- project-doc ---" in t
        )

    user_texts = [
        str(msg["content"])
        for msg in messages
        if msg.get("role") == "user" and msg.get("content")
    ]
    task_user_texts = [text for text in user_texts if not _is_instruction_dump(text)]
    search_texts = task_user_texts or user_texts
    for text in reversed(search_texts):
        if "OSS_HANDOFF_JSON" in text:
            return text
    for text in reversed(search_texts):
        if _extract_inline_file_paths(text):
            return text
    for text in reversed(search_texts):
        if _handoff_score(text) >= 1:
            return text

    best_text = ""
    best_score = 0
    for msg in messages:
        if msg.get("role") in ("system", "developer") and msg.get("content"):
            text = str(msg["content"])
            score = _handoff_score(text)
            if score >= best_score and score >= 2:
                best_text = text
                best_score = score
    if best_text:
        return best_text
    if user_texts:
        return user_texts[-1]
    return " ".join(
        str(msg["content"])
        for msg in messages
        if msg.get("role") in ("system", "developer", "user") and msg.get("content")
    )


def _handoff_score(text: str) -> int:
    upper = text.upper()
    return sum(1 for label in TASK_FIELD_LABELS if label in upper)


def _has_evidence_ledger(body: JSON) -> bool:
    """Check if the body already has an evidence ledger injected."""
    for item in body.get("input", []):
        if isinstance(item, dict) and "EVIDENCE LEDGER" in str(item.get("content", "")):
            return True
    return False


# ── v11: Task session + execution modes ──

@dataclass
class TaskSession:
    task_session_id: str
    root_response_id: str
    task_class: str
    execution_mode: str  # context_pack, managed_autonomy, bounded_write, escalate
    max_tool_exchanges: int
    tool_exchanges_used: int = 0
    duplicate_suppressions: int = 0
    required_paths: list = field(default_factory=list)
    required_commands: list = field(default_factory=list)
    read_paths: dict = field(default_factory=dict)
    commands_run: list = field(default_factory=list)
    required_outputs: list = field(default_factory=list)
    verification_steps: list = field(default_factory=list)  # v12: grep/read steps
    handoff_text: str = ""


def select_execution_mode(handoff_text: str) -> str:
    """Choose execution mode based on handoff content."""
    task_class = extract_task_class(handoff_text)
    paths = extract_allowed_paths(handoff_text) or _extract_inline_file_paths(handoff_text)
    # Paths are explicit known sources → context pack is best
    if task_class in ("prep_report", "scout") and len(paths) >= 2:
        return "context_pack"
    if task_class in ("bounded_write", "bounded_test_write", "implementation"):
        return "bounded_write"
    if task_class == "proof_critical":
        return "escalate"
    # Discovery tasks — model needs to search, not read known files
    if not paths:
        return "managed_autonomy"
    # Single known file → one read + finish
    return "context_pack" if len(paths) == 1 else "managed_autonomy"


def build_task_session(body: JSON, handoff_text: str, response_id: str) -> TaskSession:
    """Create a TaskSession from the handoff and initial response."""
    envelope = parse_task_envelope(handoff_text)
    task_class = envelope.get("task_type") or extract_task_class(handoff_text)
    mode = select_mode(envelope)
    paths = required_paths_from_envelope(envelope, handoff_text)
    if mode == "managed_autonomy" and paths:
        mode = "context_pack_report"
    fields = envelope.get("deliverable_fields") or extract_required_deliverables(handoff_text)
    budget = get_task_budget(task_class)

    # Extract required commands from verification steps
    cmd_lines = []
    if "OSS_HANDOFF_JSON" not in handoff_text:
        for line in handoff_text.split("\n"):
            if any(kw in line.upper() for kw in ("VERIFICATION", "GIT STATUS", "GIT REV-PARSE", "GIT LOG")):
                cmd_lines.append(line.strip())

    return TaskSession(
        task_session_id=new_id("tsk"),
        root_response_id=response_id,
        task_class=task_class,
        execution_mode=mode,
        max_tool_exchanges=budget if mode not in ("context_pack", "context_pack_report") else 0,
        required_paths=paths,
        required_commands=cmd_lines,
        verification_steps=envelope.get("verification_steps", []),
        required_outputs=fields,
        handoff_text=handoff_text,
    )


def _is_probable_search_term(term: str) -> bool:
    """Keep machine-ish scout tokens; drop prose like 'query snippets'."""
    if not term:
        return False
    lowered = term.lower().strip()
    if lowered in {
        "query snippets",
        "relevant hits",
        "exact files",
        "the run dir",
        "repo scripts/docs",
        "repo scripts/docs/run dir",
    }:
        return False
    if len(lowered) < 3:
        return False
    if re.search(r"[/:_.-]", term):
        return True
    if re.fullmatch(r"[0-9a-fA-F-]{12,}", term):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]*", term):
        return True
    return False


def _search_term_rank(term: str) -> tuple:
    lowered = term.lower()
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", lowered):
        return (0, -len(term), lowered)
    if any(ch in term for ch in (":", "_")):
        return (1, -len(term), lowered)
    if any(ch in term for ch in ("/", ".", "-")):
        return (2, -len(term), lowered)
    if len(term) <= 8:
        return (4, -len(term), lowered)
    return (3, -len(term), lowered)


def extract_search_terms_from_step(step: str) -> list:
    """Extract grep terms from structured scout prose without repo-specific rules."""
    step_lower = step.lower()
    if not any(word in step_lower for word in ("search", "find", "grep", "locate")):
        return []

    candidates = []
    quoted = re.findall(r"`([^`]+)`", step)
    candidates.extend(quoted)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_:\-]*", step):
        if any(ch in token for ch in ("_", ":", "-")) or token.isupper():
            candidates.append(token)

    for_match = re.search(r"\bfor\b\s+(.+)", step, flags=re.IGNORECASE)
    if for_match:
        tail = for_match.group(1)
        stop = re.search(
            r"\b(inspect|determine|return|include|identify|if\s+found|after\s+fixing)\b",
            tail,
            flags=re.IGNORECASE,
        )
        if stop:
            tail = tail[:stop.start()]
        tail = re.sub(r"\band\b", ",", tail, flags=re.IGNORECASE)
        candidates.extend(part.strip() for part in tail.split(","))

    seen = set()
    terms = []
    for candidate in candidates:
        term = candidate.strip().strip("`'\". ")
        term = re.sub(r"^(and|or)\s+", "", term, flags=re.IGNORECASE).strip()
        if not _is_probable_search_term(term):
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return sorted(terms, key=_search_term_rank)


def build_context_pack(session: TaskSession, project_root: str) -> str:
    """Gather all required sources, command-aware: grep instead of full file where specified."""
    sections = []
    files_fully_read = set()
    PACK_MAX = int(os.getenv("CONTEXT_PACK_MAX_CHARS", "24000"))
    total_chars = 0

    def _append_section(text: str):
        nonlocal total_chars
        sections.append(text)
        total_chars += len(text)

    # Parse verification steps into commands (grep, read) and paths
    cmd_requests: list = []  # (type, target, pattern)
    search_terms = []
    for step in session.verification_steps + session.required_commands:
        step_lower = step.lower().strip("- ").strip()
        # Match "grep PATTERN in FILENAME" or "grep PATTERN FILENAME"
        if step_lower.startswith("grep"):
            rest = step_lower.replace("grep ", "", 1).strip()
            if " in " in rest:
                parts = rest.split(" in ", 1)
                pattern = parts[0].strip()
                target = parts[1].strip()
            else:
                # "grep pattern filename" — last word is file
                parts = rest.rsplit(None, 1)
                pattern = parts[0].strip() if len(parts) > 1 else rest
                target = parts[1].strip() if len(parts) > 1 else ""
            if pattern and target:
                cmd_requests.append(("grep", target, pattern))
        for term in extract_search_terms_from_step(step):
            if term and term not in search_terms:
                search_terms.append(term)

    def _resolve_project_path(path: str) -> str:
        return _resolve_declared_read_path(path, project_root)

    # Process required paths
    for path in session.required_paths:
        # Check if any grep request targets this file (by filename match)
        grep_pattern = None
        for ct, target, pattern in cmd_requests:
            target_clean = target.strip().rstrip(".").lstrip("./")
            path_clean = path.strip().lstrip("./")
            if target_clean in path_clean or path_clean.endswith(target_clean) or target_clean == path_clean.split("/")[-1]:
                grep_pattern = pattern
                break

        try:
            full = _resolve_project_path(path)
            if grep_pattern:
                # Run grep instead of full read
                out = subprocess.run(
                    ["rtk", "grep", grep_pattern, path],
                    cwd=project_root, capture_output=True, text=True, timeout=15)
                files_fully_read.add(path)
                _append_section(
                    f"=== grep {grep_pattern} in {path} ===\n"
                    f"exit_code: {out.returncode}\n"
                    f"matched lines:\n{out.stdout[:4000]}\n")
                session.commands_run.append(["grep", grep_pattern, path])
                continue

            if os.path.isdir(full):
                files_fully_read.add(path)
                max_terms = int(os.getenv("CONTEXT_PACK_MAX_SEARCH_TERMS", "12"))
                terms = search_terms[:max_terms]
                if not terms:
                    terms = []
                if terms:
                    for term in terms:
                        out = subprocess.run(
                            ["rtk", "grep", term, path],
                            cwd=project_root, capture_output=True, text=True, timeout=20)
                        _append_section(
                            f"=== rtk grep {term} in {path} ===\n"
                            f"exit_code: {out.returncode}\n"
                            f"matched lines:\n{out.stdout[:5000]}\n"
                            f"stderr:\n{out.stderr[:1000]}\n")
                        session.commands_run.append(["grep", term, path])
                else:
                    out = subprocess.run(
                        ["rtk", "find", path, "-maxdepth", "2", "-type", "f"],
                        cwd=project_root, capture_output=True, text=True, timeout=20)
                    _append_section(
                        f"=== rtk find {path} -maxdepth 2 -type f ===\n"
                        f"exit_code: {out.returncode}\n{out.stdout[:5000]}\n{out.stderr[:1000]}\n")
                    session.commands_run.append(["find", path])
                continue

            if search_terms:
                max_terms = int(os.getenv("CONTEXT_PACK_MAX_SEARCH_TERMS", "12"))
                for term in search_terms[:max_terms]:
                    out = subprocess.run(
                        ["rtk", "grep", term, path],
                        cwd=project_root, capture_output=True, text=True, timeout=20)
                    _append_section(
                        f"=== rtk grep {term} in {path} ===\n"
                        f"exit_code: {out.returncode}\n"
                        f"matched lines:\n{out.stdout[:5000]}\n"
                        f"stderr:\n{out.stderr[:1000]}\n")
                    session.commands_run.append(["grep", term, path])

            raw = open(full, encoding="utf-8", errors="replace").read()
            files_fully_read.add(path)
            # Cap per file and globally
            per_file_max = min(8000, max(1000, PACK_MAX // max(len(session.required_paths), 1)))
            if len(raw) > per_file_max or total_chars + len(raw) > PACK_MAX:
                chunk = min(per_file_max, max(500, PACK_MAX - total_chars))
                _append_section(
                    f"=== {path} (TRUNCATED: {len(raw)} total, showing first {chunk}) ===\n"
                    f"{raw[:chunk]}\n[... truncated ...]\n")
            else:
                _append_section(f"=== {path} ({len(raw)} chars) ===\n{raw}\n")
        except Exception as e:
            _append_section(f"=== {path} ===\n[ERROR: {e}]\n")

    # Run git commands if requested
    for cmd_text in session.required_commands:
        cmd_text = cmd_text.strip().lstrip("- ").strip().strip(".")
        cmd_lower = cmd_text.lower()
        if cmd_lower.startswith("git ") or cmd_lower.startswith("rtk git "):
            try:
                parts = cmd_text.split()
                if parts[:2] == ["rtk", "git"]:
                    args = parts[2:]
                elif parts and parts[0] == "git":
                    args = parts[1:]
                else:
                    args = []
                if not args: continue
                out = subprocess.run(["rtk", "git"] + args, cwd=project_root,
                                     capture_output=True, text=True, timeout=15)
                _append_section(f"=== rtk git {' '.join(args)} ===\nexit_code: {out.returncode}\n{out.stdout}\n{out.stderr}\n")
                session.commands_run.append(args)
            except Exception as e:
                _append_section(f"=== rtk git ({cmd_text}) ===\n[ERROR: {e}]\n")

    session.required_paths = list(files_fully_read)
    return "\n".join(sections)


def validate_report(text: str, required_fields: list) -> tuple:
    """Check if the output is a valid report. Returns (is_valid, missing_fields)."""
    if len(text.strip()) < 50:
        return False, ["report_too_short"]
    if is_intent_or_status(text):
        return False, ["intent_or_status_detected"]
    if "Running the" in text and "startup" in text.lower():
        return False, ["startup_sentence_not_report"]
    missing = [f for f in required_fields if f.lower() not in text.lower()]
    return len(missing) == 0, missing


def _field_label(field: str) -> str:
    """Return a stable report label for a requested deliverable field."""
    label = str(field or "").strip().strip("- ").strip()
    if not label:
        return "deliverable"
    label = re.sub(r"\s+", "_", label.lower())
    label = re.sub(r"[^a-z0-9_/-]", "", label)
    return label.strip("_") or "deliverable"


def _is_exact_deliverable_field(field: str) -> bool:
    """Structured handoffs use short field tokens; prose deliverables do not."""
    text = str(field or "").strip()
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_/-]{0,80}", text))


def _format_requested_deliverable_sections(required_outputs: list) -> str:
    exact_fields = [_field_label(field) for field in required_outputs if _is_exact_deliverable_field(field)]
    if not exact_fields:
        return (
            "requested_deliverables:\n"
            "- PARTIAL: deterministic fallback preserved the requested deliverable text, "
            "but no model synthesis was available to complete the analysis."
        )
    sections = []
    for field in exact_fields:
        sections.append(
            f"{field}:\n"
            "- PARTIAL: deterministic source-pack fallback cannot certify this deliverable as complete. "
            "Review the evidence table before accepting the task result."
        )
    return "\n".join(sections)


def _requested_search_terms_from_envelope(envelope: dict) -> list:
    terms = []
    for step in envelope.get("verification_steps", []):
        for term in extract_search_terms_from_step(str(step)):
            if term not in terms:
                terms.append(term)
    return terms


def evaluate_evidence_coverage(envelope: dict, pack: str) -> tuple:
    """Check whether requested paths and search terms have evidence-pack coverage."""
    source = pack or ""
    missing = []
    covered_paths = []
    requested_paths = list(envelope.get("read_only_paths", []))
    for path in requested_paths:
        markers = (
            f"=== {path}",
            f" in {path} ===",
            f"rtk find {path}",
            f"path escapes project root: {path}",
        )
        if any(marker in source for marker in markers):
            covered_paths.append(path)
        else:
            missing.append(f"path:{path}")

    covered_terms = []
    requested_terms = _requested_search_terms_from_envelope(envelope)
    for term in requested_terms:
        if f"grep {term} " in source or f"grep {term} in " in source:
            covered_terms.append(term)
        else:
            missing.append(f"search:{term}")

    return not missing, missing, covered_paths, covered_terms


def format_evidence_coverage_section(envelope: dict, pack: str) -> str:
    complete, missing, covered_paths, covered_terms = evaluate_evidence_coverage(envelope, pack)
    requested_paths = list(envelope.get("read_only_paths", []))
    requested_terms = _requested_search_terms_from_envelope(envelope)
    status = "PASS" if complete else "PARTIAL"
    return (
        "evidence_coverage:\n"
        f"- status: {status}\n"
        f"- requested_paths: {', '.join(requested_paths) if requested_paths else 'none'}\n"
        f"- covered_paths: {', '.join(covered_paths) if covered_paths else 'none'}\n"
        f"- requested_search_terms: {', '.join(requested_terms) if requested_terms else 'none'}\n"
        f"- covered_search_terms: {', '.join(covered_terms) if covered_terms else 'none'}\n"
        f"- missing: {', '.join(missing) if missing else 'none'}"
    )


def build_context_pack_deterministic_report(session: TaskSession, pack: str, tool_output_text: str) -> str:
    """Build a terminal read report when the lightweight finalizer cannot synthesize."""
    source = pack or tool_output_text or ""
    source_lines = source.splitlines()

    grep_sections = []
    for idx, line in enumerate(source_lines):
        if line.startswith("=== rtk grep "):
            snippet = []
            for follow in source_lines[idx + 1: idx + 12]:
                if follow.startswith("==="):
                    break
                stripped = follow.strip()
                if stripped and not stripped.startswith(("exit_code:", "matched lines:", "stderr:")):
                    snippet.append(stripped)
                if len(snippet) >= 3:
                    break
            grep_sections.append((line.strip("= ").strip(), snippet))

    def _has_match(snippet: list) -> bool:
        if not snippet:
            return False
        text = "\n".join(snippet).lower()
        return "0 matches" not in text and "no matches" not in text

    def _section_snippet(heading: str, limit: int = 180) -> str:
        in_section = False
        snippets: List[str] = []
        for line in source_lines:
            stripped = line.strip()
            if stripped.lower().startswith(f"## {heading}".lower()):
                in_section = True
                continue
            if in_section and stripped.startswith("## "):
                break
            if in_section and stripped and not stripped.startswith("|") and not set(stripped) <= {"-", "|"}:
                item = stripped.strip("- ").strip()
                snippets.append(item)
                if len(snippets) >= 3 or len(" ".join(snippets)) >= limit:
                    break
        text = "; ".join(snippets).strip()
        if len(text) <= limit:
            return text
        clipped = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:")
        return clipped + "..."

    bullets = []
    if grep_sections:
        ordered_sections = (
            [section for section in grep_sections if _has_match(section[1])]
            + [section for section in grep_sections if not _has_match(section[1])]
        )
        for title, snippet in ordered_sections[:3]:
            if snippet:
                bullets.append(f"- {title}: " + " | ".join(snippet)[:220])
            else:
                bullets.append(f"- {title}: no matches in the gathered source pack")
    goal = _section_snippet("Goal")
    if goal:
        bullets.append(f"- Goal: the document says this test should {goal[0].lower() + goal[1:] if goal else goal}")
    success = _section_snippet("Success criteria", 220)
    if success:
        bullets.append(f"- Success criteria: the document lists planned checks, not observed results: {success}")
    meta = _section_snippet("Meta-validation", 220)
    if meta:
        bullets.append(f"- Meta-validation: the document says setup can be checked offline by {meta[0].lower() + meta[1:] if meta else meta}")

    evidence_lines = []
    for line in source_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("===") or stripped.startswith("[..."):
            continue
        if stripped.startswith(("# Napkin", "## Corrections", "|")):
            continue
        evidence_lines.append(stripped[:220])
        if len(evidence_lines) >= 3:
            break
    evidence = "\n".join(f"- {line}" for line in evidence_lines) or "- No source text was available in the gathered pack."
    while len(bullets) < 3:
        idx = len(bullets)
        fallback = evidence_lines[idx] if idx < len(evidence_lines) else "No additional source detail was available."
        bullets.append(f"- Source evidence: {fallback}")
    command = "rtk read " + ", ".join(session.required_paths) if session.required_paths else "rtk read"
    files = ", ".join(session.required_paths) if session.required_paths else "unknown"
    outputs = ", ".join(session.required_outputs) if session.required_outputs else "concise findings, confidence, caveats"
    summary = "\n".join(bullets[:3])
    deliverable_sections = _format_requested_deliverable_sections(session.required_outputs)
    parsed_envelope = parse_task_envelope(session.handoff_text) if session.handoff_text else {}
    coverage_envelope = {
        "read_only_paths": parsed_envelope.get("read_only_paths", session.required_paths),
        "verification_steps": parsed_envelope.get("verification_steps", session.verification_steps),
    }
    evidence_coverage = format_evidence_coverage_section(coverage_envelope, source)
    return (
        "PARTIAL\n"
        "Transport status: PASS\n"
        "Evidence-gathering status: PASS\n"
        "Synthesis status: SOURCE_PACK_RECOVERY\n"
        "Task status: PARTIAL\n"
        f"Command used: {command}\n"
        f"Files gathered: {files}\n"
        f"Summary:\n{summary}\n"
        f"Evidence snippets:\n{evidence}\n"
        f"Requested deliverable: {outputs}\n"
        f"{evidence_coverage}\n"
        f"{deliverable_sections}\n"
        "Confidence: MEDIUM\n"
        "Caveats: source-pack recovery produced this report because live model synthesis was unavailable or invalid; task completion is not certified."
    )


def suppress_duplicate_read(tool_call: dict, session: TaskSession) -> Optional[str]:
    """Return a synthetic observation for duplicate reads, or None if new."""
    path, _ = normalize_tool_args(tool_call.get("arguments", "{}"),
                                   tool_call.get("name", "unknown"))
    if not path or path not in session.read_paths:
        return None

    session.duplicate_suppressions += 1
    remaining = [p for p in session.required_paths if p not in session.read_paths]
    return (f"[ALREADY READ]\n"
            f"Path: {path}\n"
            f"Status: already inspected completely\n\n"
            f"Still required:\n"
            + "\n".join(f"- {r}" for r in remaining) +
            f"\n\nDo not request {path} again. Continue with next unread source "
            f"or return a partial report.")


# ── v12: Agent runtime — execution modes, intent rejection, bounded writes ──

EXECUTION_MODES = ("no_tool_exact", "context_pack_report", "managed_autonomy",
                    "bounded_write_exact", "bounded_write_patch", "escalate",
                    "invalid_handoff")

INTENT_PATTERNS = (
    r"\b(I am|I'm|I will|I'll|I.m going to|I am going to|Running|Starting|"
    r"About to|Next, I|Let me|I need to|I should|First, I|Now I)\b"
)

MIN_REPORT_LENGTH = 80

def parse_task_envelope(handoff_text: str) -> dict:
    """Parse structured task fields from OSS handoff text."""
    structured = _structured_handoff_to_envelope(handoff_text)
    if structured is not None:
        return structured
    envelope = _empty_task_envelope()
    current_field = None

    # Merge all lines but also handle single-line handoffs with multiple markers
    merged = " ".join(handoff_text.split("\n"))
    upper_all = merged.upper()

    # Extract fields by marker patterns
    _extract_field(envelope, merged, "ROLE:", "role")
    _extract_field(envelope, merged, "GOAL:", "goal")
    role_section = _extract_labeled_section(handoff_text, "ROLE")
    goal_section = _extract_labeled_section(handoff_text, "GOAL")
    task_type_section = _extract_labeled_section(handoff_text, "TASK TYPE")
    if role_section:
        envelope["role"] = role_section
    if goal_section:
        envelope["goal"] = goal_section
    if task_type_section:
        envelope["task_type"] = task_type_section
    elif "TASK TYPE:" in upper_all:
        envelope["task_type"] = _extract_after(merged, "TASK TYPE:")

    read_section = _extract_labeled_section(handoff_text, "READ-ONLY PATHS")
    owned_section = _extract_labeled_section(handoff_text, "OWNED PATHS")
    forbidden_section = _extract_labeled_section(handoff_text, "DO NOT TOUCH") or _extract_labeled_section(handoff_text, "FORBIDDEN")
    verification_section = _extract_labeled_section(handoff_text, "VERIFICATION STEPS") or _extract_labeled_section(handoff_text, "VERIFICATION")
    deliverable_section = _extract_labeled_section(handoff_text, "DELIVERABLE")

    if read_section:
        envelope["read_only_paths"] = _parse_path_list(read_section)
    elif "READ-ONLY PATHS:" in upper_all:
        envelope["read_only_paths"] = _parse_path_list(_extract_segment(merged, "READ-ONLY PATHS:"))
    if owned_section or "OWNED PATHS:" in upper_all:
        owned_paths = _parse_path_list(owned_section or _extract_segment(merged, "OWNED PATHS:"))
        envelope["owned_paths"] = owned_paths
        envelope["write_allowed"] = bool(owned_paths)
    if forbidden_section:
        envelope["forbidden_actions"] = _parse_path_list(forbidden_section)
    elif "DO NOT TOUCH:" in upper_all or "FORBIDDEN:" in upper_all:
        marker = "DO NOT TOUCH:" if "DO NOT TOUCH:" in merged else "FORBIDDEN:"
        envelope["forbidden_actions"] = _parse_path_list(_extract_segment(merged, marker))
    if verification_section:
        envelope["verification_steps"] = [s.strip().strip("- ") for s in verification_section.replace("\n", ",").split(",") if s.strip()]
    elif "VERIFICATION STEPS:" in upper_all or "VERIFICATION:" in upper_all:
        marker = "VERIFICATION STEPS:" if "VERIFICATION STEPS:" in merged else "VERIFICATION:"
        raw = _extract_segment(merged, marker)
        # Split by commas to get individual steps, but keep multi-word steps together
        envelope["verification_steps"] = [s.strip().strip("- ") for s in raw.split(",") if s.strip()]
    if deliverable_section:
        envelope["deliverable_fields"] = [s.strip().strip("- ") for s in deliverable_section.splitlines() if s.strip()]
    elif "DELIVERABLE:" in upper_all:
        raw = _extract_after(merged, "DELIVERABLE:")
        envelope["deliverable_fields"] = [raw.strip()] if raw else []

    if "EXACTLY" in upper_all or "EXACT STRING" in upper_all or "EXACT OUTPUT" in upper_all:
        # Only no_tool_exact if there are NO read paths to process
        # If READ-ONLY PATHS are present with deliverables, it's a context-pack task
        if not envelope.get("read_only_paths") and not envelope.get("deliverable_fields"):
            envelope["no_tools_required"] = True
        envelope["exact_content"] = _extract_exact_content(merged)
    task_type_lower = envelope.get("task_type", "").lower()
    if "proof-critical" in task_type_lower or "proof_critical" in task_type_lower:
        envelope["proof_critical"] = True

    return envelope


def _extract_exact_content(text: str) -> str:
    """Extract deterministic one-line content from bounded exact-write tasks."""
    patterns = (
        r"single line:\s*([^\.;]+)",
        r"exactly this(?: single)? line:\s*([^\.;]+)",
        r"exact string:\s*([^\.;]+)",
        r"exact output:\s*([^\.;]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("`'\"")
    return ""


def _is_field_label(line: str) -> bool:
    normalized = line.strip().strip(":").upper()
    return normalized in TASK_FIELD_LABELS


def _extract_labeled_section(text: str, label: str) -> str:
    """Extract a structured handoff section where the label may be on its own line."""
    lines = text.splitlines()
    label_upper = label.upper()
    captured: List[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_section and captured:
                captured.append("")
            continue
        upper = stripped.upper()
        if upper == label_upper or upper == f"{label_upper}:":
            in_section = True
            continue
        if upper.startswith(f"{label_upper}:"):
            after = stripped.split(":", 1)[1].strip()
            return after
        if in_section and _is_field_label(stripped):
            break
        if in_section:
            captured.append(stripped)
    return "\n".join(line for line in captured if line).strip()


def _extract_field(envelope: dict, text: str, marker: str, field: str):
    if marker in text:
        envelope[field] = _extract_after(text, marker)


def _extract_after(text: str, marker: str) -> str:
    text_upper = text.upper()
    idx = text_upper.find(marker.upper())
    if idx < 0:
        return ""
    return text[idx + len(marker):].strip()


def _extract_segment(text: str, marker: str) -> str:
    """Extract the segment after a marker, stopping at the next known marker."""
    after = _extract_after(text, marker)
    stop_markers = ("ROLE:", "GOAL:", "TASK TYPE:", "OWNED PATHS:", "READ-ONLY PATHS:",
                    "DO NOT TOUCH:", "FORBIDDEN:", "VERIFICATION STEPS:", "VERIFICATION:",
                    "DELIVERABLE:", "COMPLETION RULE:", "ESCALATION RULE:", "PREREQUISITES:",
                    "RELEVANT CONVENTIONS:")
    after_upper = after.upper()
    earliest = len(after)
    for sm in stop_markers:
        idx = after_upper.find(sm.upper())
        if 0 <= idx < earliest:
            earliest = idx
    # Only stop at a period followed by a space and uppercase letter
    # (which indicates a new sentence, not a file extension)
    for i, ch in enumerate(after):
        if ch == "." and i + 2 < len(after) and after[i+1] == " " and after[i+2].isupper():
            if i < earliest:
                earliest = i + 1  # include the period
            break
    if earliest < len(after):
        return after[:earliest].strip().rstrip(".")
    return after.strip().rstrip(".")


def _parse_path_list(line: str) -> list:
    after = line.split(":", 1)[1] if ":" in line else line
    parts = [p.strip().strip(",").strip("- ").strip() for p in after.replace(";", ",").replace("\n", ",").split(",")
            if p.strip() and not p.strip().lower().startswith(("no ", "none", "do not"))]
    normalized = []
    for p in parts:
        # Strip trailing sentence-boundary periods from filenames
        if p.endswith(".") and not p.endswith(".."):
            stem = p[:-1]
            # Only strip if it looks like a sentence boundary (uppercase next word would have followed)
            if "." in stem or any(stem.lower().endswith(ext) for ext in
                (".md", ".py", ".js", ".ts", ".toml", ".json", ".txt", ".yml", ".yaml", ".css", ".html")):
                p = stem
        # Normalize absolute paths
        if p.startswith("/"):
            cwd = os.getcwd()
            if p.startswith(cwd):
                rel = os.path.relpath(p, cwd)
                if not _is_placeholder_path(rel):
                    normalized.append(rel)
            else:
                if not _is_placeholder_path(p):
                    normalized.append(p)
        else:
            if not _is_placeholder_path(p):
                normalized.append(p)
    return _clean_required_paths(normalized)


def select_mode(envelope: dict) -> str:
    if envelope.get("schema_error"):
        return "invalid_handoff"
    if envelope.get("proof_critical"):
        return "escalate"
    if envelope.get("write_allowed") and envelope.get("owned_paths") and envelope.get("exact_content"):
        return "bounded_write_exact"
    if envelope.get("write_allowed") and envelope.get("owned_paths"):
        return "bounded_write_patch"
    if envelope.get("write_allowed"):
        return "invalid_handoff"
    if envelope.get("read_only_paths") and envelope.get("deliverable_fields"):
        return "context_pack_report"
    if envelope.get("read_only_paths"):
        return "context_pack_report"
    if envelope.get("no_tools_required"):
        return "no_tool_exact"
    return "managed_autonomy"


def legacy_direct_write_modes_enabled() -> bool:
    default = os.getenv("OSS_DIRECT_WRITE_HANDOFFS", "1")
    return os.getenv("OSS_LEGACY_DIRECT_WRITES", default).strip().lower() in {"1", "true", "yes", "on"}


def build_legacy_write_demoted_report(mode: str, envelope: Optional[dict] = None) -> str:
    """Terminal report for raw direct write handoffs when runtime write lanes are required."""
    envelope = envelope or {}
    if mode == "bounded_write_exact":
        detail = "raw OSS exact-write handoffs are disabled by default."
        safety = "No exact-content write was performed by the bridge."
    else:
        detail = "raw OSS write handoffs are disabled by default."
        safety = "No bridge-owned raw write closure was accepted."
    task_type = str(envelope.get("task_type") or "unknown").strip() or "unknown"
    owned = ", ".join(str(path) for path in envelope.get("owned_paths", []) if str(path).strip()) or "none"
    return (
        "PARTIAL\n"
        "Synthesis status: DETERMINISTIC_LEGACY_WRITE_DEMOTED\n"
        f"Reason: {detail}\n"
        f"Task type: {task_type}\n"
        f"Owned paths: {owned}\n"
        "Terminal authority: bridge_runtime\n"
        "Rejected terminal source: raw_model_progress_or_final_text\n"
        "Required path: use a MissionV1 A4/A5/A6 implementation mission so the runtime owns patch validation, apply, verification, rollback, and final status.\n"
        "Confidence: HIGH\n"
        f"Caveats: {safety}"
    )


def raw_write_handoff_demoted_report(handoff_text: str, legacy_enabled: Optional[bool] = None) -> str:
    """Return a deterministic fail-closed report for raw write/implementation handoffs."""
    if legacy_enabled is None:
        legacy_enabled = legacy_direct_write_modes_enabled()
    if legacy_enabled or not handoff_text:
        return ""
    envelope = parse_task_envelope(handoff_text)
    mode = select_mode(envelope)
    if mode not in ("bounded_write_exact", "bounded_write_patch"):
        return ""
    return build_legacy_write_demoted_report(mode, envelope)


def should_use_direct_agent_loop(mode: str, evidence_ledger_present: bool, enabled: bool = True) -> bool:
    """Return true when direct read-only utility work should stay in the live agent loop."""
    return bool(
        enabled
        and mode in ("context_pack", "context_pack_report")
        and not evidence_ledger_present
    )


def _path_satisfies_required_path(path: str, required_path: str) -> bool:
    if not path or not required_path:
        return False
    if path == required_path:
        return True
    cwd = os.getcwd()
    path_abs = path if os.path.isabs(path) else os.path.abspath(os.path.join(cwd, path))
    required_abs = required_path if os.path.isabs(required_path) else os.path.abspath(os.path.join(cwd, required_path))
    return path_abs == required_abs


def direct_loop_required_sources_satisfied(required_paths: list, read_paths: set, current_path: str) -> bool:
    if not required_paths:
        return False
    for required in required_paths:
        if any(_path_satisfies_required_path(path, required) for path in read_paths):
            continue
        if _path_satisfies_required_path(current_path, required):
            continue
        return False
    return True


def _path_set_contains(paths: set, target_path: str) -> bool:
    if not target_path:
        return False
    return any(_path_satisfies_required_path(path, target_path) for path in paths)


def _remaining_required_paths(required_paths: list, read_paths: set) -> list:
    remaining = []
    for required in required_paths or []:
        if not any(_path_satisfies_required_path(path, required) for path in read_paths):
            remaining.append(required)
    return remaining


def direct_loop_terminal_decision(
    required_paths: list,
    completed_read_paths: set,
    current_path: str,
    turn: int,
    max_exchanges: int,
) -> tuple:
    """Return (decision, remaining_paths) for direct read-loop continuation safety."""
    evidence_paths = set(completed_read_paths or set())
    current_repeats_completed = _path_set_contains(evidence_paths, current_path)
    if current_path:
        evidence_paths.add(current_path)
    remaining = _remaining_required_paths(required_paths, evidence_paths)
    if not remaining:
        return ("sources_satisfied", [])
    if current_repeats_completed:
        return ("repeated_completed_read", remaining)
    if turn >= max_exchanges:
        return ("budget_exhausted", remaining)
    return ("continue", remaining)


def build_direct_loop_terminal_report(
    *,
    reason: str,
    model_alias: str,
    completed_paths: set,
    current_path: str,
    remaining_paths: list,
    turn: int,
    max_exchanges: int,
) -> str:
    evidence_paths = set(completed_paths or set())
    if current_path:
        evidence_paths.add(current_path)
    inspected = ", ".join(sorted(evidence_paths)) or "none"
    missing = ", ".join(remaining_paths or []) or "none"
    return (
        "PARTIAL\n"
        "Synthesis status: DETERMINISTIC_DIRECT_LOOP_TERMINAL\n"
        f"Reason: {reason}\n"
        f"Model: {model_alias}\n"
        f"Tool exchanges: {turn}/{max_exchanges}\n"
        f"Files inspected: {inspected}\n"
        f"Missing required sources: {missing}\n"
        "Confidence: MEDIUM\n"
        "Caveats: The bridge stopped the direct OSS tool loop deterministically because "
        "the model did not make forward progress across required sources."
    )


def _pending_tool_call_outputs_from_state(state: StoredResponse) -> list:
    pending = set(str(x) for x in state.pending_call_ids or [])
    try:
        stored_output = json.loads(state.output_items_json or "[]")
    except Exception:
        stored_output = []
    if isinstance(stored_output, list):
        stable_items = []
        for item in stored_output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call":
                continue
            if str(item.get("call_id") or "") not in pending:
                continue
            stable = dict(item)
            stable["status"] = "completed"
            stable_items.append(stable)
        if stable_items:
            return stable_items

    output = []
    for msg in state.messages:
        for tc in msg.get("tool_calls") or []:
            call_id = str(tc.get("id") or tc.get("codex", {}).get("call_id") or "")
            if call_id not in pending:
                continue
            func = tc.get("function", {})
            output.append({
                "type": "function_call",
                "id": new_id("fc"),
                "call_id": call_id,
                "name": func.get("name", "tool"),
                "arguments": func.get("arguments", "{}"),
                "status": "completed",
            })
    return output


def build_response_from_pending_child(body: JSON, state: StoredResponse) -> JSON:
    """Rebuild a stored pending tool-call response for idempotent continuation replay."""
    return APP.build_response_shell(
        body,
        state.model_alias,
        response_id=state.response_id,
        created_at=state.created_at,
        status="completed",
        output=_pending_tool_call_outputs_from_state(state),
    )


def _pending_command_summaries_from_state(state: StoredResponse) -> list:
    out = []
    for item in _pending_tool_call_outputs_from_state(state):
        path, command = normalize_tool_args(item.get("arguments", "{}"), item.get("name", ""))
        out.append(command or path or item.get("name", "tool"))
    return out


def _pending_command_signature_from_output_json(output_items_json: str) -> str:
    try:
        items = json.loads(output_items_json or "[]")
    except Exception:
        items = []
    summaries = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        path, command = normalize_tool_args(item.get("arguments", "{}"), item.get("name", ""))
        summaries.append(command or path or str(item.get("name", "tool")))
    return "\n".join(summaries)


def _tool_args_dict(tool_args: Any) -> dict:
    if isinstance(tool_args, dict):
        return tool_args
    if isinstance(tool_args, str):
        try:
            parsed = json.loads(tool_args)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _command_workdir(tool_args: Any, default: str) -> str:
    args = _tool_args_dict(tool_args)
    workdir = str(args.get("workdir") or "").strip()
    if workdir and os.path.isdir(workdir):
        return workdir
    return default


def _workdir_from_history(messages: list, default: str) -> str:
    for msg in reversed(messages or []):
        for tc in reversed(msg.get("tool_calls") or []):
            args = tc.get("function", {}).get("arguments", "{}")
            workdir = _command_workdir(args, "")
            if workdir:
                return workdir
    return default



def _extract_pattern_from_args(args: Any, tool_name: str = "") -> str:
    """Extract a search pattern from tool call arguments."""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except Exception:
            # Try to extract pattern from raw string
            import re as _re
            m = _re.search(r'(?:pattern|query|search)[=:]\\s*["\'"]?([^\\s"\'"]+)', args)
            return m.group(1) if m else ""
    else:
        parsed = args
    if isinstance(parsed, dict):
        return str(parsed.get("pattern", "") or parsed.get("query", "") or "")
    return ""


def _matching_required_path(path: str, required_paths: list) -> str:
    for required in required_paths or []:
        if _path_satisfies_required_path(path, required):
            return required
    return ""


def _resolve_declared_read_path(path: str, base_root: str) -> str:
    """Resolve a declared read-only path.

    Relative paths are scoped to the project/root workdir. Explicit absolute paths
    are allowed because native Codex subagents can read declared global context
    such as installed skills. This only applies to read-only evidence floors; write
    paths remain governed by owned-path policy.
    """
    if not str(path or "").strip():
        raise PermissionError("empty read path")
    root = os.path.realpath(os.path.abspath(base_root or os.getcwd()))
    if os.path.isabs(path):
        return os.path.realpath(os.path.abspath(path))
    target = os.path.realpath(os.path.abspath(os.path.join(root, path)))
    if target != root and not target.startswith(root + os.sep):
        raise PermissionError(f"path escapes project root: {path}")
    return target


def _hash_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def _safe_evidence_excerpt(text: str, *, max_chars: int = 1200) -> tuple:
    """Return a short redacted excerpt for model-authored synthesis."""
    try:
        from codex_oss.runtime.policy import scan_secrets
        redacted, found_secret = scan_secrets(str(text or ""))
    except Exception:
        redacted, found_secret = str(text or ""), False
    lines = []
    for line in redacted.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lines.append(stripped[:240])
        if len("\n".join(lines)) >= max_chars:
            break
    excerpt = "\n".join(lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n[truncated]"
    return excerpt, found_secret or len(str(text or "")) > len(excerpt)


def _completed_read_evidence_from_history(messages: list) -> dict:
    pending: Dict[str, str] = {}
    evidence = {}
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            name = func.get("name", "")
            args = func.get("arguments", "{}")
            path, _ = normalize_tool_args(args, name)
            kind = effective_tool_kind(name, args, classify_tool_call_name(name))
            call_id = tc.get("id") or tc.get("codex", {}).get("call_id")
            if path and _is_placeholder_path(path):
                continue
            if path and kind == "read" and call_id:
                pending[str(call_id)] = path
        tool_call_id = msg.get("tool_call_id") or msg.get("call_id")
        if msg.get("role") in ("tool", "function") or msg.get("type") in ("function_call_output", "tool_result"):
            path = pending.get(str(tool_call_id or ""))
            if path:
                content = as_text(msg.get("content", msg.get("output", "")))
                if tool_output_indicates_failure(content):
                    continue
                excerpt, redacted = _safe_evidence_excerpt(content)
                evidence[path] = {
                    "path": path,
                    "source": "consumer_tool_output",
                    "exit_code": 0,
                    "output_chars": len(content),
                    "output_sha256": _hash_text(content),
                    "output_excerpt": excerpt,
                    "redactions_applied": redacted,
                }
    return evidence


def _default_local_read_executor(path: str, workdir: str) -> tuple:
    """Read declared local sources without depending on repo-specific shell tools."""
    try:
        project_root = os.path.realpath(os.getcwd())
        root = os.path.realpath(os.path.abspath(workdir or project_root))
        if not (root == project_root or root.startswith(project_root + os.sep)):
            root = project_root
        target = _resolve_declared_read_path(path, root)
        if not os.path.exists(target):
            return 1, f"file not found: {path}"
        if os.path.isdir(target):
            return 1, f"path is a directory, not a readable file: {path}"
        max_bytes = int(os.getenv("OSS_SERVER_SIDE_READ_MAX_BYTES", "1048576"))
        size = os.path.getsize(target)
        with open(target, "rb") as f:
            data = f.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        if truncated or size > len(data):
            text += f"\n[server-side read truncated at {max_bytes} bytes of {size} total bytes]"
        return 0, text
    except Exception as exc:
        return 1, str(exc)


def build_server_side_read_completion_report(
    *,
    parent_response_id: str,
    child_state: Optional[StoredResponse],
    required_paths: list,
    evidence: dict,
    failures: list,
    reason: str = "pending_tool_call_not_adopted_recovered_by_bridge",
    synthesis_status: str = "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION",
) -> str:
    completed_paths = set(evidence.keys())
    missing = _remaining_required_paths(required_paths, completed_paths)
    status = "COMPLETE" if not missing and not failures else "PARTIAL"
    if reason == "declared_read_floor_completed_by_bridge" and status == "COMPLETE":
        caveat = (
            "Caveats: The bridge completed the declared read-only evidence floor "
            "server-side to avoid client continuation replay; no writes were performed."
        )
    elif reason == "declared_read_floor_completed_by_bridge":
        caveat = (
            "Caveats: The bridge attempted the declared read-only evidence floor "
            "server-side, but one or more required sources could not be inspected."
        )
    else:
        caveat = (
            "Caveats: The consumer did not adopt a pending read tool call, so the bridge completed "
            "the declared read-only evidence floor deterministically from the task ledger."
        )
    lines = [
        status,
        f"Synthesis status: {synthesis_status}",
        f"Reason: {reason}",
        (
            "Narrative status: runtime_deterministic_fallback"
            if synthesis_status == "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION"
            else "Narrative status: runtime_report"
        ),
        f"Parent response: {parent_response_id}",
        f"Pending response: {child_state.response_id if child_state else 'none'}",
        f"Replay count: {child_state.pending_replay_count if child_state else 0}",
        f"Files inspected: {', '.join(sorted(completed_paths)) if completed_paths else 'none'}",
        f"Missing required sources: {', '.join(missing) if missing else 'none'}",
        "No writes performed: true",
        "Evidence:",
    ]
    for path in sorted(evidence):
        item = evidence[path]
        lines.append(
            f"- {path}: source={item.get('source')}, chars={item.get('output_chars')}, "
            f"sha256={item.get('output_sha256')}"
        )
    if failures:
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in failures)
    lines.extend([
        "Confidence: HIGH" if status == "COMPLETE" else "Confidence: MEDIUM",
        caveat,
    ])
    return "\n".join(lines)


def build_server_side_read_finalizer_prompt(
    *,
    handoff_text: str,
    required_paths: list,
    evidence: dict,
    failures: list,
) -> str:
    """Build a no-tools prompt for narrative over canonical read evidence."""
    requested = ", ".join(required_paths or []) or "none"
    evidence_lines = []
    for path in sorted(evidence):
        item = evidence[path]
        evidence_lines.append(
            f"PATH: {path}\n"
            f"SOURCE: {item.get('source')}\n"
            f"EXIT_CODE: {item.get('exit_code')}\n"
            f"CHARS: {item.get('output_chars')}\n"
            f"SHA256: {item.get('output_sha256')}\n"
            f"EXCERPT:\n{item.get('output_excerpt', '')}"
        )
    failure_text = "\n".join(f"- {f}" for f in failures) if failures else "none"
    return (
        "You are writing only the narrative section for a read-only OSS subagent task.\n"
        "The bridge/runtime already gathered the evidence below. Do not call tools. "
        "Do not claim files were modified. Do not mention hidden reasoning or private scratchpads.\n\n"
        "The bridge/runtime owns status, files inspected, missing sources, hashes, and write safety. "
        "Do not include those as authority fields. Write natural, user-facing prose only. Include:\n"
        "- concise findings based only on the evidence excerpts\n"
        "- confidence\n"
        "- caveats\n\n"
        f"Original handoff:\n{handoff_text[:2000]}\n\n"
        f"Required paths: {requested}\n"
        f"Failures: {failure_text}\n\n"
        "Canonical evidence:\n"
        f"{chr(10).join(evidence_lines)[:6000]}"
    )


READ_NARRATIVE_FORBIDDEN_AUTHORITY_PATTERNS = re.compile(
    r"^("
    r"complete|partial|failed|fail|pass|"
    r"synthesis status|reason|parent response|pending response|replay count|"
    r"evidence-gathering status|files inspected|missing required sources|"
    r"no writes performed|evidence|canonical evidence"
    r")\s*:"
    r"|^(complete|partial|failed|fail|pass)\s*$"
    r"|no writes (?:were )?performed"
    r"|sha256\s*=",
    re.IGNORECASE | re.MULTILINE,
)

READ_NARRATIVE_AUTHORITY_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*("
    r"status|synthesis status|reason|parent response|pending response|replay count|"
    r"evidence authority|narrative authority|evidence-gathering status|"
    r"files inspected|missing required sources|no writes performed|evidence"
    r")\s*(?:\*\*)?\s*:",
    re.IGNORECASE,
)

READ_NARRATIVE_EVIDENCE_NEGATION = re.compile(
    r"\b("
    r"cannot\s+(?:read|open|access|inspect)|"
    r"can't\s+(?:read|open|access|inspect)|"
    r"unable\s+to\s+(?:read|open|access|inspect)|"
    r"no\s+(?:file\s*system|filesystem)\s+access|"
    r"bridge\s+runtime\s+(?:is\s+)?(?:unavailable|not\s+available)|"
    r"runtime\s+(?:is\s+)?(?:unavailable|not\s+available)|"
    r"files\s+inspected\s*:\s*none|"
    r"none\s+\(unable\s+to\s+open\)|"
    r"declared\s+sources\s+cannot\s+be\s+read"
    r")\b",
    re.IGNORECASE,
)


def sanitize_model_read_narrative(text: str) -> str:
    """Strip authority-shaped lines while preserving natural model prose."""
    kept = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if re.fullmatch(r"(complete|partial|failed|fail|pass)", stripped, flags=re.IGNORECASE):
            continue
        if READ_NARRATIVE_AUTHORITY_LINE.search(stripped):
            continue
        if re.search(r"\bsha256\s*=", stripped, flags=re.IGNORECASE):
            continue
        if re.search(r"\bno\s+(?:files?\s+)?(?:writes?|write operations|modifications|files?\s+were\s+(?:created|modified|changed))\b", stripped, flags=re.IGNORECASE):
            continue
        if re.search(r"\b(?:no|zero)\s+write\s+operations\b", stripped, flags=re.IGNORECASE):
            continue
        if re.search(r"\bno\s+files?\s+were\b", stripped, flags=re.IGNORECASE):
            continue
        if re.search(r"\b(?:written|modified|created|executed)\s+during\s+this\s+task\b", stripped, flags=re.IGNORECASE):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _model_read_narrative_required_fields(handoff_text: str) -> list[str]:
    runtime_owned = {
        "status",
        "files inspected",
        "files gathered",
        "missing required sources",
        "commands run",
        "commands used",
        "command used",
        "evidence",
        "hashes",
        "sha256",
        "no writes performed",
    }
    fields = []
    for field in extract_required_deliverables(handoff_text):
        for part in str(field or "").split(","):
            label = part.strip()
            normalized = label.lower()
            if normalized and normalized not in runtime_owned:
                fields.append(label)
    for default in ("findings", "confidence", "caveats"):
        if not any(str(field).strip().lower() == default for field in fields):
            fields.append(default)
    return fields


def validate_model_read_narrative(text: str, required_fields: list) -> tuple:
    """Validate model prose without letting it declare runtime-owned facts."""
    stripped = sanitize_model_read_narrative(text)
    if len(stripped) < 50:
        return False, ["report_too_short"]
    if is_intent_or_status(stripped):
        return False, ["intent_or_status_detected"]
    if READ_NARRATIVE_FORBIDDEN_AUTHORITY_PATTERNS.search(stripped):
        return False, ["runtime_authority_field_detected"]
    if READ_NARRATIVE_EVIDENCE_NEGATION.search(stripped):
        return False, ["runtime_evidence_negated"]
    lowered = stripped.lower()

    def has_narrative_field(field: str) -> bool:
        normalized = str(field or "").strip().lower()
        if not normalized:
            return True
        if normalized in lowered:
            return True
        if normalized == "findings":
            return any(token in lowered for token in (
                "summary", "summar", "observ", "indicat", "shows", "showed",
                "contains", "describes", "covers", "read", "inspected",
            ))
        if normalized == "caveats":
            return any(token in lowered for token in (
                "caveat", "limit", "limited", "scope", "scoped", "only",
                "truncated", "not", "however",
            ))
        if normalized == "confidence":
            return any(token in lowered for token in ("confidence", "confident", "high", "medium", "low"))
        return False

    missing = [f for f in required_fields if not has_narrative_field(f)]
    return len(missing) == 0, missing


def build_model_authored_server_side_read_completion_report(
    *,
    parent_response_id: str,
    child_state: Optional[StoredResponse],
    required_paths: list,
    evidence: dict,
    failures: list,
    reason: str,
    narrative: str,
) -> str:
    completed_paths = set(evidence.keys())
    missing = _remaining_required_paths(required_paths, completed_paths)
    status = "COMPLETE" if not missing and not failures else "PARTIAL"
    lines = [
        status,
        "Synthesis status: MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION",
        f"Reason: {reason}",
        "Evidence authority: bridge_runtime",
        "Narrative authority: model_finalizer",
        f"Parent response: {parent_response_id}",
        f"Pending response: {child_state.response_id if child_state else 'none'}",
        f"Replay count: {child_state.pending_replay_count if child_state else 0}",
        "Evidence-gathering status: PASS" if status == "COMPLETE" else "Evidence-gathering status: PARTIAL",
        f"Files inspected: {', '.join(sorted(completed_paths)) if completed_paths else 'none'}",
        f"Missing required sources: {', '.join(missing) if missing else 'none'}",
        "No writes performed: true",
        "Evidence:",
    ]
    for path in sorted(evidence):
        item = evidence[path]
        lines.append(
            f"- {path}: source={item.get('source')}, chars={item.get('output_chars')}, "
            f"sha256={item.get('output_sha256')}"
        )
    if failures:
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in failures)
    lines.extend([
        "",
        "Model-authored narrative:",
        str(narrative or "").strip(),
    ])
    return "\n".join(lines)


def try_model_authored_server_side_read_report(
    *,
    parent_response_id: str = "none",
    child_state: Optional[StoredResponse] = None,
    handoff_text: str,
    required_paths: list,
    evidence: dict,
    failures: list,
    finalizer_call,
    timeout_seconds: float,
    reason: str = "pending_tool_call_not_adopted_recovered_by_bridge",
    log_fn=None,
    mission_dir: str = "",
    model_alias: str = "",
) -> str:
    """Return natural model-authored read report when a bounded finalizer succeeds."""
    if not callable(finalizer_call):
        return ""

    # Build bounded prompt using read_evidence module
    prompt, prompt_stats = build_bounded_finalizer_prompt(
        handoff_text=handoff_text,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
    )

    started = time.time()
    try:
        text = str(finalizer_call(prompt, timeout_seconds) or "").strip()
        elapsed = time.time() - started
    except Exception as exc:
        elapsed = time.time() - started
        if log_fn:
            log_fn("server_side_read_model_finalizer_failed", error=str(exc))
        if mission_dir:
            log_finalizer_attempt(
                mission_dir=mission_dir,
                model_alias=model_alias,
                prompt_chars=prompt_stats["prompt_chars"],
                files_count=prompt_stats["files_included"],
                excerpt_chars_total=prompt_stats["excerpt_chars_total"],
                timeout_seconds=timeout_seconds,
                elapsed_seconds=elapsed,
                result="provider_error",
                validation_errors=[str(exc)[:200]],
            )
        return ""

    # Parse ReadNarrativeDraftV1 from model output
    narrative_draft, parse_error = parse_read_narrative_draft(text)
    if narrative_draft is None:
        if log_fn:
            log_fn("server_side_read_model_finalizer_invalid",
                    error=parse_error, text_len=len(text))
        if mission_dir:
            log_finalizer_attempt(
                mission_dir=mission_dir,
                model_alias=model_alias,
                prompt_chars=prompt_stats["prompt_chars"],
                files_count=prompt_stats["files_included"],
                excerpt_chars_total=prompt_stats["excerpt_chars_total"],
                timeout_seconds=timeout_seconds,
                elapsed_seconds=elapsed,
                result="schema_invalid",
                validation_errors=[parse_error or "no findings extracted"],
            )
        return ""

    # Validate narrative against evidence
    valid, validation_errors = validate_read_narrative_draft(narrative_draft, evidence)

    if mission_dir:
        log_finalizer_attempt(
            mission_dir=mission_dir,
            model_alias=model_alias,
            prompt_chars=prompt_stats["prompt_chars"],
            files_count=prompt_stats["files_included"],
            excerpt_chars_total=prompt_stats["excerpt_chars_total"],
            timeout_seconds=timeout_seconds,
            elapsed_seconds=elapsed,
            result="success" if valid else "semantic_invalid",
            validation_errors=validation_errors if not valid else [],
        )

    if not valid:
        if log_fn:
            log_fn("server_side_read_model_finalizer_semantic_invalid",
                    errors=validation_errors, text_len=len(text))
        return ""

    if log_fn:
        log_fn("server_side_read_model_finalizer_ok",
                text_len=len(text),
                findings_count=len(narrative_draft.get("findings", []) or []))

    child_response_id = child_state.response_id if child_state else "none"
    replay_count = child_state.pending_replay_count if child_state else 0

    return build_model_authored_read_report(
        parent_response_id=parent_response_id,
        child_response_id=child_response_id,
        replay_count=replay_count,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        reason=reason,
        narrative_draft=narrative_draft,
    )


def _execute_missing_declared_reads(
    *,
    required_paths: list,
    evidence: dict,
    failures: list,
    read_executor,
    workdir: str,
) -> None:
    for required in _remaining_required_paths(required_paths, set(evidence.keys())):
        exit_code, output = read_executor(required, workdir)
        if exit_code == 0:
            excerpt, redacted = _safe_evidence_excerpt(output)
            evidence[required] = {
                "path": required,
                "source": "bridge_server_side_read",
                "exit_code": exit_code,
                "output_chars": len(output),
                "output_sha256": _hash_text(output),
                "output_excerpt": excerpt,
                "redactions_applied": redacted,
            }
        else:
            failures.append(f"{required}: exit_code={exit_code} output={str(output)[:200]}")


def complete_declared_reads_from_bridge(
    *,
    parent_response_id: str,
    messages: list,
    handoff_text: str,
    project_root: str,
    executor=None,
    finalizer_call=None,
    finalizer_timeout_seconds: float = 12,
    reason: str = "declared_read_floor_completed_by_bridge",
    log_fn=None,
    mission_dir: str = "",
    model_alias: str = "",
) -> str:
    """Complete a declared read-only evidence floor without waiting for client adoption."""
    envelope = parse_task_envelope(handoff_text)
    required_paths = required_paths_from_envelope(envelope, handoff_text)
    if not required_paths:
        return ""
    read_executor = executor or _default_local_read_executor
    evidence = _completed_read_evidence_from_history(messages)
    failures: list[str] = []
    workdir = _workdir_from_history(messages, project_root)

    # Derive mission identity
    mission_id = envelope.get("mission_id", "") or f"read_{_hash_text(handoff_text)[:12]}"
    resolved_mission_dir = mission_dir or os.path.join(
        project_root, ".codex-oss", "missions", mission_id
    )

    # Create visible commentary sink for read floor
    commentary = VisibleCommentarySink(
        mission_id=mission_id,
        mission_dir=resolved_mission_dir,
        mode="summary",
    )
    commentary.emit(
        "mission_started",
        "Read-only evidence floor accepted",
        f"Detected a declared read-only evidence floor with {len(required_paths)} required files. Completing reads server-side to avoid replay loops.",
        phase="READ_FLOOR",
        source="runtime",
        evidence_refs=sorted(required_paths)[:8],
    )
    commentary.emit(
        "read_floor_detected",
        "Declared read floor identified",
        f"The handoff declares {len(required_paths)} required read-only source(s). The runtime will complete this evidence floor server-side.",
        phase="READ_FLOOR",
        source="runtime",
        metadata={"required_count": len(required_paths)},
    )

    # Gather evidence from history
    history_evidence = _completed_read_evidence_from_history(messages)
    if history_evidence:
        commentary.emit(
            "evidence_from_history",
            "Evidence found in message history",
            f"Found {len(history_evidence)} files already read in the message history.",
            phase="READ_FLOOR",
            source="runtime",
            evidence_refs=sorted(history_evidence.keys())[:8],
        )

    remaining_before = _remaining_required_paths(required_paths, set(evidence.keys()))
    if remaining_before:
        commentary.emit(
            "server_side_read_started",
            "Completing declared read floor",
            f"Reading {len(remaining_before)} remaining required files server-side.",
            phase="READ_FLOOR",
            source="runtime",
            evidence_refs=remaining_before[:8],
        )

    _execute_missing_declared_reads(
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        read_executor=read_executor,
        workdir=workdir,
    )

    # Merge history evidence into main evidence dict
    for path, item in history_evidence.items():
        if path not in evidence:
            evidence[path] = item

    if failures:
        commentary.emit(
            "read_failures",
            "Some reads failed",
            f"{len(failures)} required files could not be read.",
            phase="READ_FLOOR",
            source="runtime",
            severity="warning",
        )
    else:
        commentary.emit(
            "server_side_read_completed",
            "Read floor evidence gathered",
            f"Successfully inspected {len(evidence)} files ({len(required_paths)} required).",
            phase="READ_FLOOR",
            source="runtime",
            evidence_refs=sorted(evidence.keys())[:8],
        )

    completed = set(evidence.keys())
    missing = _remaining_required_paths(required_paths, completed)
    status = "COMPLETE" if not missing and not failures else "PARTIAL"

    # Persist canonical read evidence
    _persist_read_evidence_artifacts(
        mission_dir=resolved_mission_dir,
        mission_id=mission_id,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        status=status,
        synthesis_status="DETERMINISTIC_SERVER_SIDE_READ_COMPLETION",
        parent_response_id=parent_response_id,
        reason=reason,
    )

    # Attempt model-authored narration
    if callable(finalizer_call) and not failures:
        commentary.emit(
            "model_finalizer_started",
            "Requesting model-authored narrative",
            "Attempting model-authored narrative over canonical evidence.",
            phase="REPORT",
            source="runtime",
        )

    model_report = try_model_authored_server_side_read_report(
        parent_response_id=parent_response_id,
        child_state=None,
        handoff_text=handoff_text,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        finalizer_call=finalizer_call,
        timeout_seconds=finalizer_timeout_seconds,
        reason=reason,
        log_fn=log_fn,
        mission_dir=resolved_mission_dir,
        model_alias=model_alias,
    )

    if model_report:
        commentary.emit(
            "model_finalizer_succeeded",
            "Model-authored narrative accepted",
            "The model finalizer produced a valid narrative over the runtime-owned evidence.",
            phase="REPORT",
            source="runtime",
        )
        _persist_read_evidence_artifacts(
            mission_dir=resolved_mission_dir,
            mission_id=mission_id,
            required_paths=required_paths,
            evidence=evidence,
            failures=failures,
            status=status,
            synthesis_status="MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION",
            parent_response_id=parent_response_id,
            reason=reason,
        )
        commentary.emit(
            "mission_completed",
            "Mission completed (model-authored)",
            "Read floor complete with model-authored narrative. Evidence and report artifacts persisted.",
            phase="REPORT",
            source="runtime",
            artifact_refs=["canonical_read_evidence.json", "read_report_skeleton.json", "visible_commentary.jsonl", "summary.md"],
        )
        commentary.close({"status": status, "mission_id": mission_id, "confidence": "HIGH" if status == "COMPLETE" else "MEDIUM", "closure_source": "model_authored_server_side_read_completion"})
        return model_report

    if callable(finalizer_call) and not failures:
        commentary.emit(
            "model_finalizer_failed",
            "Model finalizer unavailable",
            "The model finalizer did not produce a valid narrative. Using deterministic fallback.",
            phase="REPORT",
            source="runtime",
            severity="warning",
        )

    if reason == "declared_read_floor_completed_by_bridge":
        if missing or failures:
            commentary.emit(
                "mission_partial",
                "Read floor incomplete",
                f"Some required sources could not be inspected.",
                phase="REPORT",
                source="runtime",
                severity="warning",
                artifact_refs=["canonical_read_evidence.json", "visible_commentary.jsonl", "summary.md"],
            )
            commentary.close({"status": "PARTIAL", "mission_id": mission_id, "confidence": "MEDIUM", "closure_source": "deterministic_server_side_read_incomplete"})
            return build_server_side_read_completion_report(
                parent_response_id=parent_response_id,
                child_state=None,
                required_paths=required_paths,
                evidence=evidence,
                failures=failures,
                reason=reason,
                synthesis_status="RUNTIME_SERVER_SIDE_READ_INCOMPLETE",
            )
        if log_fn:
            log_fn(
                "declared_read_floor_model_report_unavailable",
                evidence_count=len(evidence),
                failure_count=len(failures),
            )
        commentary.emit(
            "deterministic_fallback_used",
            "Deterministic read completion",
            "Using deterministic read floor report. Evidence was gathered but no model narrative is available.",
            phase="REPORT",
            source="runtime",
            artifact_refs=["canonical_read_evidence.json", "visible_commentary.jsonl", "summary.md"],
        )
        commentary.emit(
            "mission_completed",
            "Mission completed (deterministic)",
            "Read floor complete with deterministic report. Evidence and commentary artifacts persisted.",
            phase="REPORT",
            source="runtime",
            artifact_refs=["canonical_read_evidence.json", "read_report_skeleton.json", "visible_commentary.jsonl", "summary.md"],
        )
        commentary.close({"status": status, "mission_id": mission_id, "confidence": "HIGH" if status == "COMPLETE" else "MEDIUM", "closure_source": "deterministic_server_side_read_completion"})
        return build_server_side_read_completion_report(
            parent_response_id=parent_response_id,
            child_state=None,
            required_paths=required_paths,
            evidence=evidence,
            failures=failures,
            reason=reason,
            synthesis_status="DETERMINISTIC_SERVER_SIDE_READ_COMPLETION",
        )
    commentary.close({"status": status, "mission_id": mission_id, "confidence": "HIGH" if status == "COMPLETE" else "MEDIUM"})
    return build_server_side_read_completion_report(
        parent_response_id=parent_response_id,
        child_state=None,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        reason=reason,
    )

def _persist_read_evidence_artifacts(
    *,
    mission_dir: str,
    mission_id: str,
    required_paths: list,
    evidence: dict,
    failures: list,
    status: str,
    synthesis_status: str,
    parent_response_id: str = "none",
    child_response_id: str = "none",
    replay_count: int = 0,
    reason: str = "",
    narrative_draft=None,
) -> None:
    """Persist CanonicalReadEvidenceV1, ReadReportSkeletonV1, and related artifacts."""
    try:
        persist_read_artifacts(
            mission_dir=mission_dir,
            mission_id=mission_id,
            required_paths=required_paths,
            evidence=evidence,
            failures=failures,
            status=status,
            synthesis_status=synthesis_status,
            parent_response_id=parent_response_id,
            child_response_id=child_response_id,
            replay_count=replay_count,
            reason=reason,
            narrative_draft=narrative_draft,
        )
    except Exception:
        pass  # Artifact persistence is best-effort


def complete_pending_reads_from_bridge(
    *,
    parent_response_id: str,
    child_state: StoredResponse,
    handoff_text: str,
    project_root: str,
    executor=None,
    finalizer_call=None,
    finalizer_timeout_seconds: float = 8,
    log_fn=None,
) -> str:
    envelope = parse_task_envelope(handoff_text)
    required_paths = required_paths_from_envelope(envelope, handoff_text)
    if not required_paths:
        return ""
    if envelope.get("write_allowed") or envelope.get("owned_paths"):
        if log_fn:
            log_fn("server_side_read_fallback_skipped", reason="write_handoff")
        return ""
    task_type = str(envelope.get("task_type") or "").strip().lower()
    if task_type in {"bounded_write", "bounded_test_write", "implementation", "docs_support"}:
        if log_fn:
            log_fn("server_side_read_fallback_skipped", reason="write_task_type")
        return ""
    for step in envelope.get("verification_steps", []):
        lowered = str(step or "").lower()
        if any(token in lowered for token in (
            "test", "lint", "typecheck", "write", "edit", "append", "patch",
            "modify", "changed", "after the edit",
        )):
            if log_fn:
                log_fn("server_side_read_fallback_skipped", reason="non_read_verification")
            return ""

    # Derive mission identity and create commentary
    mission_id = envelope.get("mission_id", "") or f"pending_read_{_hash_text(handoff_text)[:12]}"
    mission_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    commentary = VisibleCommentarySink(
        mission_id=mission_id,
        mission_dir=mission_dir,
        mode="summary",
    )
    commentary.emit(
        "mission_started",
        "Pending read recovery started",
        f"Recovering pending reads for {mission_id}. The consumer did not adopt pending tool calls.",
        phase="READ_FLOOR",
        source="runtime",
    )

    read_executor = executor or _default_local_read_executor
    evidence = _completed_read_evidence_from_history(child_state.messages)
    failures = []
    workdir = _workdir_from_history(child_state.messages, project_root)

    # ── Execute pending tool calls (reads, greps, ls) ──
    # Use RuntimeContractCompleter module functions for grep/ls support

    pending_items = _pending_tool_call_outputs_from_state(child_state)
    grep_count = 0
    ls_count = 0
    
    commentary.emit(
        "server_side_read_started",
        "Recovering pending tool calls",
        f"Found {len(pending_items)} pending tool call(s) to recover.",
        phase="READ_FLOOR",
        source="runtime",
        metadata={"pending_count": len(pending_items)},
    )

    for item in pending_items:
        name = item.get("name", "")
        args = item.get("arguments", "{}")
        path, _ = normalize_tool_args(args, name)
        kind = effective_tool_kind(name, args, classify_tool_call_name(name))
        if path and _is_placeholder_path(path):
            continue

        if kind == "read" and path:
            required = _matching_required_path(path, required_paths)
            if not required:
                failures.append(f"pending read path is outside required sources: {path}")
                continue
            workdir = _command_workdir(args, workdir)
            if any(_path_satisfies_required_path(done, required) for done in evidence):
                continue
            exit_code, output = read_executor(path, workdir)
            if exit_code == 0:
                excerpt, redacted = _safe_evidence_excerpt(output)
                evidence[required] = {
                    "path": required,
                    "source": "bridge_server_side_read",
                    "exit_code": exit_code,
                    "output_chars": len(output),
                    "output_sha256": _hash_text(output),
                    "output_excerpt": excerpt,
                    "redactions_applied": redacted,
                }
            else:
                failures.append(f"{required}: exit_code={exit_code} output={str(output)[:200]}")

        elif kind in ("search", "grep", "grep_read") and path:
            # Use RuntimeContractCompleter for grep actions
            pattern = _extract_pattern_from_args(args, name)
            if not pattern:
                continue
            grep_action = {
                "kind": "required_grep",
                "tool_name": "rtk_grep",
                "arguments": {"path": path, "pattern": pattern},
            }
            from codex_oss.read_evidence import _execute_required_grep, _execute_required_ls
            exit_code, output = _execute_required_grep(
                grep_action["arguments"], project_root
            )
            grep_count += 1
            key = f"grep:{path}:{pattern[:40]}"
            excerpt, redacted = _safe_evidence_excerpt(output)
            evidence[key] = {
                "path": key,
                "source": "bridge_server_side_action",
                "exit_code": exit_code,
                "output_chars": len(output),
                "output_sha256": _hash_text(output),
                "output_excerpt": excerpt,
                "redactions_applied": redacted,
                "action_kind": "required_grep",
            }
            # grep exit_code 1 (no matches) is valid evidence
            if exit_code != 0 and exit_code != 1:
                failures.append(f"{name} on {path}: exit_code={exit_code}")

        elif kind in ("ls", "list") and path:
            ls_action = {
                "kind": "required_ls",
                "tool_name": "rtk_ls",
                "arguments": {"path": path},
            }
            exit_code, output = _execute_required_ls(
                ls_action["arguments"], project_root
            )
            ls_count += 1
            key = f"ls:{path}"
            excerpt, redacted = _safe_evidence_excerpt(output)
            evidence[key] = {
                "path": key,
                "source": "bridge_server_side_action",
                "exit_code": exit_code,
                "output_chars": len(output),
                "output_sha256": _hash_text(output),
                "output_excerpt": excerpt,
                "redactions_applied": redacted,
                "action_kind": "required_ls",
            }
            if exit_code != 0:
                failures.append(f"{name} on {path}: exit_code={exit_code}")

    if grep_count:
        commentary.emit(
            "grep_actions_recovered",
            "Grep actions recovered",
            f"Executed {grep_count} grep/search action(s) server-side.",
            phase="READ_FLOOR",
            source="runtime",
        )
    if ls_count:
        commentary.emit(
            "ls_actions_recovered",
            "List actions recovered",
            f"Executed {ls_count} ls action(s) server-side.",
            phase="READ_FLOOR",
            source="runtime",
        )

    _execute_missing_declared_reads(
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        read_executor=read_executor,
        workdir=workdir,
    )

    remaining_after_reads = _remaining_required_paths(required_paths, set(evidence.keys()))
    if not remaining_after_reads:
        failures = [
            failure for failure in failures
            if "pending read path is outside required sources" not in str(failure)
        ]

    completed = set(evidence.keys())
    missing = _remaining_required_paths(required_paths, completed)
    status = "COMPLETE" if not missing and not failures else "PARTIAL"

    # Persist artifacts
    _persist_read_evidence_artifacts(
        mission_dir=mission_dir,
        mission_id=mission_id,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        status=status,
        synthesis_status="DETERMINISTIC_SERVER_SIDE_READ_COMPLETION",
        parent_response_id=parent_response_id,
        child_response_id=child_state.response_id,
        replay_count=child_state.pending_replay_count,
        reason="pending_tool_call_not_adopted_recovered_by_bridge",
    )

    # Attempt model-authored narration
    if callable(finalizer_call) and not failures:
        commentary.emit(
            "model_finalizer_started",
            "Requesting model-authored narrative",
            "Attempting model-authored narrative over recovered evidence.",
            phase="REPORT",
            source="runtime",
        )

    model_report = try_model_authored_server_side_read_report(
        parent_response_id=parent_response_id,
        child_state=child_state,
        handoff_text=handoff_text,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        finalizer_call=finalizer_call,
        timeout_seconds=finalizer_timeout_seconds,
        reason="pending_tool_call_not_adopted_recovered_by_bridge",
        log_fn=log_fn,
        mission_dir=mission_dir,
        model_alias="",
    )

    if model_report:
        commentary.emit(
            "model_finalizer_succeeded",
            "Model-authored narrative accepted",
            "The model finalizer produced a valid narrative over recovered evidence.",
            phase="REPORT",
            source="runtime",
        )
        _persist_read_evidence_artifacts(
            mission_dir=mission_dir,
            mission_id=mission_id,
            required_paths=required_paths,
            evidence=evidence,
            failures=failures,
            status=status,
            synthesis_status="MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION",
            parent_response_id=parent_response_id,
            child_response_id=child_state.response_id,
            replay_count=child_state.pending_replay_count,
            reason="pending_tool_call_not_adopted_recovered_by_bridge",
        )
        commentary.emit(
            "mission_completed",
            "Pending recovery completed (model-authored)",
            "Recovered pending tool calls with model-authored narrative.",
            phase="REPORT",
            source="runtime",
            artifact_refs=["canonical_read_evidence.json", "visible_commentary.jsonl", "summary.md"],
        )
        commentary.close({"status": status, "mission_id": mission_id, "confidence": "HIGH" if status == "COMPLETE" else "MEDIUM", "closure_source": "model_authored_server_side_read_completion"})
        return model_report

    if callable(finalizer_call):
        commentary.emit(
            "model_finalizer_failed",
            "Model finalizer unavailable",
            "Using deterministic fallback for pending recovery report.",
            phase="REPORT",
            source="runtime",
            severity="warning",
        )

    commentary.emit(
        "deterministic_fallback_used",
        "Deterministic recovery report",
        "Pending tool calls recovered deterministically.",
        phase="REPORT",
        source="runtime",
        artifact_refs=["canonical_read_evidence.json", "visible_commentary.jsonl", "summary.md"],
    )
    commentary.emit(
        "mission_completed",
        "Pending recovery completed (deterministic)",
        "Recovered pending tool calls with deterministic report.",
        phase="REPORT",
        source="runtime",
        artifact_refs=["canonical_read_evidence.json", "visible_commentary.jsonl", "summary.md"],
    )
    # ── v11: Persist adoption probes from child state ──
    if child_state.adoption_probes_json:
        try:
            from codex_oss.tool_call_adoption import persist_adoption_probes
            sm = adoption_state_machine_from_response(child_state)
            probes = sm.to_probes()
            persist_adoption_probes(mission_dir, probes, sm)
        except Exception:
            pass

    commentary.close({"status": status, "mission_id": mission_id, "confidence": "HIGH" if status == "COMPLETE" else "MEDIUM", "closure_source": "deterministic_server_side_read_completion"})

    return build_server_side_read_completion_report(
        parent_response_id=parent_response_id,
        child_state=child_state,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        reason="pending_tool_call_not_adopted_recovered_by_bridge",
    )



def execute_pending_owned_write_from_bridge(
    *,
    parent_response_id: str,
    child_state: StoredResponse,
    handoff_text: str,
    project_root: str,
    log_fn=None,
) -> str:
    """Execute a pending simple owned-path write when Codex fails to adopt it."""
    envelope = parse_task_envelope(handoff_text)
    if select_mode(envelope) not in ("bounded_write_exact", "bounded_write_patch"):
        return ""
    if not envelope.get("owned_paths"):
        return ""
    pending_items = _pending_tool_call_outputs_from_state(child_state)
    if len(pending_items) != 1:
        return ""
    item = pending_items[0]
    _, command = normalize_tool_args(item.get("arguments", "{}"), item.get("name", ""))
    append_op = _append_redirection_from_shell_command(command or "")
    if not append_op:
        return ""
    target_path, content = append_op
    if not _path_is_within_owned_paths(target_path, envelope.get("owned_paths", []), project_root):
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"write target {target_path} is outside the declared owned paths",
        )
    full_target = os.path.abspath(os.path.join(project_root, target_path))
    if not _path_is_within_project(full_target, project_root):
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"write target {target_path} is outside the project root",
        )
    os.makedirs(os.path.dirname(full_target), exist_ok=True)
    before = ""
    if os.path.exists(full_target):
        try:
            with open(full_target, "r", encoding="utf-8", errors="replace") as handle:
                before = handle.read()
        except Exception:
            before = ""
    already_applied = content and content in before
    try:
        if not already_applied:
            with open(full_target, "a", encoding="utf-8") as handle:
                handle.write(content)
    except Exception as exc:
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"server-side owned write failed: {exc}",
        )
    try:
        with open(full_target, "r", encoding="utf-8", errors="replace") as handle:
            after = handle.read()
    except Exception:
        after = ""
    write_verified = bool(content and content in after)
    changed_paths = collect_owned_path_changes(envelope.get("owned_paths", []), project_root)
    if write_verified and _repo_relative_path(target_path, project_root) not in changed_paths:
        changed_paths.append(_repo_relative_path(target_path, project_root))
    verification_output = (
        f"server-side append {'already present' if already_applied else 'wrote'} {target_path}; "
        "pending consumer tool call was not adopted"
    )
    if log_fn:
        log_fn(
            "server_side_owned_write_complete",
            parent_response_id=parent_response_id,
            pending_response_id=child_state.response_id,
            target_path=target_path,
            already_applied=already_applied,
        )
    return build_patch_contract_report(
        envelope,
        changed_paths,
        "PASS" if write_verified else "PARTIAL",
        "owned path write completed server-side after pending tool adoption failure"
        if not already_applied else
        "owned path write was already present during idempotent server-side recovery",
        verification_seen=True,
        verification_output=verification_output,
    )


def execute_pending_owned_verification_from_bridge(
    *,
    parent_response_id: str,
    child_state: StoredResponse,
    handoff_text: str,
    project_root: str,
    log_fn=None,
) -> str:
    """Execute a pending owned-path read/search verification after an owned write."""
    envelope = parse_task_envelope(handoff_text)
    if select_mode(envelope) not in ("bounded_write_exact", "bounded_write_patch"):
        return ""
    pending_items = _pending_tool_call_outputs_from_state(child_state)
    if len(pending_items) != 1:
        return ""
    item = pending_items[0]
    path, command = normalize_tool_args(item.get("arguments", "{}"), item.get("name", ""))
    if not command or not _shell_command_is_read_like(command):
        return ""
    if not path:
        return ""
    allowed_paths = list(envelope.get("owned_paths") or []) + list(envelope.get("read_only_paths") or [])
    if not any(_path_satisfies_required_path(path, allowed) for allowed in allowed_paths):
        return ""
    if not _path_is_within_project(path, project_root):
        return ""
    full_target = os.path.abspath(os.path.join(project_root, path))
    if not os.path.exists(full_target):
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"verification target {path} does not exist",
            verification_seen=True,
            verification_output=f"server-side verification could not read {path}",
        )
    try:
        with open(full_target, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except Exception as exc:
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"server-side verification read failed: {exc}",
            verification_seen=True,
            verification_output=f"server-side verification could not read {path}",
        )
    markers = []
    pattern = _search_pattern_from_shell_command(command)
    if pattern:
        markers.append(pattern)
    markers.extend(marker for marker in _verification_markers_from_handoff(handoff_text) if marker not in markers)
    verified = any(marker in content for marker in markers) if markers else bool(content)
    completed_marker = ""
    if not verified:
        marker_written, completed_marker = _complete_declared_marker_write_if_safe(
            envelope=envelope,
            handoff_text=handoff_text,
            path=path,
            markers=markers,
            project_root=project_root,
        )
        if marker_written:
            try:
                with open(full_target, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except Exception:
                content = ""
            verified = any(marker in content for marker in markers) if markers else bool(content)
    changed_paths = collect_owned_path_changes(envelope.get("owned_paths", []), project_root)
    rel = _repo_relative_path(path, project_root)
    if verified and any(_path_satisfies_required_path(path, owned) for owned in envelope.get("owned_paths", [])):
        if rel not in changed_paths:
            changed_paths.append(rel)
    if log_fn:
        log_fn(
            "server_side_owned_verification_complete",
            parent_response_id=parent_response_id,
            pending_response_id=child_state.response_id,
            target_path=path,
            verified=verified,
            completed_marker=completed_marker,
        )
    return build_patch_contract_report(
        envelope,
        changed_paths,
        "PASS" if verified else "FAIL",
        "owned path verification completed server-side after pending tool adoption failure"
        if verified and not completed_marker else
        "declared owned marker write completed server-side after model skipped the write"
        if verified and completed_marker else
        "owned path verification failed server-side after pending tool adoption failure",
        verification_seen=True,
        verification_output=(
            f"server-side verification {'found' if verified else 'did not find'} "
            f"{', '.join(markers) if markers else 'readable content'} in {path}; "
            "pending consumer tool call was not adopted"
        ),
    )


def execute_pending_owned_shell_from_bridge(
    *,
    parent_response_id: str,
    child_state: StoredResponse,
    handoff_text: str,
    project_root: str,
    log_fn=None,
) -> str:
    """Execute a missed bounded implementation shell command under owned-path accounting."""
    envelope = parse_task_envelope(handoff_text)
    if select_mode(envelope) not in ("bounded_write_exact", "bounded_write_patch"):
        return ""
    owned_paths = list(envelope.get("owned_paths") or [])
    if not owned_paths:
        return ""
    pending_items = _pending_tool_call_outputs_from_state(child_state)
    if len(pending_items) != 1:
        return ""
    item = pending_items[0]
    path, command = normalize_tool_args(item.get("arguments", "{}"), item.get("name", ""))
    if not command:
        return ""
    if _shell_command_is_read_like(command):
        return ""
    if _append_redirection_from_shell_command(command):
        return ""
    args = _tool_args_dict(item.get("arguments", "{}"))
    workdir = _command_workdir(args, project_root)
    if not _path_is_within_project(workdir, project_root):
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"tool workdir {workdir} is outside the project root",
        )
    if path and not _path_is_within_project(path, project_root):
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"tool target {path} is outside the project root",
        )

    markers = _verification_markers_from_handoff(handoff_text)
    before = _owned_path_snapshot(owned_paths, project_root)
    marker_already_present = _markers_present_in_paths(
        markers,
        owned_paths + list(envelope.get("read_only_paths") or []),
        project_root,
    )
    timeout_s = int(os.getenv("OSS_SERVER_SIDE_TOOL_TIMEOUT_SECONDS", "30"))
    run_kwargs = {
        "shell": True,
        "cwd": workdir,
        "capture_output": True,
        "text": True,
        "timeout": timeout_s,
    }
    shell_path = os.getenv("SHELL")
    if shell_path:
        run_kwargs["executable"] = shell_path
    try:
        proc = subprocess.run(command, **run_kwargs)
    except subprocess.TimeoutExpired:
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"server-side bounded tool timed out after {timeout_s}s",
        )
    except Exception as exc:
        return build_patch_contract_report(
            envelope,
            [],
            "FAIL",
            f"server-side bounded tool failed: {exc}",
        )

    after = _owned_path_snapshot(owned_paths, project_root)
    changed_paths = collect_owned_path_changes(owned_paths, project_root)
    for changed in _changed_owned_paths_from_snapshots(before, after):
        if changed not in changed_paths:
            changed_paths.append(changed)
    marker_present = _markers_present_in_paths(
        markers,
        owned_paths + list(envelope.get("read_only_paths") or []),
        project_root,
    )
    verification_required = verification_contract_requested(envelope) or bool(markers)
    verification_seen = marker_present if verification_required else bool(changed_paths)
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    output_excerpt = (stdout or stderr or "no output").replace("\n", " | ")[:300]
    if proc.returncode != 0:
        status = "PARTIAL" if changed_paths else "FAIL"
        reason = (
            f"server-side bounded tool exited {proc.returncode} after pending tool adoption failure"
        )
    elif changed_paths and (verification_seen or not verification_required):
        status = "PASS"
        reason = "owned path changes completed server-side after pending tool adoption failure"
    elif marker_already_present and marker_present:
        status = "PASS"
        reason = "owned path verification marker was already present during idempotent server-side recovery"
    elif changed_paths:
        status = "PARTIAL"
        reason = "owned path changes were observed, but verification was not observed server-side"
    else:
        status = "FAIL"
        reason = "server-side bounded tool produced no declared owned-path changes"

    if log_fn:
        log_fn(
            "server_side_owned_shell_complete",
            parent_response_id=parent_response_id,
            pending_response_id=child_state.response_id,
            exit_code=proc.returncode,
            changed_paths=changed_paths,
            verification_seen=verification_seen,
        )
    return build_patch_contract_report(
        envelope,
        changed_paths,
        status,
        reason,
        verification_seen=bool(verification_seen),
        verification_output=(
            f"server-side bounded shell exit_code={proc.returncode}; output={output_excerpt}; "
            "pending consumer tool call was not adopted"
        ),
    )


def build_pending_child_not_fulfilled_report(
    *,
    parent_response_id: str,
    child_state: StoredResponse,
    handoff_text: str,
) -> str:
    envelope = parse_task_envelope(handoff_text)
    required_paths = required_paths_from_envelope(envelope, handoff_text)
    completed_paths = _extract_completed_read_paths_from_history(child_state.messages)
    missing = _remaining_required_paths(required_paths, completed_paths)
    pending = _pending_command_summaries_from_state(child_state)
    return (
        "PARTIAL\n"
        "Synthesis status: DETERMINISTIC_PENDING_TOOL_ADOPTION_FAILURE\n"
        "Reason: pending_tool_call_not_fulfilled\n"
        f"Parent response: {parent_response_id}\n"
        f"Pending response: {child_state.response_id}\n"
        f"Replay count: {child_state.pending_replay_count}\n"
        f"Pending command: {', '.join(pending) if pending else 'unknown'}\n"
        f"Files inspected: {', '.join(sorted(completed_paths)) if completed_paths else 'none'}\n"
        f"Missing required sources: {', '.join(missing) if missing else 'none'}\n"
        "Confidence: MEDIUM\n"
        "Caveats: The bridge had already produced the next tool call, but the consumer "
        "kept replaying the parent tool result instead of fulfilling the pending call."
    )


def is_intent_or_status(text: str) -> bool:
    if len(text) < MIN_REPORT_LENGTH:
        return True
    import re
    intent_match = re.search(INTENT_PATTERNS, text, re.IGNORECASE)
    if intent_match:
        # Evidence markers must be report-structure indicators, not just common words
        report_markers = (
            "oss_report_begin", "pass\n", "fail\n", "partial\n", "status:", "task status:",
            "summary:", "evidence:", "evidence snippets:", "evidence table:", "confidence:",
            "caveat:", "caveats:", "files inspected:", "files gathered:",
            "commands run:", "commands used:", "command used:",
        )
        has_report_structure = any(marker in text.lower() for marker in report_markers)
        if not has_report_structure:
            return True
    return False


def verification_contract_requested(envelope: dict) -> bool:
    """Return true when the handoff asks for verification proof, not just search evidence."""
    fields = [_field_label(field) for field in envelope.get("deliverable_fields", [])]
    if any("verification" in field or field in ("tests", "check", "checks") for field in fields):
        return True
    for step in envelope.get("verification_steps", []):
        lowered = str(step).lower()
        if any(word in lowered for word in ("run ", "test", "diff --check", "typecheck", "lint", "doctor", "verify")):
            return True
    return False


def declared_read_floor_only(envelope: dict) -> bool:
    """Return true for simple read-only source floors the bridge can safely complete."""
    if not envelope.get("read_only_paths"):
        return False
    if envelope.get("write_allowed") or envelope.get("owned_paths"):
        return False
    task_type = str(envelope.get("task_type") or "").strip().lower()
    if task_type in {"bounded_write", "bounded_test_write", "implementation", "docs_support"}:
        return False
    for step in envelope.get("verification_steps", []):
        lowered = str(step or "").lower()
        if any(token in lowered for token in (
            "grep", "search", "find ", "locate", "test", "lint", "typecheck",
            "write", "edit", "append", "patch", "modify", "changed", "after the edit",
        )):
            return False
    return True


def output_claims_verification(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "verification passed",
            "verification: pass",
            "verification result: pass",
            "tests passed",
            "checks passed",
            "diff --check passed",
            "doctor passed",
        )
    )


def tool_output_indicates_failure(output_text: str) -> bool:
    lowered = str(output_text or "").lower()
    if re.search(r"(process exited with code|exit code|exit_code:)\s*[1-9]\d*", lowered):
        return True
    if any(token in lowered for token in (
        "traceback (most recent call last)",
        "assertionerror",
        "syntaxerror",
        "command blocked by pretooluse hook",
        "blocked by pretooluse",
        "pre-tool block",
        "pretooluse",
    )):
        return True
    return False


def pretool_block_repair_instruction(output_text: str) -> str:
    text = str(output_text or "")
    lowered = text.lower()
    if "pretooluse" not in lowered and "pre-tool" not in lowered:
        return ""
    if "use `rtk read" in lowered or "instead of raw `cat`" in lowered:
        return (
            "[RUNTIME TOOL REPAIR]\n"
            "Your previous file-read command was blocked by repo policy. Retry with `rtk read <path>`. "
            "Do not use `cat`, and do not treat the blocked command as the final answer."
        )
    if "use `rtk ls" in lowered or "instead of raw `ls`" in lowered:
        return (
            "[RUNTIME TOOL REPAIR]\n"
            "Your previous list command was blocked by repo policy. Retry with `rtk ls <path>`. "
            "Do not use raw `ls`, and do not treat the blocked command as the final answer."
        )
    if "use `rtk grep" in lowered or "instead of raw `rg`" in lowered or "instead of raw `grep`" in lowered:
        return (
            "[RUNTIME TOOL REPAIR]\n"
            "Your previous search command was blocked by repo policy. Retry with `rtk grep <pattern> <path>`. "
            "Do not treat the blocked command as the final answer."
        )
    return (
        "[RUNTIME TOOL REPAIR]\n"
        "Your previous command was blocked by repo policy. Retry once using the suggested RTK replacement from the tool output. "
        "Do not treat the blocked command as the final answer."
    )


def should_retry_pretool_block(output_text: str, exit_code: int, turn: int, max_exchanges: int) -> bool:
    """Return true when a repo policy block should get one normal repair turn."""
    return bool(exit_code != 0 and turn < max_exchanges and pretool_block_repair_instruction(output_text))


def validate_report_output(text: str, mode: str, envelope: dict,
                           verification_observed: bool = False,
                           evidence_coverage_complete: bool = True) -> tuple:
    is_valid = True
    missing = []
    t = text.lower()

    if is_intent_or_status(text):
        return False, ["intent_or_status_detected"]

    required_by_mode = {
        "context_pack_report": ["confidence", "caveat"],
        "managed_autonomy": ["confidence", "caveat"],
        "bounded_write_exact": ["file", "confidence"],
        "bounded_write_patch": ["file", "confidence", "caveat"],
        "no_tool_exact": [],
        "escalate": [],
    }

    if mode in ("context_pack", "context_pack_report", "managed_autonomy", "bounded_write_exact", "bounded_write_patch"):
        if not re.search(r"\b(pass|fail|partial)\b", t):
            is_valid = False
            missing.append("status")

    for field in required_by_mode.get(mode, []):
        if field not in t:
            is_valid = False
            missing.append(field)

    exact_deliverables = [
        _field_label(field)
        for field in envelope.get("deliverable_fields", [])
        if _is_exact_deliverable_field(field)
    ]
    for field in exact_deliverables:
        if field.lower() not in t:
            is_valid = False
            missing.append(field)

    if verification_contract_requested(envelope) and output_claims_verification(text) and not verification_observed:
        is_valid = False
        missing.append("verification_observed")

    if mode in ("context_pack", "context_pack_report", "managed_autonomy"):
        if not evidence_coverage_complete and re.search(r"\bpass\b", t) and not re.search(r"\bpartial\b", t):
            is_valid = False
            missing.append("evidence_coverage")

    return is_valid, missing


def build_deterministic_write_report(path: str, success: bool, observed: str, mode: str) -> str:
    status = "PASS" if success else "FAIL"
    return (
        f"{status}\n"
        f"File changed: {path}\n"
        f"Verification command: rtk read {path}\n"
        f"Observed content: {observed}\n"
        f"Confidence: HIGH\n"
        f"Caveat: deterministic write by bridge runtime (mode={mode})"
    )


# ── End v11 preamble ──


def is_setup_read(path: str) -> bool:
    """Check if a file read is setup/context, not task work."""
    p = path.strip().strip("'\"").strip()
    for prefix in SETUP_PATH_PREFIXES:
        if p.startswith(prefix) and ("skill" in p.lower() or "SKILL" in p):
            return True
    return False


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
        # v10: add columns if missing (safe on existing DBs)
        try:
            self.db.execute("ALTER TABLE responses ADD COLUMN tool_exchange_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("ALTER TABLE responses ADD COLUMN task_max_exchanges INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("ALTER TABLE responses ADD COLUMN previous_response_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("ALTER TABLE responses ADD COLUMN pending_replay_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("ALTER TABLE responses ADD COLUMN output_items_json TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute("ALTER TABLE responses ADD COLUMN adoption_probes_json TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
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
            if state.previous_response_id and state.pending_call_ids:
                signature = _pending_command_signature_from_output_json(state.output_items_json or "[]")
                if signature:
                    rows = self.db.execute(
                        """
                        SELECT output_items_json FROM responses
                        WHERE previous_response_id = ?
                          AND response_id != ?
                          AND pending_call_ids_json NOT IN ('[]', '')
                        """,
                        (state.previous_response_id, state.response_id),
                    ).fetchall()
                    semantic_replays = sum(
                        1
                        for row in rows
                        if _pending_command_signature_from_output_json(row[0] or "[]") == signature
                    )
                    state.pending_replay_count = max(int(state.pending_replay_count or 0), semantic_replays)
            self.db.execute(
                """
                INSERT OR REPLACE INTO responses
                (response_id, model_alias, model_upstream, messages_json, pending_call_ids_json, created_at, tool_exchange_count, task_max_exchanges, previous_response_id, pending_replay_count, output_items_json, adoption_probes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.response_id,
                    state.model_alias,
                    state.model_upstream,
                    json_dumps(state.messages),
                    json_dumps(state.pending_call_ids),
                    state.created_at,
                    state.tool_exchange_count,
                    state.task_max_exchanges,
                    state.previous_response_id,
                    state.pending_replay_count,
                    state.output_items_json or "[]",
                    state.adoption_probes_json or "",
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
                "SELECT response_id, model_alias, model_upstream, messages_json, pending_call_ids_json, created_at, "
                "COALESCE(tool_exchange_count, 0), COALESCE(task_max_exchanges, 1), "
                "COALESCE(previous_response_id, ''), COALESCE(pending_replay_count, 0), "
                "COALESCE(output_items_json, '[]'), COALESCE(adoption_probes_json, '') "
                "FROM responses WHERE response_id = ?",
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
            tool_exchange_count=row[6] if len(row) > 6 else 0,
            task_max_exchanges=row[7] if len(row) > 7 else 1,
            previous_response_id=row[8] if len(row) > 8 else "",
            pending_replay_count=row[9] if len(row) > 9 else 0,
            output_items_json=row[10] if len(row) > 10 else "[]",
            adoption_probes_json=row[11] if len(row) > 11 else "",
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

    def find_pending_child(self, previous_response_id: str) -> Optional[StoredResponse]:
        if not previous_response_id:
            return None
        with self.lock:
            row = self.db.execute(
                """
                SELECT response_id FROM responses
                WHERE previous_response_id = ?
                  AND pending_call_ids_json NOT IN ('[]', '')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(previous_response_id),),
            ).fetchone()
        if not row:
            return None
        return self.get(row[0])

    def find_terminal_pending_child(self, previous_response_id: str, replay_limit: int) -> Optional[StoredResponse]:
        if not previous_response_id:
            return None
        with self.lock:
            row = self.db.execute(
                """
                SELECT response_id FROM responses
                WHERE previous_response_id = ?
                  AND pending_call_ids_json NOT IN ('[]', '')
                  AND COALESCE(pending_replay_count, 0) > ?
                ORDER BY COALESCE(pending_replay_count, 0) DESC, created_at DESC
                LIMIT 1
                """,
                (str(previous_response_id), int(replay_limit)),
            ).fetchone()
        if not row:
            return None
        return self.get(row[0])

    def increment_pending_replay_count(self, response_id: str) -> int:
        with self.lock:
            self.db.execute(
                "UPDATE responses SET pending_replay_count = COALESCE(pending_replay_count, 0) + 1 WHERE response_id = ?",
                (response_id,),
            )
            row = self.db.execute(
                "SELECT COALESCE(pending_replay_count, 0) FROM responses WHERE response_id = ?",
                (response_id,),
            ).fetchone()
            self.db.commit()
        return int(row[0]) if row else 0


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


# ── v8: Transactional tool-turn adapter ──

# Request classification
RequestKind = str  # "fresh_user_turn" | "tool_result_continuation" | "orphan_tool_result_continuation" | "resumed_user_turn"
ToolKind = str  # "read" | "write" | "shell" | "unknown"

@dataclass
class CompactedToolOutput:
    original: str
    compacted: str
    exit_code: int
    is_compacted: bool
    original_bytes: int
    original_lines: int

def classify_request_kind(body: JSON) -> RequestKind:
    """Classify the incoming request to determine the handling path."""
    input_items = body.get("input", [])
    tool_outputs = [m for m in input_items if m.get("type") == "function_call_output"]
    if tool_outputs:
        return "tool_result_continuation" if body.get("previous_response_id") else "orphan_tool_result_continuation"
    if body.get("previous_response_id"):
        return "resumed_user_turn"
    return "fresh_user_turn"

def classify_tool_call_name(name: str) -> ToolKind:
    """Classify a tool call by its name."""
    n = (name or "").lower()
    if any(kw in n for kw in ("read", "cat", "head", "tail", "grep", "find", "ls", "nl", "sed")):
        return "read"
    if any(kw in n for kw in ("write", "edit", "patch", "apply_patch", "create", "mkdir")):
        return "write"
    if any(kw in n for kw in ("exec", "bash", "sh", "test", "run", "npm", "node", "python")):
        return "shell"
    return "unknown"

def compact_tool_output(output_text: str, max_chars: int = 20000,
                        max_lines: int = 400, max_stdout: int = 16000) -> CompactedToolOutput:
    """Compact large tool outputs to prevent upstream stalls."""
    import hashlib
    lines = output_text.split("\n")
    n_bytes = len(output_text.encode("utf-8"))
    n_lines = len(lines)

    if n_bytes <= max_chars and n_lines <= max_lines:
        return CompactedToolOutput(
            original=output_text, compacted=output_text,
            exit_code=0, is_compacted=False,
            original_bytes=n_bytes, original_lines=n_lines)

    sha = hashlib.sha256(output_text.encode()).hexdigest()[:16]
    preview_first = "\n".join(lines[:200])
    preview_last = "\n".join(lines[-80:])

    compacted = (
        f"[TOOL OUTPUT COMPACTED]\n\n"
        f"Original bytes: {n_bytes}\n"
        f"Original lines: {n_lines}\n"
        f"SHA256: {sha}\n\n"
        f"--- first 200 lines ---\n{preview_first}\n\n"
        f"--- last 80 lines ---\n{preview_last}\n\n"
        f"If more exact content is required, ask the parent orchestrator for a narrower read."
    )
    return CompactedToolOutput(
        original=output_text, compacted=compacted,
        exit_code=0, is_compacted=True,
        original_bytes=n_bytes, original_lines=n_lines)

def build_deterministic_write_report(model: str, tool_name: str, path: str,
                                      success: bool) -> JSON:
    """Build a deterministic assistant report for write operations."""
    if success:
        text = (
            f"OSS write completed.\n\n"
            f"Model: {model}\n"
            f"Tool call: {tool_name}\n"
            f"Changed path: {path}\n"
            f"Result: success\n"
            f"Confidence: MEDIUM\n"
            f"Caveat: final OSS model report bypassed; tool execution confirmed\n"
            f"Escalation: GPT review required before accepting changes"
        )
    else:
        text = (
            f"OSS write failed.\n\n"
            f"Model: {model}\n"
            f"Tool call: {tool_name}\n"
            f"Path: {path}\n"
            f"Result: failure\n"
            f"Confidence: LOW\n"
            f"Escalation: GPT-5.4 review required"
        )
    return {
        "id": new_id("msg"),
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _repo_relative_path(path: str, project_root: str) -> str:
    p = os.path.normpath(os.path.expanduser(str(path or "")))
    if not p:
        return ""
    if os.path.isabs(p):
        try:
            return os.path.relpath(p, project_root)
        except ValueError:
            return p
    return p


def _owned_path_snapshot(owned_paths: list, project_root: str) -> dict:
    """Hash files under owned paths, including ignored/untracked files."""
    snapshot: dict[str, str] = {}
    root = os.path.realpath(os.path.abspath(project_root))
    for owned in owned_paths or []:
        if not _path_is_within_project(owned, root):
            continue
        full = os.path.realpath(os.path.abspath(owned if os.path.isabs(owned) else os.path.join(root, owned)))
        if not (full == root or full.startswith(root + os.sep)):
            continue
        if not os.path.exists(full):
            rel = _repo_relative_path(full, root)
            snapshot[rel] = "<missing>"
            continue
        paths = []
        if os.path.isfile(full):
            paths = [full]
        elif os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".codex-oss", "node_modules", "__pycache__"}]
                for filename in filenames:
                    paths.append(os.path.join(dirpath, filename))
        for path in paths:
            rel = _repo_relative_path(path, root)
            try:
                with open(path, "rb") as handle:
                    snapshot[rel] = hashlib.sha256(handle.read()).hexdigest()
            except Exception:
                snapshot[rel] = "<unreadable>"
    return snapshot


def _changed_owned_paths_from_snapshots(before: dict, after: dict) -> list[str]:
    changed = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path) and path not in changed:
            changed.append(path)
    return changed


def _markers_present_in_paths(markers: list[str], paths: list[str], project_root: str) -> bool:
    if not markers:
        return False
    root = os.path.realpath(os.path.abspath(project_root))
    for path in paths or []:
        if not _path_is_within_project(path, root):
            continue
        full = os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
        if not os.path.exists(full):
            continue
        candidates = []
        if os.path.isfile(full):
            candidates = [full]
        elif os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".codex-oss", "node_modules", "__pycache__"}]
                candidates.extend(os.path.join(dirpath, filename) for filename in filenames)
        for candidate in candidates:
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except Exception:
                continue
            if any(marker in text for marker in markers):
                return True
    return False


def _declared_marker_write_allowed(envelope: dict, handoff_text: str, path: str, marker: str, project_root: str) -> bool:
    if not marker or marker not in str(handoff_text or ""):
        return False
    if select_mode(envelope) not in ("bounded_write_exact", "bounded_write_patch"):
        return False
    if not (envelope.get("write_allowed") or envelope.get("owned_paths")):
        return False
    if not _path_is_within_owned_paths(path, envelope.get("owned_paths", []), project_root):
        return False
    lowered = " ".join([
        str(envelope.get("goal") or ""),
        " ".join(str(step or "") for step in envelope.get("verification_steps", [])),
        str(handoff_text or ""),
    ]).lower()
    return any(word in lowered for word in (
        "write", "add", "append", "insert", "create", "modify", "update",
    ))


def _complete_declared_marker_write_if_safe(
    *,
    envelope: dict,
    handoff_text: str,
    path: str,
    markers: list[str],
    project_root: str,
) -> tuple[bool, str]:
    if not path or not markers:
        return (False, "")
    if not _path_is_within_project(path, project_root):
        return (False, "")
    target = os.path.abspath(path if os.path.isabs(path) else os.path.join(project_root, path))
    if not _path_is_within_project(target, project_root):
        return (False, "")
    marker = next(
        (
            candidate for candidate in markers
            if _declared_marker_write_allowed(envelope, handoff_text, path, candidate, project_root)
        ),
        "",
    )
    if not marker:
        return (False, "")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        before = ""
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                before = handle.read()
        if marker not in before:
            sep = "" if not before or before.endswith("\n") else "\n"
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(f"{sep}{marker}\n")
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            after = handle.read()
    except Exception:
        return (False, "")
    return (marker in after, marker)


def _path_is_within_project(path: str, project_root: str) -> bool:
    if not path:
        return False
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(project_root, path))
    root = os.path.normpath(project_root)
    return full == root or full.startswith(root + os.sep)


def _path_is_within_owned_paths(path: str, owned_paths: list, project_root: str) -> bool:
    if not path:
        return True
    if not _path_is_within_project(path, project_root):
        return False
    rel = _repo_relative_path(path, project_root)
    for owned in owned_paths or []:
        owned_rel = _repo_relative_path(owned, project_root).rstrip(os.sep)
        if rel == owned_rel or rel.startswith(owned_rel + os.sep):
            return True
    return False


def collect_owned_path_changes(owned_paths: list, project_root: str) -> list:
    """Return changed/untracked paths under the owned path set."""
    scoped = []
    for path in owned_paths or []:
        if _path_is_within_project(path, project_root):
            scoped.append(_repo_relative_path(path, project_root))
    if not scoped:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", project_root, "status", "--porcelain", "--"] + scoped,
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    changed = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path and path not in changed:
            changed.append(path)
    return changed


def _find_tool_call_details(prev_state: Optional[StoredResponse], tool_call_id: str) -> tuple:
    """Return (tool_name, tool_args, target_path) for the current tool call when known."""
    if not prev_state:
        return ("", "", "")
    for msg in prev_state.messages:
        for tc in msg.get("tool_calls") or []:
            tc_func = tc.get("function", {})
            if tc.get("id") == tool_call_id or tc.get("codex", {}).get("call_id") == tool_call_id:
                tool_name = tc_func.get("name", "")
                tool_args = tc_func.get("arguments", "")
                target_path, _ = normalize_tool_args(tool_args, tool_name)
                return (tool_name, tool_args, target_path or "")
    return ("", "", "")


def build_patch_contract_report(envelope: dict, changed_paths: list, status: str,
                                reason: str, verification_seen: bool = False,
                                verification_output: str = "") -> str:
    owned = ", ".join(envelope.get("owned_paths", [])) or "unknown"
    changed = ", ".join(changed_paths) if changed_paths else "none"
    verification = "observed" if verification_seen else "not_observed"
    output_line = ""
    if verification_output:
        clipped = verification_output.strip().replace("\n", " | ")[:500]
        output_line = f"Verification evidence: {clipped}\n"
    return (
        f"{status}\n"
        "Transport status: PASS\n"
        f"Task status: {status}\n"
        f"Owned paths: {owned}\n"
        f"Changed owned paths: {changed}\n"
        f"Verification status: {verification}\n"
        f"{output_line}"
        f"Reason: {reason}\n"
        "Confidence: MEDIUM\n"
        "Caveats: deterministic bridge patch report; GPT review must verify semantic correctness before acceptance."
    )


def build_deterministic_error_report(error_kind: str, details: str) -> JSON:
    """Build a deterministic error report when a tool fails."""
    return {
        "id": new_id("msg"),
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": (
            f"OSS tool failed.\n\n"
            f"Error: {error_kind}\n"
            f"Details: {details}\n\n"
            f"Confidence: LOW\n"
            f"Escalation: GPT-5.4 review required"
        ), "annotations": []}],
    }


def build_read_evidence_metadata(
    *,
    model_alias: str,
    tool_name: str,
    tool_kind: str,
    tool_call_id: str,
    tool_args: str,
    target_path: str,
    output_text: str,
    exit_code: int,
    required_fields: list,
) -> JSON:
    """Canonical evidence object for runtime-owned read closure."""
    import hashlib

    output_bytes = output_text.encode("utf-8", errors="replace")
    _, command = normalize_tool_args(tool_args, tool_name)
    return {
        "schema_version": "raw_read_evidence.v1",
        "model_alias": model_alias,
        "tool_name": tool_name,
        "tool_kind": tool_kind,
        "tool_call_id": tool_call_id,
        "command": command,
        "normalized_path": target_path,
        "exit_code": exit_code,
        "output_chars": len(output_text),
        "output_lines": len(output_text.splitlines()),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "required_deliverables": list(required_fields or []),
    }


# ── End v8 preamble ──


class ProxyApp:
    def __init__(self):
        self.upstream_base = os.getenv("UPSTREAM_BASE", "https://opencode.ai/zen/go/v1").rstrip("/")
        self.upstream_chat_url = f"{self.upstream_base}/chat/completions"
        self.upstream_models_url = f"{self.upstream_base}/models"
        self.upstream_key = os.getenv("UPSTREAM_API_KEY") or os.getenv("OPENCODE_GO_API_KEY", "")
        self.proxy_key = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""
        if self.proxy_key and self.upstream_key and self.proxy_key == self.upstream_key:
            print("FATAL: Local proxy token (PROXY_API_KEY/LITELLM_MASTER_KEY) must not equal the upstream OpenCode Go key.", file=sys.stderr)
            print("Use a separate local bearer token for Codex-to-bridge auth. Set CODEX_OSS_LOCAL_TOKEN.", file=sys.stderr)
            sys.exit(1)
        if self.proxy_key and self.upstream_key and len(self.proxy_key) > 30 and self.proxy_key.startswith("sk-"):
            if self.proxy_key[:8] == self.upstream_key[:8]:
                print("WARNING: Local proxy token shares prefix with upstream key. Verify they are different.", file=sys.stderr)
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
        self.direct_agent_loop_v2 = os.getenv("DIRECT_AGENT_LOOP_V2", "1") != "0"

        # ── Bridge v7 hardening ──

        # Fatal missing key in production mode
        if self.gpt_model_strategy == "error" and not self.upstream_key:
            if os.getenv("ALLOW_MISSING_OPENCODE_KEY", "0") != "1":
                print("FATAL: UPSTREAM_API_KEY/OPENCODE_GO_API_KEY is not set and GPT_MODEL_STRATEGY=error.", file=sys.stderr)
                print("Set UPSTREAM_API_KEY, OPENCODE_GO_API_KEY, or start with ALLOW_MISSING_OPENCODE_KEY=1", file=sys.stderr)
                sys.exit(1)

        # Concurrency semaphores — prevent rate-limit death spirals
        self.max_global_concurrency = int(os.getenv("MAX_GLOBAL_UPSTREAM_CONCURRENCY", "2"))
        self.model_concurrency = json.loads(
            os.getenv("MODEL_CONCURRENCY_JSON", '{"deepseek-v4-pro":1,"kimi-k2.6":1,"deepseek-v4-flash":2}') or "{}"
        )
        self.global_semaphore = threading.Semaphore(self.max_global_concurrency)
        self.model_semaphores: Dict[str, threading.Semaphore] = {}

        # Circuit breaker — mark models degraded after capacity errors
        self.model_health: Dict[str, JSON] = {}  # model → {"status": "ok"|"degraded", "errors": int, "since": timestamp}
        self.circuit_breaker_errors = int(os.getenv("CIRCUIT_BREAKER_ERRORS", "2"))
        self.circuit_breaker_cooldown = int(os.getenv("CIRCUIT_BREAKER_COOLDOWN", "300"))

        # Max-turn guard — prevent runaway token burn in OSS agent loops
        self.max_tool_turns = int(os.getenv("OSS_MAX_TOOL_TURNS", "6"))
        self.max_write_tool_turns = int(os.getenv("OSS_MAX_WRITE_TOOL_TURNS", "3"))

        # ── Bridge v8: Transactional tool-turn adapter ──
        self.native_max_tool_exchanges = int(os.getenv("OSS_NATIVE_MAX_TOOL_EXCHANGES", "1"))
        self.continuation_tools = os.getenv("CONTINUATION_TOOLS", "none")
        self.continuation_model = os.getenv("CONTINUATION_MODEL", "kimi-k2.6")
        self.continuation_fallbacks = [
            m.strip() for m in os.getenv("CONTINUATION_FALLBACK_MODELS", "deepseek-v4-flash").split(",") if m.strip()
        ]
        self.continuation_deadline = float(os.getenv("CONTINUATION_DEADLINE_SECONDS", "60"))
        self.write_result_mode = os.getenv("WRITE_RESULT_MODE", "deterministic")
        self.write_report_deadline = float(os.getenv("WRITE_REPORT_DEADLINE_SECONDS", "20"))
        self.upstream_first_byte_timeout = float(os.getenv("UPSTREAM_FIRST_BYTE_TIMEOUT_SECONDS", "30"))
        self.upstream_idle_timeout = float(os.getenv("UPSTREAM_IDLE_TIMEOUT_SECONDS", "30"))
        self.oss_turn_deadline = float(os.getenv("OSS_TURN_DEADLINE_SECONDS", "120"))
        self.max_tool_output_chars = int(os.getenv("MAX_TOOL_OUTPUT_CHARS", "20000"))
        self.degraded_completion_on_timeout = os.getenv("DEGRADED_COMPLETION_ON_TIMEOUT", "1") != "0"

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
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
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
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
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

    def _acquire_model(self, model: str) -> bool:
        sem = self.model_semaphores.get(model)
        if sem is None:
            sem = threading.Semaphore(self.model_concurrency.get(model, 1))
            self.model_semaphores[model] = sem
        return sem.acquire(blocking=False)

    def _release_model(self, model: str) -> None:
        sem = self.model_semaphores.get(model)
        if sem:
            sem.release()

    def _check_circuit_breaker(self, model: str) -> Optional[str]:
        health = self.model_health.get(model)
        if health and health.get("status") == "degraded":
            if time.time() - health["since"] < self.circuit_breaker_cooldown:
                return health.get("fallback")
            health["status"] = "ok"
        return None

    def _record_model_error(self, model: str) -> None:
        health = self.model_health.get(model, {"errors": 0, "since": time.time(), "status": "ok"})
        health["errors"] = health.get("errors", 0) + 1
        if health["errors"] >= self.circuit_breaker_errors:
            health["status"] = "degraded"
            health["since"] = time.time()
            fb = self.fallback_model_map.get(model, [None])[0] if self.fallback_model_map.get(model) else None
            health["fallback"] = fb
            self.log("circuit_breaker_open", model=model, fallback=fb, cooldown_s=self.circuit_breaker_cooldown)
        self.model_health[model] = health

    # ── v8: Transactional tool-turn adapter methods ──

    def resolve_continuation_tools(self, tool_kind: ToolKind) -> Optional[List[JSON]]:
        """Return tools for a continuation turn, or None to strip all tools and force finalization."""
        if self.continuation_tools == "none":
            return None
        return []

    def should_deterministic_close(self, tool_kind: ToolKind, exit_code: int) -> bool:
        """Whether this tool result should get a deterministic report without calling the model."""
        if tool_kind == "write" and self.write_result_mode == "deterministic":
            return True
        if exit_code != 0:
            return True
        return False

    def build_continuation_payload(self, model: str, base_messages: List[JSON],
                                     compacted: CompactedToolOutput,
                                     tool_kind: ToolKind, original_task: str) -> JSON:
        """Build a finalizer payload — no tools, short context, compacted output."""
        messages = list(base_messages)
        tool_output_text = compacted.compacted[:self.max_tool_output_chars]
        messages.append({
            "role": "user",
            "content": (
                f"Task: {original_task}\n\n"
                f"Tool executed: {tool_kind} operation\n"
                f"Tool result:\n{tool_output_text}"
            )
        })
        payload = {
            "model": model,
            "messages": messages,
            "stream": self.upstream_streaming,
        }
        # No tools on finalizer
        payload["tools"] = []
        return payload

    def call_continuation_with_deadline(self, payload: JSON, deadline: float) -> JSON:
        """Call upstream with a hard deadline. Returns response or raises."""
        payload = dict(payload)
        force_non_stream = bool(payload.pop("_codex_force_non_stream", False))
        if os.getenv("CONTINUATION_STREAM", "1") != "0" and not force_non_stream:
            return self.call_continuation_stream_with_deadline(payload, deadline)

        start = time.time()
        payload["stream"] = False
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
            "Accept": "application/json",
        }

        req = urllib.request.Request(self.upstream_chat_url, data=data, headers=headers, method="POST")
        remaining = max(1.0, deadline - (time.time() - start))

        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except Exception as e:
            self.log("continuation_upstream_failed", error=str(e), deadline=deadline)
            raise

    def call_continuation_stream_with_deadline(self, payload: JSON, deadline: float) -> JSON:
        """Use the streaming transport for no-tool continuation synthesis."""
        start = time.time()
        content_parts: List[str] = []
        usage: JSON = {}
        try:
            remaining = max(1.0, deadline - (time.time() - start))
            for kind, value in self.iter_upstream_chat_stream(payload, timeout=remaining):
                if time.time() - start > deadline:
                    raise TimeoutError("streaming continuation deadline exceeded")
                if kind == "complete":
                    return value
                if kind == "chunk":
                    if isinstance(value, dict) and value.get("usage"):
                        usage = value["usage"]
                    for choice in (value.get("choices") if isinstance(value, dict) else []) or []:
                        delta = choice.get("delta") or {}
                        if isinstance(delta, dict) and delta.get("content") is not None:
                            content_parts.append(as_text(delta.get("content")))
                        message = choice.get("message") or {}
                        if isinstance(message, dict) and message.get("content") is not None:
                            content_parts.append(as_text(message.get("content")))
                elif kind == "done":
                    break
        except Exception as e:
            self.log("continuation_upstream_failed", error=str(e), deadline=deadline)
            raise

        content = "".join(content_parts)
        if not content.strip():
            self.log("continuation_upstream_failed",
                     error="streaming continuation produced no content", deadline=deadline)
            raise UpstreamError(502, "streaming continuation produced no content")
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage,
        }

    def build_degraded_completion(self, model: str, reason: str, tool_kind: ToolKind) -> JSON:
        """Build a degraded-but-terminal assistant message for stalled continuations."""
        return {
            "id": new_id("msg"),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": (
                f"OSS worker could not complete the final report.\n\n"
                f"Model: {model}\n"
                f"Operation: {tool_kind}\n"
                f"Reason: {reason}\n"
                f"Confidence: LOW\n"
                f"Escalation: GPT-5.4 review required"
            ), "annotations": []}],
        }

    def build_response_shell(self, body: JSON, model_alias: str, response_id: str,
                             created_at: int, status: str, output: List[JSON]) -> JSON:
        return {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "model": model_alias,
            "output": output,
            "parallel_tool_calls": False,
            "previous_response_id": body.get("previous_response_id"),
            "store": False,
            "temperature": None,
            "top_p": None,
            "truncation": "disabled",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "metadata": {},
        }

    def call_upstream_chat(self, payload: JSON) -> JSON:
        if not self.upstream_key:
            raise UpstreamError(500, "OPENCODE_GO_API_KEY is not set")

        # Concurrency gate — prevent rate-limit death spirals
        model = payload.get("model", "unknown")
        acquired_global = self.global_semaphore.acquire(timeout=10)
        if not acquired_global:
            raise UpstreamError(429, f"Global concurrency limit ({self.max_global_concurrency}) reached")
        model_acquired = False
        try:
            model_sem = self.model_semaphores.get(model)
            if model_sem is None:
                model_sem = threading.Semaphore(self.model_concurrency.get(model, 1))
                self.model_semaphores[model] = model_sem
            model_acquired = model_sem.acquire(timeout=30)
            if not model_acquired:
                raise UpstreamError(429, f"Model concurrency limit for {model} reached")
        except UpstreamError:
            self.global_semaphore.release()
            raise

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
            "Accept": "application/json",
        }

        last_err: Optional[UpstreamError] = None
        try:
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
        finally:
            if model_acquired:
                self.model_semaphores.get(model, threading.Semaphore(1)).release()
            self.global_semaphore.release()

    def iter_upstream_chat_stream(self, payload: JSON, timeout: Optional[float] = None):
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
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
            "Accept": "text/event-stream, application/json",
        }

        req = urllib.request.Request(self.upstream_chat_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
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
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
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

        # Detect subagent fork: GPT model with continuation context.
        # In production mode, auto-alias to OSS so subagent spawn succeeds.
        prev_id = body.get("previous_response_id")
        if self.gpt_model_strategy == "error" and is_gpt_model(model_alias) and prev_id:
            self.log("gpt_subagent_fork_auto_alias", model_alias=model_alias,
                     fallback=self.gpt_oss_fallback)
            model_alias = self.gpt_oss_fallback

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
        handoff_text = _extract_handoff_text(base_messages)
        handoff_mode = select_mode(parse_task_envelope(handoff_text)) if handoff_text else ""
        if handoff_mode in {"bounded_write_exact", "bounded_write_patch"} and not legacy_direct_write_modes_enabled():
            converted_tools = []
            reverse_name_map = {}
            guard = {
                "role": "system",
                "content": (
                    "This raw OSS write handoff is deprecated. Do not call tools or write files. "
                    "Return a concise FAILED/PARTIAL report explaining that implementation work must use a MissionV1 A4/A5/A6 runtime-controlled patch lane."
                ),
            }
            base_messages = [guard] + base_messages
            self.log("legacy_direct_write_tools_stripped", mode=handoff_mode)
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
        from codex_oss.transport.response_builder import build_response_object_from_chat

        return build_response_object_from_chat(
            body=body,
            chat_resp=chat_resp,
            base_messages=base_messages,
            model_alias=model_alias,
            model_upstream=model_upstream,
            reverse_name_map=reverse_name_map,
            state_put=self.state.put,
            stored_response_factory=StoredResponse,
            repair_chat_history=repair_chat_history,
            extract_budget=_extract_budget,
            restore_tool_name=restore_tool_name,
            new_id=new_id,
            now=now,
            json_dumps=json_dumps,
            as_text=as_text,
            response_id=response_id,
            created_at=created_at,
        )


APP = ProxyApp()
START_TIME = time.time()
ACTIVE_REQUESTS = 0
ACTIVE_REQUESTS_COND = threading.Condition()
SHUTDOWN_REQUESTED = threading.Event()

from codex_oss.transport.chat_stream import ChatStreamAssembler
from codex_oss.transport.emitter import ResponseEmitter


class Handler(BaseHTTPRequestHandler):
    server_version = "ResponsesChatProxy/12.0"

    def _track_request_start(self) -> None:
        global ACTIVE_REQUESTS
        with ACTIVE_REQUESTS_COND:
            ACTIVE_REQUESTS += 1

    def _track_request_end(self) -> None:
        global ACTIVE_REQUESTS
        with ACTIVE_REQUESTS_COND:
            ACTIVE_REQUESTS = max(0, ACTIVE_REQUESTS - 1)
            ACTIVE_REQUESTS_COND.notify_all()

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

    def _emit_raw_write_demotion_if_needed(
        self,
        body: JSON,
        base_messages: List[JSON],
        model_alias: str,
    ) -> bool:
        """Terminalize deprecated raw write handoffs before raw model prose can close them."""
        handoff_text = _extract_handoff_text(base_messages)
        report_text = raw_write_handoff_demoted_report(handoff_text)
        if not report_text:
            return False
        APP.log("legacy_direct_write_terminal_guard", mode=select_mode(parse_task_envelope(handoff_text)))
        emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
        emitter.emit_text_message(report_text)
        emitter.complete()
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not APP.auth_ok(self.headers.get("Authorization", "")):
            self._send_error_obj(401, "Unauthorized", "unauthorized")
            return

        if self.path.rstrip("/") in ("/v1/models", "/models"):
            status, data, ctype = APP.forward_models()
            self._send_bytes(status, data, ctype)
            return

        if self.path.rstrip("/") in ("/health", "/v1/health"):
            from codex_oss.health import build_health_status
            self._send_json(200, build_health_status(APP, BRIDGE_VERSION, __file__, START_TIME))
            return

        self._send_error_obj(404, f"Unknown path: {self.path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        self._track_request_start()
        try:
            self._do_POST_tracked()
        finally:
            self._track_request_end()

    def _do_POST_tracked(self) -> None:
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

            # ── v1 spec: A2/A3 managed investigation via runtime loop ──
            from codex_oss.managed_bridge import run_managed_mission_from_body, should_handle_managed_mission_body
            managed_emitter = None
            commentary_callback = None
            visible_stream_enabled = os.getenv("OSS_VISIBLE_TRACE_STREAM", "1") != "0"
            if body.get("stream") and visible_stream_enabled and should_handle_managed_mission_body(body, raw_model_alias):
                managed_emitter = ResponseEmitter(self, new_id("resp"), raw_model_alias, True)
                managed_emitter.start()

                def commentary_callback(event):
                    if not isinstance(event, dict) or event.get("safe_for_user") is not True:
                        return
                    text = str(event.get("message") or "").strip()
                    if not text:
                        return
                    managed_emitter.emit_commentary_message(text)

            managed = run_managed_mission_from_body(
                body=body,
                raw_model_alias=raw_model_alias,
                log_fn=APP.log,
                call_payload_fn=APP.call_continuation_with_deadline,
                map_model_fn=lambda model: map_model(model, APP.model_map),
                request_deadline=float(os.getenv("REQUEST_DEADLINE_SECONDS", "90")),
                commentary_callback=commentary_callback,
            )
            if managed.handled:
                emitter = managed_emitter or ResponseEmitter(self, new_id("resp"), raw_model_alias, bool(body.get("stream")))
                try:
                    emitter.emit_text_message(managed.report_text, phase="final_answer")
                    emitter.complete()
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    APP.log(
                        "client_disconnected",
                        phase="managed_runtime_emit",
                        mission_status=managed.status,
                        error=str(e),
                    )
                    self.close_connection = True
                return
            if managed_emitter is not None:
                APP.log("managed_runtime_predicate_miss", model_alias=raw_model_alias)
                managed_emitter.emit_text_message(
                    "The managed runtime stream could not route this mission. Start a fresh OSS subagent task with a valid MissionV1 handoff.",
                    phase="final_answer",
                )
                managed_emitter.complete()
                return

            # ── v8: Check for continuation BEFORE prepare_chat_payload ──
            request_kind = classify_request_kind(body)
            if request_kind in ("tool_result_continuation", "orphan_tool_result_continuation"):
                self._handle_continuation(body)
                return

            payload, base_messages, model_alias, model_upstream, reverse_name_map = APP.prepare_chat_payload(body)
            if self._emit_raw_write_demotion_if_needed(body, base_messages, model_alias):
                return
            handoff_text = _extract_handoff_text(base_messages)
            envelope = parse_task_envelope(handoff_text) if handoff_text else {}
            handoff_mode = select_mode(envelope) if handoff_text else ""
            if (
                os.getenv("OSS_FRESH_SERVER_SIDE_READ_FLOOR", "1") != "0"
                and handoff_mode in ("context_pack", "context_pack_report")
                and declared_read_floor_only(envelope)
                and not _has_evidence_ledger(body)
            ):
                report_text = complete_declared_reads_from_bridge(
                    parent_response_id=new_id("resp"),
                    messages=base_messages,
                    handoff_text=handoff_text,
                    project_root=os.getcwd(),
                    finalizer_call=self._server_side_read_finalizer_call(model_alias),
                    finalizer_timeout_seconds=float(os.getenv("OSS_SERVER_SIDE_READ_FINALIZER_TIMEOUT_SECONDS", "30")),
                    reason="declared_read_floor_completed_by_bridge",
                    log_fn=APP.log,
                )
                if report_text:
                    APP.log("fresh_server_side_read_floor_complete", mode=handoff_mode)
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(report_text)
                    emitter.complete()
                    return

            # ── v8: Transactional continuation path ──
            request_kind = classify_request_kind(body)
            APP.log(
                "request",
                model_alias=model_alias,
                model_upstream=model_upstream,
                messages=len(payload.get("messages", [])),
                tools=len(payload.get("tools", [])),
                stream=bool(body.get("stream")),
                turn_kind=request_kind,
                tool_count=sum(1 for m in payload.get("messages", []) if m.get("role") == "tool"),
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
            # Return a recoverable message instead of a raw provider error.
            # This lets GPT-5.5 gracefully restart the subagent task.
            self._send_json(200, {
                "id": body.get("previous_response_id") or "resp_orphan",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": body.get("model", "unknown"),
                "output": [{
                    "type": "message",
                    "id": "msg_orphan_recovery",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": (
                            "The bridge lost its tool-call state for this turn, likely due to "
                            "proxy restart or expired state. Start a fresh OSS subagent task "
                            "with the original request. No repository changes were accepted."
                        ),
                        "annotations": [],
                    }],
                }],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            })
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

    def _handle_continuation(self, body: JSON) -> None:
        """v8: Handle tool_result_continuation with deterministic finalization.

        This is a dedicated path — it does NOT go through prepare_chat_payload.
        For writes/errors: deterministic close. For reads: compacted finalizer call.
        """
        model_alias = str(body.get("model") or "ocg-deepseek-v4-pro")
        model_upstream = APP.route_gpt_model_for_chat_bridge(model_alias) or map_model(model_alias, APP.model_map)
        request_start = time.time()
        request_deadline = float(os.getenv("REQUEST_DEADLINE_SECONDS", "90"))

        # Classify tool outputs
        tool_outputs = [m for m in body.get("input", []) if m.get("type") == "function_call_output"]
        if not tool_outputs:
            # Fall through to normal path if somehow no tool outputs
            self._handle_fresh_turn(body)
            return

        first_tool = tool_outputs[0]
        tool_call_id = str(first_tool.get("call_id", ""))
        tool_name_raw = first_tool.get("name", "")

        # Look up stored state to find the previous tool call's original name
        prev_id = body.get("previous_response_id")
        prev_state = None
        if prev_id:
            prev_state = APP.state.get(str(prev_id))
        if not prev_state and tool_call_id:
            prev_state = APP.state.find_by_call_ids([tool_call_id])
            if prev_state and not prev_id:
                prev_id = prev_state.response_id
                body["previous_response_id"] = prev_id
        if prev_state and tool_call_id and tool_call_id not in set(prev_state.pending_call_ids or []):
            call_state = APP.state.find_by_call_ids([tool_call_id])
            if call_state:
                prev_state = call_state
                prev_id = call_state.response_id
                body["previous_response_id"] = prev_id

        reverse_name_map = {}
        if prev_state:
            # ── v11: Mark tool call as adopted by Codex consumer ──
            if tool_call_id:
                sm = adoption_state_machine_from_response(prev_state)
                if tool_call_id in sm.calls:
                    sm.mark_adopted(tool_call_id)
                    sm.mark_completed(tool_call_id, tool_output_text)
                    adoption_state_machine_to_response(sm, prev_state)
                    APP.state.put(prev_state)
            # Try to find the tool name from the stored messages
            for msg in prev_state.messages:
                tool_calls = msg.get("tool_calls") or []
                for tc in tool_calls:
                    tc_func = tc.get("function", {})
                    if tc.get("id") == tool_call_id or tc.get("codex", {}).get("call_id") == tool_call_id:
                        tool_name_raw = tc_func.get("name", tool_name_raw)
                    if tc_func.get("name"):
                        reverse_name_map[tc_func["name"]] = tc_func["name"]

        tool_kind = classify_tool_call_name(tool_name_raw)
        tool_output_raw = first_tool.get("output", "")
        tool_output_text = str(tool_output_raw)

        completed_read_paths_before = _extract_completed_read_paths_from_history(prev_state.messages) if prev_state else set()

        # Count turn and determine budget. Use completed read evidence as a backstop so
        # response-id churn cannot reset a direct-agent loop to exchange 1 forever.
        if prev_state:
            history_turns = _count_tool_result_messages(prev_state.messages)
            turn = max(prev_state.tool_exchange_count, history_turns, len(completed_read_paths_before)) + 1
            max_exchanges = prev_state.task_max_exchanges or 1
        else:
            turn = 1
            max_exchanges = 1

        APP.log("continuation_turn", turn=turn, max_exchanges=max_exchanges,
                tool_kind=tool_kind, tool_name=tool_name_raw,
                output_chars=len(tool_output_text))

        # Setup reads (skill files) don't count against budget
        if turn == 1 and is_setup_read(tool_name_raw or tool_call_id):
            turn = 0  # don't count this
            APP.log("continuation_setup_read", tool=tool_name_raw)

        # Compact large outputs
        compacted = compact_tool_output(tool_output_text, max_chars=APP.max_tool_output_chars)
        if compacted.is_compacted:
            APP.log("tool_output_compacted", original_bytes=compacted.original_bytes,
                    original_lines=compacted.original_lines)

        # Exit code detection
        exit_code = 0
        if isinstance(tool_output_raw, dict) and tool_output_raw.get("error"):
            exit_code = 1
        elif tool_output_indicates_failure(tool_output_text):
            exit_code = 1

        handoff_text = _extract_handoff_text(prev_state.messages) if prev_state else ""
        envelope = parse_task_envelope(handoff_text)
        mode = select_mode(envelope)
        matched_tool_name, tool_args, target_path = _find_tool_call_details(prev_state, tool_call_id)
        if matched_tool_name:
            tool_name_raw = matched_tool_name
        tool_kind = effective_tool_kind(tool_name_raw, tool_args, tool_kind)
        required_fields_for_evidence = extract_required_deliverables(handoff_text)
        read_evidence = build_read_evidence_metadata(
            model_alias=model_alias,
            tool_name=tool_name_raw,
            tool_kind=tool_kind,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
            target_path=target_path,
            output_text=tool_output_text,
            exit_code=exit_code,
            required_fields=required_fields_for_evidence,
        )

        if (
            os.getenv("OSS_PROACTIVE_SERVER_SIDE_READ_FLOOR", "1") != "0"
            and prev_state
            and exit_code == 0
            and mode in ("context_pack", "context_pack_report")
            and declared_read_floor_only(envelope)
            and not _has_evidence_ledger(body)
        ):
            repaired_messages = repair_chat_history(prev_state.messages, tool_outputs)
            report_text = complete_declared_reads_from_bridge(
                parent_response_id=str(prev_id or prev_state.response_id),
                messages=repaired_messages,
                handoff_text=handoff_text,
                project_root=os.getcwd(),
                finalizer_call=self._server_side_read_finalizer_call(model_alias),
                finalizer_timeout_seconds=float(os.getenv("OSS_SERVER_SIDE_READ_FINALIZER_TIMEOUT_SECONDS", "30")),
                reason="declared_read_floor_completed_by_bridge",
                log_fn=APP.log,
            )
            if report_text:
                APP.log("proactive_server_side_read_floor_complete", mode=mode)
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(report_text)
                emitter.complete()
                return

        if prev_id and prev_state and mode in ("context_pack", "context_pack_report"):
            pending_child = APP.state.find_pending_child(str(prev_id))
            if pending_child:
                replay_count = APP.state.increment_pending_replay_count(pending_child.response_id)
                pending_child.pending_replay_count = replay_count
                replay_limit = int(os.getenv("OSS_PENDING_REPLAY_TERMINAL_REPLAYS", "2"))
                APP.log(
                    "continuation_pending_child_replay",
                    parent_response_id=str(prev_id),
                    pending_response_id=pending_child.response_id,
                    replay_count=replay_count,
                    replay_limit=replay_limit,
                )
                if replay_count > replay_limit:
                    report_text = ""
                    report_text = execute_pending_owned_write_from_bridge(
                        parent_response_id=str(prev_id),
                        child_state=pending_child,
                        handoff_text=handoff_text,
                        project_root=os.getcwd(),
                        log_fn=APP.log,
                    )
                    report_text = report_text or execute_pending_owned_shell_from_bridge(
                        parent_response_id=str(prev_id),
                        child_state=pending_child,
                        handoff_text=handoff_text,
                        project_root=os.getcwd(),
                        log_fn=APP.log,
                    )
                    report_text = report_text or execute_pending_owned_verification_from_bridge(
                        parent_response_id=str(prev_id),
                        child_state=pending_child,
                        handoff_text=handoff_text,
                        project_root=os.getcwd(),
                        log_fn=APP.log,
                    )
                    if os.getenv("OSS_SERVER_SIDE_READ_FALLBACK", "1") != "0":
                        report_text = report_text or complete_pending_reads_from_bridge(
                            parent_response_id=str(prev_id),
                            child_state=pending_child,
                            handoff_text=handoff_text,
                            project_root=os.getcwd(),
                            finalizer_call=self._server_side_read_finalizer_call(model_alias),
                            finalizer_timeout_seconds=float(os.getenv("OSS_SERVER_SIDE_READ_FINALIZER_TIMEOUT_SECONDS", "30")),
                            log_fn=APP.log,
                        )
                    if not report_text:
                        report_text = build_pending_child_not_fulfilled_report(
                            parent_response_id=str(prev_id),
                            child_state=pending_child,
                            handoff_text=handoff_text,
                        )
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(report_text)
                    emitter.complete()
                    return
                resp_obj = build_response_from_pending_child(body, pending_child)
                if body.get("stream"):
                    self._send_sse(resp_obj)
                else:
                    self._send_json(200, resp_obj)
                return

        if mode == "bounded_write_patch":
            if not legacy_direct_write_modes_enabled():
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(build_legacy_write_demoted_report(mode))
                emitter.complete()
                APP.log("legacy_direct_write_demoted", mode=mode)
                return
            repair_instruction = pretool_block_repair_instruction(tool_output_text)
            if repair_instruction and exit_code != 0 and prev_state and turn < max_exchanges:
                input_items = list(body.get("input", []))
                input_items.insert(0, {"role": "system", "content": repair_instruction})
                body["input"] = input_items
                APP.log(
                    "bounded_patch_pretool_block_repair_continuation",
                    tool_kind=tool_kind,
                    turn=turn,
                    max_exchanges=max_exchanges,
                )
                self._handle_fresh_turn(body)
                return
            from codex_oss.legacy_modes import handle_bounded_patch_continuation
            patch_decision = handle_bounded_patch_continuation(
                envelope=envelope,
                tool_kind=tool_kind,
                tool_output_text=tool_output_text,
                compacted_output=compacted.compacted,
                exit_code=exit_code,
                target_path=target_path,
                turn=turn,
                max_exchanges=max_exchanges,
                project_root=os.getcwd(),
                collect_owned_path_changes=collect_owned_path_changes,
                path_is_within_owned_paths=_path_is_within_owned_paths,
                build_patch_contract_report=build_patch_contract_report,
                tool_output_indicates_failure=tool_output_indicates_failure,
            )
            if patch_decision.continue_for_verification and prev_state:
                input_items = list(body.get("input", []))
                input_items.insert(0, {"role": "system", "content": patch_decision.ledger_text})
                body["input"] = input_items
                APP.log(patch_decision.log_event, **patch_decision.log_fields)
                self._handle_fresh_turn(body)
                if prev_id:
                    refreshed = APP.state.get(str(prev_id))
                    if refreshed:
                        refreshed.tool_exchange_count = turn
                        refreshed.task_max_exchanges = max_exchanges
                        APP.state.put(refreshed)
                return
            if patch_decision.handled:
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(patch_decision.text)
                emitter.complete()
                if patch_decision.log_event:
                    APP.log(patch_decision.log_event, **patch_decision.log_fields)
                return

        # Deterministic close for writes and errors (even under budget)
        # Writers always close deterministically after first write
        if exit_code != 0 or tool_kind == "write":
            repair_instruction = pretool_block_repair_instruction(tool_output_text)
            if repair_instruction and prev_state and turn < max_exchanges:
                input_items = list(body.get("input", []))
                input_items.insert(0, {"role": "system", "content": repair_instruction})
                body["input"] = input_items
                APP.log("pretool_block_repair_continuation", tool_kind=tool_kind, turn=turn, max_exchanges=max_exchanges)
                self._handle_fresh_turn(body)
                return
            if tool_kind == "write":
                report = build_deterministic_write_report(
                    model=model_alias,
                    tool_name=tool_name_raw,
                    path=target_path or tool_call_id,
                    success=(exit_code == 0),
                )
            else:
                report = build_deterministic_error_report(
                    f"{tool_kind}_tool_failed",
                    compacted.compacted[:500],
                )
            APP.log("continuation_deterministic_close", tool_kind=tool_kind)
            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
            emitter.emit_text_message(report["content"][0]["text"])
            emitter.complete()
            return

        use_direct_agent_loop = False

        # Continue with tools if budget remains (v10: managed autonomy)
        if turn < max_exchanges and prev_state:
            APP.log("continuation_continue", turn=turn, max_exchanges=max_exchanges)

            # Determine execution mode from handoff
            if not handoff_text:
                handoff_text = _extract_handoff_text(prev_state.messages)
                envelope = parse_task_envelope(handoff_text)
                mode = select_mode(envelope)
            APP.log("execution_mode", mode=mode, paths=envelope.get("read_only_paths", []))
            read_paths = set(completed_read_paths_before)
            if target_path:
                read_paths_after_current = set(read_paths)
                read_paths_after_current.add(target_path)
            else:
                read_paths_after_current = set(read_paths)

            # Context-pack: gather sources, one no-tools model call
            context_pack_attempted = False

            # no_tool_exact mode: just pass text through
            if mode == "invalid_handoff":
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(
                    "FAIL\n"
                    "Reason: invalid OSS handoff schema.\n"
                    f"Schema error: {envelope.get('schema_error')}\n"
                    "Confidence: HIGH — bridge rejected a malformed structured handoff before executing delegated work."
                )
                emitter.complete()
                return

            if mode == "no_tool_exact":
                context_pack_attempted = True
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(str(tool_output_text) if tool_output_text else "OK")
                emitter.complete()
                return

            # Bounded exact write mode: runtime handles it
            if mode == "bounded_write_exact" and not context_pack_attempted:
                if not legacy_direct_write_modes_enabled():
                    context_pack_attempted = True
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(build_legacy_write_demoted_report(mode))
                    emitter.complete()
                    APP.log("legacy_direct_write_demoted", mode=mode)
                    return
                context_pack_attempted = True
                APP.log("mode_bounded_write_exact")
                from codex_oss.legacy_modes import handle_bounded_write_exact
                exact_decision = handle_bounded_write_exact(envelope, mode, tool_kind, os.getcwd(), APP.log)
                if exact_decision.wait_for_model_write:
                    context_pack_attempted = False
                    APP.log("bounded_write_waiting_for_write", tool_kind=tool_kind, **exact_decision.log_fields)
                elif exact_decision.handled:
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(exact_decision.text)
                    emitter.complete()
                    if exact_decision.log_event:
                        APP.log(exact_decision.log_event, **exact_decision.log_fields)
                    return

            evidence_ledger_present = _has_evidence_ledger(body)
            use_direct_agent_loop = should_use_direct_agent_loop(
                mode, evidence_ledger_present, APP.direct_agent_loop_v2)
            if use_direct_agent_loop:
                APP.log("direct_agent_loop_continue", mode=mode, turn=turn, max_exchanges=max_exchanges)

            if (
                mode in ("context_pack", "context_pack_report")
                and not evidence_ledger_present
                and not use_direct_agent_loop
            ):
                context_pack_attempted = True
                from codex_oss.legacy_modes import handle_context_pack_report
                context_decision = handle_context_pack_report(
                    body=body,
                    envelope=envelope,
                    handoff_text=handoff_text,
                    prev_id=prev_id or "unknown",
                    prev_state_messages=prev_state.messages,
                    mode=mode,
                    tool_output_text=tool_output_text,
                    request_deadline=request_deadline,
                    request_start=request_start,
                    project_root=os.getcwd(),
                    continuation_model=APP.continuation_model,
                    model_map=APP.model_map,
                    continuation_deadline=APP.continuation_deadline,
                    continuation_fallbacks=APP.continuation_fallbacks,
                    max_tool_output_chars=APP.max_tool_output_chars,
                    log_fn=APP.log,
                    map_model=map_model,
                    call_continuation_with_deadline=APP.call_continuation_with_deadline,
                    build_task_session=build_task_session,
                    extract_read_paths_from_history=_extract_read_paths_from_history,
                    build_context_pack=build_context_pack,
                    evaluate_evidence_coverage=evaluate_evidence_coverage,
                    is_intent_or_status=is_intent_or_status,
                    validate_report_output=validate_report_output,
                    build_context_pack_deterministic_report=build_context_pack_deterministic_report,
                )
                if context_decision.handled:
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(context_decision.text)
                    emitter.complete()
                    if context_decision.log_event:
                        APP.log(context_decision.log_event, **context_decision.log_fields)
                    return

            # Skip managed autonomy if context-pack was attempted
            if not context_pack_attempted:
                # Managed autonomy: suppress duplicate reads
                if mode == "managed_autonomy" or use_direct_agent_loop:
                    # Check if current tool call is for an already-read path
                    dup_msg = suppress_duplicate_read(
                        {"name": tool_name_raw, "arguments": tool_args},
                        TaskSession(task_session_id="", root_response_id="", task_class="",
                                    execution_mode="", max_tool_exchanges=max_exchanges,
                                    read_paths={p: {"complete": True} for p in read_paths},
                                    required_paths=required_paths_from_envelope(envelope, handoff_text)))
                    if dup_msg:
                        APP.log("duplicate_suppressed", tool=tool_name_raw)
                        input_items = list(body.get("input", []))
                        input_items.insert(0, {"role": "system", "content": f"[RUNTIME SUPPRESSION]\n{dup_msg}"})
                        body["input"] = input_items

                # Inject evidence ledger for managed autonomy
                required_paths = required_paths_from_envelope(envelope, handoff_text)
                direct_decision, remaining = direct_loop_terminal_decision(
                    required_paths,
                    read_paths,
                    target_path,
                    turn,
                    max_exchanges,
                )
                direct_sources_satisfied = use_direct_agent_loop and direct_decision == "sources_satisfied"
                if direct_sources_satisfied:
                    APP.log("direct_agent_loop_sources_satisfied",
                            mode=mode, path=target_path, required_paths=required_paths)
                elif use_direct_agent_loop and direct_decision in ("repeated_completed_read", "budget_exhausted"):
                    APP.log(
                        "direct_agent_loop_terminal_guard",
                        reason=direct_decision,
                        turn=turn,
                        max_exchanges=max_exchanges,
                        path=target_path,
                        remaining=remaining,
                    )
                    text = build_direct_loop_terminal_report(
                        reason=direct_decision,
                        model_alias=model_alias,
                        completed_paths=read_paths,
                        current_path=target_path,
                        remaining_paths=remaining,
                        turn=turn,
                        max_exchanges=max_exchanges,
                    )
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(text)
                    emitter.complete()
                    return
                else:
                    if use_direct_agent_loop and turn >= max_exchanges:
                        APP.log("direct_agent_loop_budget_exhausted", turn=turn, max_exchanges=max_exchanges)
                    elif use_direct_agent_loop:
                        APP.log("direct_agent_loop_continue_with_tools",
                                turn=turn, max_exchanges=max_exchanges)
                
                if remaining and not _has_evidence_ledger(body):
                    ledger_text = "EVIDENCE LEDGER\n"
                    if read_paths_after_current:
                        ledger_text += f"Already read: {', '.join(sorted(read_paths_after_current))}\n"
                    ledger_text += f"Still required: {', '.join(remaining)}\n"
                    ledger_text += f"Tool budget remaining: {max_exchanges - turn}\n"
                    ledger_text += "Do not reread completed files."
                    input_items = list(body.get("input", []))
                    input_items.insert(0, {"role": "system", "content": ledger_text})
                    body["input"] = input_items

                if not direct_sources_satisfied and turn < max_exchanges:
                    body["_codex_oss_tool_exchange_count"] = turn
                    body["_codex_oss_task_max_exchanges"] = max_exchanges
                    self._handle_fresh_turn(body)
                    # Update state with incremented turn count
                    if prev_id:
                        refreshed = APP.state.get(str(prev_id))
                        if refreshed:
                            refreshed.tool_exchange_count = turn
                            refreshed.task_max_exchanges = max_exchanges
                            APP.state.put(refreshed)
                    return

        # Finalizer call for reads — no tools, short deadline, compacted output
        stream = bool(body.get("stream"))
        emitter = ResponseEmitter(self, new_id("resp"), model_alias, stream)
        from codex_oss.legacy_modes import handle_read_finalizer
        def run_finalizer():
            return handle_read_finalizer(
                body=body,
                prev_state_messages=prev_state.messages if prev_state else [],
                tool_outputs=tool_outputs,
                tool_kind=tool_kind,
                compacted_output=compacted.compacted,
                model_alias=model_alias,
                reverse_name_map=reverse_name_map,
                continuation_model=model_alias if use_direct_agent_loop else APP.continuation_model,
                model_map=APP.model_map,
                continuation_tools=APP.continuation_tools,
                continuation_deadline=APP.continuation_deadline,
                continuation_fallbacks=APP.continuation_fallbacks,
                max_tool_output_chars=APP.max_tool_output_chars,
                degraded_completion_on_timeout=APP.degraded_completion_on_timeout,
                log_fn=APP.log,
                map_model=map_model,
                repair_chat_history=repair_chat_history,
                merge_new_user_messages=merge_new_user_messages,
                extract_handoff_text=_extract_handoff_text,
                extract_required_deliverables=extract_required_deliverables,
                validate_report=validate_report,
                call_continuation_with_deadline=APP.call_continuation_with_deadline,
                build_response_object=APP.build_response_object,
                build_degraded_completion=APP.build_degraded_completion,
                evidence_metadata=read_evidence,
            )

        if stream:
            finalizer_decision = self._await_streamed_read_finalizer(
                emitter,
                body,
                model_alias,
                run_finalizer,
            )
            if finalizer_decision is None:
                return
        else:
            finalizer_decision = run_finalizer()
        if finalizer_decision.handled:
            if stream:
                emitter.emit_text_message(finalizer_decision.text)
            elif finalizer_decision.response_obj:
                emitter._json_response = finalizer_decision.response_obj
            else:
                emitter.emit_text_message(finalizer_decision.text)
            emitter.complete()
            if finalizer_decision.log_event:
                APP.log(finalizer_decision.log_event, **finalizer_decision.log_fields)
        else:
            self._send_error_obj(502, f"All continuation finalizers failed for {tool_kind}")

    def _await_streamed_read_finalizer(
        self,
        emitter: ResponseEmitter,
        body: JSON,
        model_alias: str,
        run_finalizer,
        heartbeat_s: Optional[float] = None,
    ):
        """Run a blocking read finalizer while keeping the Responses stream alive."""
        if not emitter._sse_headers_sent:
            emitter.start()

        result_q: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_q.put(("ok", run_finalizer()))
            except Exception as exc:
                result_q.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        interval = heartbeat_s if heartbeat_s is not None else float(os.getenv("SSE_UPSTREAM_HEARTBEAT_SECONDS", "5"))
        while True:
            try:
                kind, value = result_q.get(timeout=interval)
            except queue.Empty:
                try:
                    self._write_in_progress(
                        emitter.response_id,
                        model_alias,
                        emitter.created_at,
                        body,
                        "read_finalizer_wait",
                    )
                except ClientDisconnected:
                    APP.log(
                        "client_disconnected",
                        response_id=emitter.response_id,
                        phase="read_finalizer_wait",
                    )
                    self.close_connection = True
                    return None
                continue
            if kind == "ok":
                return value
            raise value

    def _server_side_read_finalizer_call(self, model_alias: str):
        """Return a bounded no-tool finalizer callable for recovered read evidence."""
        def call(prompt: str, timeout_seconds: float) -> str:
            preferred = os.getenv("OSS_SERVER_SIDE_READ_FINALIZER_MODEL", "").strip()
            default_model = preferred or "deepseek-v4-flash"
            candidates = [default_model]
            if model_alias not in candidates:
                candidates.append(model_alias)
            for fallback in APP.continuation_fallbacks:
                if fallback not in candidates:
                    candidates.append(fallback)
            deadline = time.monotonic() + max(1.0, float(timeout_seconds or 1.0))
            last_error: Exception | None = None
            for candidate in candidates:
                remaining = deadline - time.monotonic()
                if remaining <= 1.0:
                    break
                payload = {
                    "model": map_model(candidate, APP.model_map),
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "tools": [],
                    "_codex_force_non_stream": True,
                }
                try:
                    response = APP.call_continuation_with_deadline(payload, min(APP.continuation_deadline, remaining))
                    return response.get("choices", [{}])[0].get("message", {}).get("content", "")
                except Exception as exc:
                    last_error = exc
                    APP.log("server_side_read_finalizer_candidate_failed", model=candidate, error=str(exc))
            if last_error:
                raise last_error
            raise TimeoutError("server-side read finalizer deadline exhausted")
        return call

    def _handle_fresh_turn(self, body: JSON) -> None:
        """Fallback for when continuation path can't handle the request."""
        payload, base_messages, model_alias, model_upstream, reverse_name_map = APP.prepare_chat_payload(body)
        if self._emit_raw_write_demotion_if_needed(body, base_messages, model_alias):
            return
        prev_id = str(body.get("previous_response_id") or "")
        handoff_text = _extract_handoff_text(base_messages)
        handoff_mode = select_mode(parse_task_envelope(handoff_text)) if handoff_text else ""
        if handoff_mode in {"bounded_write_exact", "bounded_write_patch"} and not legacy_direct_write_modes_enabled():
            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
            emitter.emit_text_message(build_legacy_write_demoted_report(handoff_mode))
            emitter.complete()
            APP.log("legacy_direct_write_fresh_demoted", mode=handoff_mode)
            return
        envelope = parse_task_envelope(handoff_text) if handoff_text else {}
        if (
            os.getenv("OSS_FRESH_SERVER_SIDE_READ_FLOOR", "1") != "0"
            and handoff_mode in ("context_pack", "context_pack_report")
            and declared_read_floor_only(envelope)
            and not _has_evidence_ledger(body)
        ):
            report_text = complete_declared_reads_from_bridge(
                parent_response_id=new_id("resp"),
                messages=base_messages,
                handoff_text=handoff_text,
                project_root=os.getcwd(),
                finalizer_call=self._server_side_read_finalizer_call(model_alias),
                finalizer_timeout_seconds=float(os.getenv("OSS_SERVER_SIDE_READ_FINALIZER_TIMEOUT_SECONDS", "30")),
                reason="declared_read_floor_completed_by_bridge",
                log_fn=APP.log,
            )
            if report_text:
                APP.log("fresh_server_side_read_floor_complete", mode=handoff_mode)
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(report_text)
                emitter.complete()
                return
        if prev_id:
            replay_limit = int(os.getenv("OSS_PENDING_REPLAY_TERMINAL_REPLAYS", "2"))
            terminal_child = APP.state.find_terminal_pending_child(prev_id, replay_limit)
            if terminal_child:
                handoff_text = _extract_handoff_text(base_messages)
                report_text = ""
                report_text = execute_pending_owned_write_from_bridge(
                    parent_response_id=prev_id,
                    child_state=terminal_child,
                    handoff_text=handoff_text,
                    project_root=os.getcwd(),
                    log_fn=APP.log,
                )
                report_text = report_text or execute_pending_owned_shell_from_bridge(
                    parent_response_id=prev_id,
                    child_state=terminal_child,
                    handoff_text=handoff_text,
                    project_root=os.getcwd(),
                    log_fn=APP.log,
                )
                report_text = report_text or execute_pending_owned_verification_from_bridge(
                    parent_response_id=prev_id,
                    child_state=terminal_child,
                    handoff_text=handoff_text,
                    project_root=os.getcwd(),
                    log_fn=APP.log,
                )
                if os.getenv("OSS_SERVER_SIDE_READ_FALLBACK", "1") != "0":
                    report_text = report_text or complete_pending_reads_from_bridge(
                        parent_response_id=prev_id,
                        child_state=terminal_child,
                        handoff_text=handoff_text,
                        project_root=os.getcwd(),
                        finalizer_call=self._server_side_read_finalizer_call(model_alias),
                        finalizer_timeout_seconds=float(os.getenv("OSS_SERVER_SIDE_READ_FINALIZER_TIMEOUT_SECONDS", "30")),
                        log_fn=APP.log,
                    )
                if not report_text:
                    report_text = build_pending_child_not_fulfilled_report(
                        parent_response_id=prev_id,
                        child_state=terminal_child,
                        handoff_text=handoff_text,
                    )
                APP.log(
                    "fresh_turn_semantic_pending_terminal",
                    parent_response_id=prev_id,
                    pending_response_id=terminal_child.response_id,
                    replay_count=terminal_child.pending_replay_count,
                )
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(report_text)
                emitter.complete()
                return
        if body.get("stream"):
            self._send_sse_with_upstream(body, payload, base_messages, model_alias, model_upstream, reverse_name_map)
        else:
            chat_resp, model_used = self._call_upstream_with_fallback(payload, model_alias, model_upstream)
            resp_obj = APP.build_response_object(body, chat_resp, base_messages, model_alias, model_used, reverse_name_map)
            self._send_json(200, resp_obj)

    def _call_upstream_with_fallback(self, payload: JSON, model_alias: str, model_upstream: str) -> Tuple[JSON, str]:
        # Circuit breaker check
        fallback_from_breaker = APP._check_circuit_breaker(model_upstream)
        if fallback_from_breaker:
            fb_payload = dict(payload)
            fb_payload["model"] = map_model(str(fallback_from_breaker), APP.model_map)
            APP.log("circuit_breaker_routing", from_model=model_upstream, to_model=fb_payload["model"])
            try:
                return APP.call_upstream_chat(fb_payload), fb_payload["model"]
            except UpstreamError:
                pass  # Fall through to normal fallback

        try:
            return APP.call_upstream_chat(payload), model_upstream
        except UpstreamError as first_err:
            APP._record_model_error(model_upstream)
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
            body=body,
            base_messages=base_messages,
            model_alias=model_alias,
            model_upstream=model_upstream,
            reverse_name_map=reverse_name_map,
            response_id=response_id,
            created_at=created_at,
            write_sse=self._write_sse,
            write_progress=lambda note: self._write_in_progress(response_id, model_alias, created_at, body, note),
            state_put=APP.state.put,
            stored_response_factory=StoredResponse,
            build_response_shell=APP.build_response_shell,
            repair_chat_history=repair_chat_history,
            extract_budget=_extract_budget,
            restore_tool_name=restore_tool_name,
            new_id=new_id,
            json_dumps=json_dumps,
            as_text=as_text,
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
        response_id = str(resp_obj.get("id") or "")
        created_response = {**resp_obj, "status": "in_progress", "output": []}
        self._write_sse(
            "response.created",
            {
                "type": "response.created",
                "response": created_response,
                "response_id": response_id,
                "sequence_number": 1,
            },
        )
        self._write_sse(
            "response.in_progress",
            {
                "type": "response.in_progress",
                "response": created_response,
                "response_id": response_id,
                "sequence_number": 2,
            },
        )
        self._emit_sse_items_and_completed(resp_obj, sequence_start=2)

    def _emit_sse_items_and_completed(self, resp_obj: JSON, *, sequence_start: int = 0) -> None:
        sequence_number = sequence_start
        response_id = str(resp_obj.get("id") or "")

        def emit(event: str, payload: JSON) -> None:
            nonlocal sequence_number
            sequence_number += 1
            enriched = dict(payload)
            enriched.setdefault("response_id", response_id)
            enriched.setdefault("sequence_number", sequence_number)
            self._write_sse(event, enriched)

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

            emit(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": idx, "item": item_for_added},
            )

            if item_type == "message":
                content = item.get("content") or []
                if content:
                    part = content[0]
                    emit(
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
                        emit(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "output_index": idx,
                                "content_index": 0,
                                "delta": delta,
                                "item_id": item.get("id"),
                            },
                        )
                    emit(
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "output_index": idx,
                            "content_index": 0,
                            "text": text,
                            "item_id": item.get("id"),
                        },
                    )
                    emit(
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
                # TODO(adoption-probes): Create ResponsesToolStateMachine per response
                # and track adoption via codex_oss.tool_call_adoption when
                # function_call_output is received. See WS1 in the plan.
                for start in range(0, len(args), arg_chunk_size):
                    delta = args[start : start + arg_chunk_size]
                    emit(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": idx,
                            "item_id": item.get("id"),
                            "delta": delta,
                        },
                    )
                emit(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": idx,
                        "item_id": item.get("id"),
                        "name": item.get("name"),
                        "arguments": args,
                    },
                )

            emit(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": idx, "item": item},
            )

        completed_response = resp_obj
        if os.getenv("SSE_COMPACT_COMPLETED_FOR_TOOL_CALLS", "0") == "1":
            completed_response = self._compact_completed_response(resp_obj)

        emit("response.completed", {"type": "response.completed", "response": completed_response})
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

    # ── v8: Classification tests ──
    assert classify_request_kind({"input": [{"role": "user", "content": "hello"}]}) == "fresh_user_turn"
    assert classify_request_kind({"input": [{"type": "function_call_output", "call_id": "x", "output": "y"}]}) == "orphan_tool_result_continuation"
    assert classify_request_kind({"input": [{"type": "function_call_output", "call_id": "x", "output": "y"}], "previous_response_id": "abc"}) == "tool_result_continuation"
    assert classify_request_kind({"previous_response_id": "abc"}) == "resumed_user_turn"

    # Tool classification
    assert classify_tool_call_name("rtk_read") == "read"
    assert classify_tool_call_name("exec_command") == "shell"
    assert classify_tool_call_name("write_to_file") == "write"
    assert classify_tool_call_name("apply_patch") == "write"
    assert classify_tool_call_name("unknown_tool") == "unknown"

    # Tool output compaction
    small = compact_tool_output("short output", max_chars=1000, max_lines=10)
    assert not small.is_compacted
    assert small.compacted == "short output"

    big = compact_tool_output("\n".join(f"line {i}" for i in range(500)), max_chars=200, max_lines=10)
    assert big.is_compacted
    assert "TOOL OUTPUT COMPACTED" in big.compacted
    assert "line 0" in big.compacted
    assert big.original_lines == 500

    # Deterministic write report
    report = build_deterministic_write_report("ocg-deepseek-v4-pro", "write_to_file", "/tmp/test.txt", True)
    assert "OSS write completed" in report["content"][0]["text"]
    assert "MEDIUM" in report["content"][0]["text"]

    report_fail = build_deterministic_write_report("ocg-deepseek-v4-pro", "write_to_file", "/tmp/test.txt", False)
    assert "OSS write failed" in report_fail["content"][0]["text"]

    # Deterministic error report
    error_report = build_deterministic_error_report("file_not_found", "src/missing.js does not exist")
    assert "OSS tool failed" in error_report["content"][0]["text"]
    assert "file_not_found" in error_report["content"][0]["text"]

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
    httpd.daemon_threads = False

    shutdown_started = threading.Event()

    def shutdown(signum, frame):
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        SHUTDOWN_REQUESTED.set()
        try:
            with ACTIVE_REQUESTS_COND:
                active = ACTIVE_REQUESTS
            sys.stderr.write(f"shutting down signal={signum} active_requests={active}\n")
            sys.stderr.flush()
        except Exception:
            pass
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Responses->Chat proxy listening on http://{args.host}:{args.port}/v1", file=sys.stderr)
    httpd.serve_forever()
    drain_deadline = time.time() + float(os.getenv("BRIDGE_SHUTDOWN_DRAIN_SECONDS", "180"))
    with ACTIVE_REQUESTS_COND:
        while ACTIVE_REQUESTS > 0 and time.time() < drain_deadline:
            remaining = max(0.1, drain_deadline - time.time())
            ACTIVE_REQUESTS_COND.wait(timeout=min(1.0, remaining))
        active = ACTIVE_REQUESTS
    if active:
        print(f"shutdown drain expired active_requests={active}", file=sys.stderr)
    httpd.server_close()


if __name__ == "__main__":
    main()
