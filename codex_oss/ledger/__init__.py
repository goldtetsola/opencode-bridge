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
    chars_total: int = 0
    chars_returned: int = 0
    sha256: str = ""
    tool: str = ""
    turn: int = 0
    risk_flags: List[str] = field(default_factory=list)


@dataclass
class CommandEntry:
    tool: str
    args: dict
    exit_code: int
    stdout_sha256: str = ""
    matches_count: int = 0
    turn: int = 0


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

    def add_file(self, path: str, result: "ToolResult", turn: int):
        entry = FileEntry(
            path=path,
            complete=result.complete,
            chars_total=result.chars_total,
            chars_returned=len(result.stdout),
            sha256=result.sha256,
            tool=result.tool,
            turn=turn,
            risk_flags=list(result.risk_flags),
        )
        self.files_inspected[path] = entry
        self.total_bytes_read += result.chars_total
        if result.redactions_applied:
            self.redactions_applied = True

    def add_command(self, tool: str, args: dict, result: "ToolResult", turn: int):
        entry = CommandEntry(
            tool=tool, args=args, exit_code=result.exit_code,
            stdout_sha256=result.sha256,
            matches_count=result.stdout.count("\n") if result.stdout else 0,
            turn=turn,
        )
        self.commands_run.append(entry)

    def add_risk_flag(self, flag: str):
        if flag not in self.risk_flags:
            self.risk_flags.append(flag)

    def is_duplicate(self, path: str) -> bool:
        entry = self.files_inspected.get(path)
        return entry is not None and entry.complete

    def record_duplicate(self):
        self.duplicate_actions_blocked += 1

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
