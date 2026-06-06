"""ToolTurnTransactionV1 state helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JSON = dict[str, Any]


TERMINAL_STATES = {"resolved", "recovered", "failed_closed"}


@dataclass
class ToolTurnTransaction:
    call_id: str
    tool_name: str
    state: str = "started"
    recovery_guidance: str = ""
    events: list[JSON] = field(default_factory=list)

    def resolve(self, output_ref: str) -> "ToolTurnTransaction":
        self.state = "resolved"
        self.events.append({"event": "resolved", "output_ref": output_ref})
        return self

    def recover(self, reason: str) -> "ToolTurnTransaction":
        self.state = "recovered"
        self.events.append({"event": "recovered", "reason": reason})
        return self

    def fail_closed(self, guidance: str) -> "ToolTurnTransaction":
        self.state = "failed_closed"
        self.recovery_guidance = guidance
        self.events.append({"event": "failed_closed", "guidance": guidance})
        return self

    def terminalized(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_record(self) -> JSON:
        return {
            "schema_version": "tool_turn_transaction.v1",
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "state": self.state,
            "terminalized": self.terminalized(),
            "recovery_guidance": self.recovery_guidance,
            "events": list(self.events),
        }


def recover_orphan_continuation(call_id: str, known_call_ids: set[str]) -> JSON:
    tx = ToolTurnTransaction(call_id=call_id, tool_name="unknown")
    if call_id in known_call_ids:
        return tx.recover("matched canonical call_id").to_record()
    return tx.fail_closed("restart or escalate: unknown function-call continuation").to_record()


def transaction_for_tool_result(*, call_id: str, tool_name: str, result: Any) -> JSON:
    """Terminalize a bridge-owned tool turn from its ToolResult-like output."""
    tx = ToolTurnTransaction(call_id=call_id, tool_name=tool_name)
    if result is None:
        return tx.fail_closed("tool returned no result; retry through runtime recovery or escalate").to_record()

    complete = bool(getattr(result, "complete", True))
    exit_code = getattr(result, "exit_code", None)
    output_ref = str(getattr(result, "sha256", "") or "")
    if not output_ref:
        output_ref = f"exit_code:{exit_code}"

    if complete:
        record = tx.resolve(output_ref).to_record()
        record["exit_code"] = exit_code
        return record

    stderr = str(getattr(result, "stderr", "") or "").strip()
    guidance = "tool result incomplete; retry with narrower scope or escalate"
    if stderr:
        guidance = f"{guidance}: {stderr[:160]}"
    record = tx.fail_closed(guidance).to_record()
    record["exit_code"] = exit_code
    return record
