"""EvidenceLedgerV1 — persistent mission state.

Tracks files inspected, commands run, claims, risk flags, duplicates, and budget.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

JSON = Dict[str, Any]


@dataclass
class FileEntry:
    path: str
    complete: bool
    full_content_cached: bool = False
    chars_total: int = 0
    chars_returned: int = 0
    sha256: str = ""
    tool: str = ""
    turn: int = 0
    risk_flags: List[str] = field(default_factory=list)
    extracts: List[dict] = field(default_factory=list)
    cached_text: str = ""


@dataclass
class CommandEntry:
    tool: str
    args: dict
    exit_code: int
    stdout_sha256: str = ""
    matches_count: int = 0
    turn: int = 0
    extracts: List[dict] = field(default_factory=list)
    cached_text: str = ""


@dataclass
class ActionTraceEntry:
    turn: int
    phase: str
    action_type: str
    tool_name: str = ""
    target_question: str = ""
    raw_arguments: dict = field(default_factory=dict)
    normalized_arguments: dict = field(default_factory=dict)
    unsupported_arguments: List[str] = field(default_factory=list)
    model_rationale: str = ""
    hypothesis: str = ""
    expected_information_gain: str = ""
    why_not_report_yet: str = ""
    runtime_decision: str = ""
    decision_reason: str = ""
    information_gain: str = "unknown"
    novelty: str = "unknown"
    specificity: str = "unknown"
    evidence_linkage: bool = False
    broad_searches_used: int = 0
    low_information_actions_used: int = 0
    duplicate_actions_used: int = 0
    deadline_remaining_seconds: float = 0
    budget_remaining: int = 0
    tool_result_summary: dict = field(default_factory=dict)


@dataclass
class EvidenceLedger:
    mission_id: str
    files_inspected: Dict[str, FileEntry] = field(default_factory=dict)
    commands_run: List[CommandEntry] = field(default_factory=list)
    claims: List[dict] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    duplicate_actions_blocked: int = 0
    tool_budget_remaining: int = 20
    total_bytes_read: int = 0
    redactions_applied: bool = False
    action_trace: List[ActionTraceEntry] = field(default_factory=list)
    claim_graph: dict = field(default_factory=dict)
    answer_graph: dict = field(default_factory=dict)
    coverage_graph: dict = field(default_factory=dict)
    evidence_agenda: dict = field(default_factory=dict)

    def add_file(self, path: str, result: "ToolResult", turn: int):
        args = getattr(result, "args", {}) or {}
        is_range_read = "start_line" in args or "end_line" in args
        entry = FileEntry(
            path=path,
            complete=result.complete,
            full_content_cached=bool(result.complete and not is_range_read),
            chars_total=result.chars_total,
            chars_returned=len(result.stdout),
            sha256=result.sha256,
            tool=result.tool,
            turn=turn,
            risk_flags=list(result.risk_flags),
            extracts=_build_extracts(result.stdout),
            cached_text=result.stdout,
        )
        self.files_inspected[path] = entry
        self.total_bytes_read += result.chars_total
        if result.redactions_applied:
            self.redactions_applied = True

    def add_command(self, tool: str, args: dict, result: "ToolResult", turn: int):
        matches_count = result.stdout.count("\n") if result.stdout else 0
        if tool == "rtk_grep" and result.stdout.lower().startswith("0 matches for "):
            matches_count = 0
        entry = CommandEntry(
            tool=tool, args=args, exit_code=result.exit_code,
            stdout_sha256=result.sha256,
            matches_count=matches_count,
            turn=turn,
            extracts=_build_extracts(result.stdout),
            cached_text=result.stdout,
        )
        self.commands_run.append(entry)

    def add_risk_flag(self, flag: str):
        if flag not in self.risk_flags:
            self.risk_flags.append(flag)

    def add_action_trace(self, entry: ActionTraceEntry):
        self.action_trace.append(entry)

    def add_claim(self, claim: dict):
        text = str((claim or {}).get("text") or "").strip()
        if not text:
            return
        existing = next((item for item in self.claims if str(item.get("text") or "").strip() == text), None)
        if existing is None:
            self.claims.append(dict(claim))
            return
        for key, value in dict(claim).items():
            if key == "evidence_refs":
                merged = list(dict.fromkeys(list(existing.get("evidence_refs", []) or []) + list(value or [])))
                existing["evidence_refs"] = merged
            elif value not in (None, "", [], {}):
                existing[key] = value

    def add_open_question(self, question: str, *, priority: str = "normal", evidence_refs: Optional[List[str]] = None):
        text = str(question or "").strip()
        if not text:
            return
        existing = next((item for item in self.open_questions if str(item.get("question") or "").strip() == text), None)
        if existing is None:
            self.open_questions.append({
                "question": text,
                "status": "open",
                "priority": priority,
                "evidence_refs": list(evidence_refs or []),
            })
            return
        if evidence_refs:
            existing["evidence_refs"] = list(dict.fromkeys(list(existing.get("evidence_refs", []) or []) + list(evidence_refs)))
        if priority and existing.get("priority") in (None, "", "normal"):
            existing["priority"] = priority

    def is_duplicate(self, path: str) -> bool:
        entry = self.files_inspected.get(path)
        return entry is not None and entry.complete and entry.full_content_cached

    def add_cached_extract(self, path: str, text: str) -> Optional[str]:
        entry = self.files_inspected.get(path)
        if not entry:
            return None
        extract_id = f"extract:{len(entry.extracts) + 1}"
        entry.extracts.append({"id": extract_id, "text": text})
        return f"file:{path}#{extract_id}"

    def record_duplicate(self):
        self.duplicate_actions_blocked += 1

    def is_duplicate_command(self, tool: str, args: dict) -> bool:
        normalized = _canonical_command_args(args)
        return any(c.tool == tool and _canonical_command_args(c.args) == normalized for c in self.commands_run)

    def spend_budget(self):
        self.tool_budget_remaining = max(0, self.tool_budget_remaining - 1)

    def summary(self, max_chars: int = 6000) -> str:
        """Compact ledger summary for model context."""
        lines = [f"Mission: {self.mission_id}", f"Budget remaining: {self.tool_budget_remaining}"]
        if self.files_inspected:
            lines.append("Files inspected:")
            for p, e in self.files_inspected.items():
                status = "complete" if e.complete else f"partial ({e.chars_returned}/{e.chars_total})"
                lines.append(f"  {p} [{status}]")
        if self.commands_run:
            lines.append("Commands run:")
            for c in self.commands_run[-5:]:  # most recent 5
                lines.append(f"  {c.tool} {json.dumps(c.args)} (exit={c.exit_code})")
        if self.risk_flags:
            lines.append(f"Risk flags: {', '.join(self.risk_flags)}")
        if self.duplicate_actions_blocked:
            lines.append(f"Duplicates blocked: {self.duplicate_actions_blocked}")
        result = "\n".join(lines)
        return result[:max_chars]


def _build_extracts(stdout: str, max_extracts: int = 3, max_chars: int = 1200) -> List[dict]:
    if not stdout:
        return []
    chunks = []
    text = stdout[:max_chars]
    chunks.append({"id": "extract:1", "text": text})
    if len(stdout) > max_chars and max_extracts > 1:
        chunks.append({"id": "extract:2", "text": stdout[-max_chars:]})
    return chunks[:max_extracts]


def _canonical_command_args(args: dict) -> dict:
    cleaned = dict(args or {})
    for noise in ("recurse", "recursive"):
        cleaned.pop(noise, None)
    return cleaned
