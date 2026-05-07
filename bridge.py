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
    tool_exchange_count: int = 0  # v10: tracks turn count for budget enforcement
    task_max_exchanges: int = 1  # v10: per-task-class budget
    read_ledger_json: str = ""  # v10: comma-sep read paths
    command_ledger_json: str = ""  # v10: pipe-sep commands


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
            return paths
    for sep in ("READ-ONLY PATHS:", "OWNED PATHS:", "ALLOWED PATHS:"):
        if sep in handoff_text:
            after = _extract_segment(handoff_text, sep)
            parts = _parse_path_list(after)
            paths.extend([p for p in parts if p and not p.lower().startswith(("no ", "none", "do not", "git ")) and len(p) > 1])
            break
    return paths


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
                return (None, str(val) if isinstance(val, str) else json.dumps(val))

    return (None, json.dumps(args))


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
            if name in ("rtk_read", "read", "cat"):
                args = func.get("arguments", "{}")
                path, _ = normalize_tool_args(args, name)
                if path:
                    paths.add(path)
        # Also check codex-format tool calls
        codex_tc = msg.get("codex")
        if codex_tc and isinstance(codex_tc, dict):
            codex_name = codex_tc.get("name", "")
            if codex_name in ("rtk_read", "read", "cat"):
                path, _ = normalize_tool_args(codex_tc.get("arguments", "{}"), codex_name)
                if path:
                    paths.add(path)
    return paths


def _extract_handoff_text(messages: list) -> str:
    """Extract the most likely current OSS handoff, not the whole history."""
    best_text = ""
    best_score = 0
    for msg in messages:
        if msg.get("role") in ("system", "developer", "user") and msg.get("content"):
            text = str(msg["content"])
            score = _handoff_score(text)
            if score >= best_score and score >= 2:
                best_text = text
                best_score = score
    if best_text:
        return best_text
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
    paths = extract_allowed_paths(handoff_text)
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
    paths = envelope.get("read_only_paths") or extract_allowed_paths(handoff_text)
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
        root = os.path.abspath(project_root)
        full = os.path.abspath(os.path.join(project_root, path))
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(f"path escapes project root: {path}")
        return full

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
        "Synthesis status: FALLBACK\n"
        "Task status: PARTIAL\n"
        f"Command used: {command}\n"
        f"Files gathered: {files}\n"
        f"Summary:\n{summary}\n"
        f"Evidence snippets:\n{evidence}\n"
        f"Requested deliverable: {outputs}\n"
        f"{evidence_coverage}\n"
        f"{deliverable_sections}\n"
        "Confidence: MEDIUM\n"
        "Caveats: deterministic bridge fallback produced this report from the gathered source pack because model synthesis was unavailable; task completion is not certified."
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
                normalized.append(os.path.relpath(p, cwd))
            else:
                normalized.append(p)
        else:
            normalized.append(p)
    return normalized


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


def is_intent_or_status(text: str) -> bool:
    if len(text) < MIN_REPORT_LENGTH:
        return True
    import re
    intent_match = re.search(INTENT_PATTERNS, text, re.IGNORECASE)
    if intent_match:
        # Evidence markers must be report-structure indicators, not just common words
        report_markers = ("oss_report_begin", "pass\n", "fail\n", "status:", "confidence:", "caveat:",
                          "files inspected:", "commands run:", "commands used:")
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
    if any(token in lowered for token in ("traceback (most recent call last)", "assertionerror", "syntaxerror")):
        return True
    return False


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
                (response_id, model_alias, model_upstream, messages_json, pending_call_ids_json, created_at, tool_exchange_count, task_max_exchanges)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                "COALESCE(tool_exchange_count, 0), COALESCE(task_max_exchanges, 1) FROM responses WHERE response_id = ?",
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


# ── End v8 preamble ──


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

        # ── Bridge v7 hardening ──

        # Fatal missing key in production mode
        if self.gpt_model_strategy == "error" and not self.upstream_key:
            if os.getenv("ALLOW_MISSING_OPENCODE_KEY", "0") != "1":
                print("FATAL: OPENCODE_GO_API_KEY is not set and GPT_MODEL_STRATEGY=error.", file=sys.stderr)
                print("Set OPENCODE_GO_API_KEY or start with ALLOW_MISSING_OPENCODE_KEY=1", file=sys.stderr)
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
        start = time.time()
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
            "User-Agent": "codex-opencode-go-responses-proxy/8.0",
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
                task_max_exchanges=_extract_budget(base_messages),
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
START_TIME = time.time()



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
                task_max_exchanges=_extract_budget(self.base_messages),
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


from codex_oss.transport.emitter import ResponseEmitter


def _extract_handoff_from_body(body: JSON) -> str:
    """Extract the full handoff text from body input items (system + user messages)."""
    text = ""
    for item in body.get("input", []):
        role = item.get("role", "")
        content = item.get("content", "")
        if role in ("system", "developer", "user") and content:
            if isinstance(content, str):
                text += content + "\n"
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text += part.get("text", "") + "\n"
    return text


class Handler(BaseHTTPRequestHandler):
    server_version = "ResponsesChatProxy/12.0"

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
            model_health = {}
            for model, h in APP.model_health.items():
                model_health[model] = {
                    "status": h.get("status", "ok"),
                    "errors": h.get("errors", 0),
                    "since": h.get("since", 0),
                }
            status_info = {
                "ok": True,
                "service": "responses-chat-proxy",
                "bridge_version": BRIDGE_VERSION,
                "time": now(),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "uptime_seconds": int(time.time() - START_TIME) if 'START_TIME' in dir() else 0,
                "argv": sys.argv,
                "supervisor": {
                    "mode": os.getenv("CODEX_OSS_SUPERVISOR_MODE", "unknown"),
                    "durable": os.getenv("CODEX_OSS_SUPERVISOR_MODE", "") != "",
                },
                "gpt_model_strategy": APP.gpt_model_strategy,
                "upstream_stream": getattr(APP, "upstream_streaming", True),
                "has_opencode_key": bool(APP.upstream_key),
                "state_db": getattr(APP.state, "path", os.getenv("PROXY_STATE_DB", "unknown")),
                "model_health": model_health,
                "concurrency": {
                    "max_global": APP.max_global_concurrency,
                },
                "transport_contract": {
                    "stream_terminal_guarantee": True,
                },
            }
            self._send_json(200, status_info)
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

            # ── v1 spec: A2/A3 managed investigation via runtime loop ──
            handoff = _extract_handoff_from_body(body)
            if handoff and "oss_agent_mission.v1" in handoff:
                try:
                    from codex_oss.mission import parse_mission_v1, InvalidHandoffError
                    from codex_oss.runtime.loop import run_loop
                    from codex_oss.ledger import EvidenceLedger
                    from codex_oss.validation import validate_report, render_report

                    mission = parse_mission_v1(handoff)
                    APP.log("mission_dispatch", tier=mission.tier, mode=mission.mode,
                            mission_id=mission.mission_id)

                    if mission.tier in ("A2", "A3"):
                        ledger = EvidenceLedger(mission_id=mission.mission_id,
                                                tool_budget_remaining=mission.tool_budget)
                        deadline = float(os.getenv("REQUEST_DEADLINE_SECONDS", "90"))

                        def _call_model(messages, tools, timeout):
                            payload = {
                                "model": map_model(raw_model_alias or "ocg-kimi-k2.6", APP.model_map),
                                "messages": messages,
                                "stream": False,
                            }
                            return APP.call_continuation_with_deadline(payload, timeout)

                        result = run_loop(mission, ledger, _call_model, [],
                                         None, mission.allowed_roots, mission.allowed_paths,
                                         request_deadline=deadline)

                        report = result.get("report", {})
                        status = result.get("status", "PARTIAL")
                        report_text = render_report(report) if isinstance(report, dict) else str(report)

                        emitter = ResponseEmitter(self, new_id("resp"), raw_model_alias, bool(body.get("stream")))
                        emitter.emit_text_message(report_text)
                        emitter.complete()
                        return
                except InvalidHandoffError as e:
                    APP.log("mission_invalid", error=str(e))
                    emitter = ResponseEmitter(self, new_id("resp"), raw_model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(
                        f"FAIL\nReason: invalid OSS handoff schema.\nSchema error: {e}\n"
                        f"Confidence: HIGH — bridge rejected a malformed structured handoff before executing delegated work.")
                    emitter.complete()
                    return
                except Exception as e:
                    APP.log("mission_crash", error=str(e))

            # ── v8: Check for continuation BEFORE prepare_chat_payload ──
            request_kind = classify_request_kind(body)
            if request_kind in ("tool_result_continuation", "orphan_tool_result_continuation"):
                self._handle_continuation(body)
                return

            payload, base_messages, model_alias, model_upstream, reverse_name_map = APP.prepare_chat_payload(body)

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

        reverse_name_map = {}
        if prev_state:
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

        # Count turn and determine budget
        if prev_state:
            turn = prev_state.tool_exchange_count + 1
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

        handoff_text = _extract_handoff_text(prev_state.messages) if prev_state else ""
        envelope = parse_task_envelope(handoff_text)
        mode = select_mode(envelope)
        _, _, target_path = _find_tool_call_details(prev_state, tool_call_id)

        if mode == "bounded_write_patch" and tool_kind == "shell":
            project_root = os.getcwd()
            changed_paths = collect_owned_path_changes(envelope.get("owned_paths", []), project_root)
            failed = exit_code != 0 or tool_output_indicates_failure(tool_output_text)
            if not changed_paths:
                status = "FAIL" if failed else "PARTIAL"
                reason = "verification tool was observed, but no changed owned path was visible in git status"
            elif failed:
                status = "FAIL"
                reason = "verification tool output indicated failure"
            else:
                status = "PASS"
                reason = "owned path changes and verification tool output were both observed"
            text = build_patch_contract_report(
                envelope, changed_paths, status, reason,
                verification_seen=True, verification_output=compacted.compacted)
            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
            emitter.emit_text_message(text)
            emitter.complete()
            APP.log("bounded_patch_verification_complete", status=status, changed_paths=changed_paths)
            return

        if mode == "bounded_write_patch" and tool_kind == "write" and exit_code == 0:
            project_root = os.getcwd()
            owned_paths = envelope.get("owned_paths", [])
            if target_path and not _path_is_within_owned_paths(target_path, owned_paths, project_root):
                text = build_patch_contract_report(
                    envelope, [], "FAIL",
                    f"write target {target_path} is outside the declared owned paths")
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(text)
                emitter.complete()
                APP.log("bounded_patch_scope_violation", target_path=target_path, owned_paths=owned_paths)
                return
            changed_paths = collect_owned_path_changes(owned_paths, project_root)
            if not changed_paths:
                text = build_patch_contract_report(
                    envelope, [], "FAIL",
                    "write tool returned success but no changed owned path was visible in git status")
                emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                emitter.emit_text_message(text)
                emitter.complete()
                APP.log("bounded_patch_no_owned_change", owned_paths=owned_paths)
                return
            if turn < max_exchanges and prev_state:
                ledger_text = (
                    "PATCH CONTRACT LEDGER\n"
                    "Execution mode: bounded_write_patch\n"
                    f"Owned paths: {', '.join(owned_paths)}\n"
                    f"Changed owned paths observed: {', '.join(changed_paths)}\n"
                    f"Required outputs: {', '.join(envelope.get('deliverable_fields', []))}\n"
                    f"Verification steps: {', '.join(envelope.get('verification_steps', []))}\n"
                    "Next: run the requested verification if possible, then return the final report. "
                    "Do not edit outside the owned paths."
                )
                input_items = list(body.get("input", []))
                input_items.insert(0, {"role": "system", "content": ledger_text})
                body["input"] = input_items
                APP.log("bounded_patch_continue_for_verification", changed_paths=changed_paths)
                self._handle_fresh_turn(body)
                if prev_id:
                    refreshed = APP.state.get(str(prev_id))
                    if refreshed:
                        refreshed.tool_exchange_count = turn
                        refreshed.task_max_exchanges = max_exchanges
                        APP.state.put(refreshed)
                return
            text = build_patch_contract_report(
                envelope, changed_paths, "PARTIAL",
                "owned path changes were observed, but verification was not observed before the tool budget ended")
            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
            emitter.emit_text_message(text)
            emitter.complete()
            APP.log("bounded_patch_partial_no_verification", changed_paths=changed_paths)
            return

        # Deterministic close for writes and errors (even under budget)
        # Writers always close deterministically after first write
        if exit_code != 0 or tool_kind == "write":
            report = build_deterministic_write_report(
                model=model_alias, tool_name=tool_name_raw,
                path=tool_call_id,
                success=(exit_code == 0)) if exit_code != 0 or tool_kind == "write" else \
                build_deterministic_error_report(tool_kind, compacted.compacted[:200])
            APP.log("continuation_deterministic_close", tool_kind=tool_kind)
            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
            emitter.emit_text_message(report["content"][0]["text"])
            emitter.complete()
            return

        # Continue with tools if budget remains (v10: managed autonomy)
        if turn < max_exchanges and prev_state:
            APP.log("continuation_continue", turn=turn, max_exchanges=max_exchanges)

            # Determine execution mode from handoff
            if not handoff_text:
                handoff_text = _extract_handoff_text(prev_state.messages)
                envelope = parse_task_envelope(handoff_text)
                mode = select_mode(envelope)
            APP.log("execution_mode", mode=mode, paths=envelope.get("read_only_paths", []))
            read_paths = _extract_read_paths_from_history(prev_state.messages)

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

            if mode in ("context_pack", "context_pack_report") and not _has_evidence_ledger(body):
                context_pack_attempted = True
                APP.log("mode_context_pack", envelope=envelope.get("read_only_paths", []))
                # ... (existing context-pack code continues below) ...

            # Bounded exact write mode: runtime handles it
            if mode == "bounded_write_exact" and not context_pack_attempted:
                context_pack_attempted = True
                APP.log("mode_bounded_write_exact")
                owned = envelope.get("owned_paths", [])
                if owned:
                    path = owned[0]
                    exact_content = envelope.get("exact_content", "")
                    if exact_content and not os.path.exists(os.path.normpath(os.path.join(os.getcwd(), path))):
                        full = os.path.normpath(os.path.join(os.getcwd(), path))
                        try:
                            os.makedirs(os.path.dirname(full), exist_ok=True)
                            with open(full, "w", encoding="utf-8") as f:
                                f.write(exact_content + "\n")
                            APP.log("bounded_write_runtime_write", path=path, bytes=len(exact_content) + 1)
                        except Exception as e:
                            APP.log("bounded_write_runtime_write_failed", path=path, error=str(e))
                    try:
                        full = os.path.normpath(os.path.join(os.getcwd(), path))
                        observed = open(full).read().strip()
                        if observed != exact_content:
                            report_text = (
                                f"FAIL\n"
                                f"File checked: {path}\n"
                                f"Observed content: {observed}\n"
                                f"Expected content: {exact_content}\n"
                                f"Confidence: HIGH — deterministic read-back did not match declared exact_content; "
                                f"Caveat: final OSS model report bypassed by bridge runtime (mode={mode})."
                            )
                            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                            emitter.emit_text_message(report_text)
                            emitter.complete()
                            APP.log("bounded_write_mismatch", path=path)
                            return
                        report_text = (
                            f"PASS\n"
                            f"File changed: {path}\n"
                            f"Observed content: {observed}\n"
                            f"Confidence: HIGH — deterministic read-back after tool execution; "
                            f"Caveat: final OSS model report bypassed by bridge runtime (mode={mode})."
                        )
                        emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                        emitter.emit_text_message(report_text)
                        emitter.complete()
                        APP.log("bounded_write_complete", path=path)
                        return
                    except Exception as e:
                        APP.log("bounded_write_failed", error=str(e))
                        if tool_kind != "write":
                            context_pack_attempted = False
                            APP.log("bounded_write_waiting_for_write", path=path, tool_kind=tool_kind)
                            # Let the model continue to the actual write tool call.
                        else:
                            report_text = (
                                f"FAIL\n"
                                f"File changed: {path}\n"
                                f"Observed content: [read-back failed: {e}]\n"
                                f"Confidence: LOW — assigned file could not be read after tool execution; "
                                f"Caveat: no model finalizer was allowed to infer success."
                            )
                            emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                            emitter.emit_text_message(report_text)
                            emitter.complete()
                            return

            if mode in ("context_pack", "context_pack_report") and not _has_evidence_ledger(body):
                context_pack_attempted = True
                APP.log("context_pack", mode=mode, paths=extract_allowed_paths(handoff_text))
                session = build_task_session(body, handoff_text, prev_id or "unknown")
                # Track what's been read from history
                read_paths = _extract_read_paths_from_history(prev_state.messages)
                session.read_paths = {p: {"complete": True} for p in read_paths}
                # Build context pack with remaining files
                pack = build_context_pack(session, os.getcwd())
                evidence_coverage_complete, _, _, _ = evaluate_evidence_coverage(envelope, pack)
                # Send as no-tools finalizer call
                finalizer_payload = {
                    "model": map_model(APP.continuation_model, APP.model_map),
                    "messages": [{"role": "system", "content":
                        f"You are producing a report from the provided source pack.\n"
                        f"Required outputs: {', '.join(session.required_outputs)}\n\n"
                        f"SOURCE PACK:\n{pack}\n\n"
                        f"Do not request tools. Produce a structured report including all required outputs. "
                        f"Distinguish source-document claims, planned success criteria, and actually observed verification. "
                        f"Do not say tests passed, commands ran, files changed, or routing occurred unless the source pack explicitly contains that executed result."}],
                    "stream": False,
                    "tools": [],
                }
                try:
                    chat_resp = APP.call_continuation_with_deadline(
                        finalizer_payload, APP.continuation_deadline)
                    text = chat_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    # v12: Reject intent/status text and reports that do not satisfy the task contract.
                    if is_intent_or_status(text):
                        APP.log("intent_rejected", text_len=len(text))
                        remaining = request_deadline - (time.time() - request_start)
                        if remaining > 20:
                            retry_payload = {
                                "model": finalizer_payload["model"],
                                "messages": finalizer_payload["messages"] + [
                                    {"role": "user", "content":
                                     "You returned status/intent text instead of a report. "
                                     "That is invalid. Return the final report now. Do not describe future actions. "
                                     "Include PASS, FAIL, or PARTIAL; confidence; caveats; and every required output field."}
                                ],
                                "stream": False,
                                "tools": [],
                            }
                            try:
                                chat_resp = APP.call_continuation_with_deadline(retry_payload, APP.continuation_deadline * 0.7)
                                text = chat_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                                APP.log("intent_retry_ok", text_len=len(text))
                            except Exception:
                                text = "PARTIAL\nConfidence: LOW\nCaveats: model returned intent/status text; retry failed."
                                APP.log("intent_retry_failed")
                    if is_intent_or_status(text):
                        APP.log("context_pack_intent_fallback", text_len=len(text))
                        text = build_context_pack_deterministic_report(session, pack, tool_output_text)
                    else:
                        is_valid, missing = validate_report_output(
                            text, mode, envelope,
                            evidence_coverage_complete=evidence_coverage_complete)
                        if not is_valid:
                            APP.log("context_pack_report_contract_fallback", missing=missing, text_len=len(text))
                            text = build_context_pack_deterministic_report(session, pack, tool_output_text)
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(text)
                    emitter.complete()
                    APP.log("context_pack_report_complete", text_len=len(text))
                    return
                except Exception as e:
                    APP.log("context_pack_failed", error=str(e))
                    text = ""
                    for fb_model in APP.continuation_fallbacks:
                        remaining = request_deadline - (time.time() - request_start)
                        if remaining <= 5:
                            break
                        fallback_payload = dict(finalizer_payload)
                        fallback_payload["model"] = map_model(fb_model, APP.model_map)
                        try:
                            APP.log("context_pack_fallback_try", fallback_model=fallback_payload["model"])
                            chat_resp = APP.call_continuation_with_deadline(
                                fallback_payload, min(APP.continuation_deadline, remaining - 2))
                            text = chat_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                            is_valid, missing = (
                                validate_report_output(
                                    text, mode, envelope,
                                    evidence_coverage_complete=evidence_coverage_complete)
                                if text else (False, ["empty_report"])
                            )
                            if text and is_valid:
                                APP.log("context_pack_fallback_ok", fallback_model=fallback_payload["model"], text_len=len(text))
                                break
                            APP.log("context_pack_fallback_invalid", fallback_model=fallback_payload["model"], missing=missing, text_len=len(text))
                            text = ""
                        except Exception as fallback_error:
                            APP.log("context_pack_fallback_failed", fallback_model=fallback_payload["model"], error=str(fallback_error))
                    if not text:
                        text = build_context_pack_deterministic_report(session, pack, tool_output_text)
                    emitter = ResponseEmitter(self, new_id("resp"), model_alias, bool(body.get("stream")))
                    emitter.emit_text_message(text)
                    emitter.complete()
                    APP.log("context_pack_degraded_report", text_len=len(text))
                    return

            # Skip managed autonomy if context-pack was attempted
            if not context_pack_attempted:
                # Managed autonomy: suppress duplicate reads
                if mode == "managed_autonomy":
                    # Check if current tool call is for an already-read path
                    dup_msg = suppress_duplicate_read(
                        {"name": tool_name_raw, "arguments": json.dumps(
                            body.get("input", [{}])[0] if body.get("input") else {})},
                        TaskSession(task_session_id="", root_response_id="", task_class="",
                                    execution_mode="", max_tool_exchanges=max_exchanges,
                                    read_paths={p: {"complete": True} for p in read_paths},
                                    required_paths=extract_allowed_paths(handoff_text)))
                    if dup_msg:
                        APP.log("duplicate_suppressed", tool=tool_name_raw)
                        input_items = list(body.get("input", []))
                        input_items.insert(0, {"role": "system", "content": f"[RUNTIME SUPPRESSION]\n{dup_msg}"})
                        body["input"] = input_items

                # Inject evidence ledger for managed autonomy
                required_paths = extract_allowed_paths(handoff_text)
                remaining = [p for p in required_paths if p not in read_paths]
                if remaining and not _has_evidence_ledger(body):
                    ledger_text = "EVIDENCE LEDGER\n"
                    if read_paths:
                        ledger_text += f"Already read: {', '.join(sorted(read_paths))}\n"
                    ledger_text += f"Still required: {', '.join(remaining)}\n"
                    ledger_text += f"Tool budget remaining: {max_exchanges - turn}\n"
                    ledger_text += "Do not reread completed files."
                    input_items = list(body.get("input", []))
                    input_items.insert(0, {"role": "system", "content": ledger_text})
                    body["input"] = input_items

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
        finalizer_model = map_model(APP.continuation_model, APP.model_map)
        original_task = ""
        if prev_state:
            for m in prev_state.messages:
                if m.get("role") == "user" and m.get("content"):
                    original_task = str(m["content"])[:500]
                    break

        finalizer_messages: List[JSON] = []
        if prev_state:
            finalizer_messages = list(repair_chat_history(prev_state.messages, tool_outputs))
            finalizer_messages = merge_new_user_messages(finalizer_messages, [])
        finalizer_messages.append({
            "role": "user",
            "content": (
                f"Task: {original_task}\n\n"
                f"Tool executed: {tool_kind} operation\n"
                f"Tool result:\n{compacted.compacted[:APP.max_tool_output_chars]}"
            )
        })

        finalizer_payload = {
            "model": finalizer_model,
            "messages": finalizer_messages,
            "stream": False,  # Non-streaming for finalizer — faster, simpler
        }
        if APP.continuation_tools == "none":
            finalizer_payload["tools"] = []
            finalizer_payload["tool_choice"] = "none"

        APP.log("continuation_finalizer", model=finalizer_model, deadline=APP.continuation_deadline)

        stream = bool(body.get("stream"))
        emitter = ResponseEmitter(self, new_id("resp"), model_alias, stream)

        try:
            chat_resp = APP.call_continuation_with_deadline(
                finalizer_payload, APP.continuation_deadline)
            resp_obj = APP.build_response_object(
                body, chat_resp, finalizer_messages, model_alias,
                map_model(model_alias, APP.model_map), reverse_name_map)
            if stream:
                emitter.start()
                text = ""
                for o in resp_obj.get("output", []):
                    if o.get("type") == "message":
                        text = o["content"][0]["text"]
                        break
                emitter.emit_text_message(text or "Finalizer completed.")
                emitter.complete()
                # Validate report quality
                handoff_text = _extract_handoff_text(finalizer_messages)
                required = extract_required_deliverables(handoff_text)
                is_valid, missing = validate_report(text, required)
                if not is_valid and text:
                    APP.log("report_invalid", missing=missing, text_len=len(text))
                elif not is_valid and not text:
                    APP.log("report_empty")
            else:
                emitter._json_response = resp_obj
                emitter.complete()
            APP.log("continuation_finalizer_ok")
        except Exception as e:
            APP.log("continuation_finalizer_failed", error=str(e))

            # Fallback ladder
            for fb_model_name in APP.continuation_fallbacks:
                fb_model = map_model(fb_model_name, APP.model_map)
                fb_payload = dict(finalizer_payload)
                fb_payload["model"] = fb_model
                try:
                    chat_resp = APP.call_continuation_with_deadline(
                        fb_payload, APP.continuation_deadline * 0.7)
                    resp_obj = APP.build_response_object(
                        body, chat_resp, finalizer_messages, model_alias,
                        map_model(model_alias, APP.model_map), reverse_name_map)
                    if stream:
                        text = ""
                        for o in resp_obj.get("output", []):
                            if o.get("type") == "message":
                                text = o["content"][0]["text"]
                                break
                        if not emitter._sse_headers_sent:
                            emitter.start()
                        emitter.emit_text_message(text or "Fallback finalizer completed.")
                    emitter.complete()
                    APP.log("continuation_fallback_ok", fallback_model=fb_model)
                    return
                except Exception:
                    APP.log("continuation_fallback_failed", model=fb_model)

            # Degraded completion — always return something terminal
            if APP.degraded_completion_on_timeout:
                degraded = APP.build_degraded_completion(model_alias, "all finalizers failed", tool_kind)
                if not emitter._sse_headers_sent:
                    emitter.start()
                emitter.emit_text_message(degraded["content"][0]["text"])
                emitter.complete()
                APP.log("continuation_degraded_complete")
            else:
                self._send_error_obj(502, f"All continuation finalizers failed for {tool_kind}")

    def _handle_fresh_turn(self, body: JSON) -> None:
        """Fallback for when continuation path can't handle the request."""
        payload, base_messages, model_alias, model_upstream, reverse_name_map = APP.prepare_chat_payload(body)
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
