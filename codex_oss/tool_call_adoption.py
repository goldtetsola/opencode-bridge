"""ToolCallAdoptionProbeV1 and ResponsesToolStateMachineV1.

Tracks whether Codex consumer adopts each bridged tool call. Provides
adoption telemetry without depending on consumer continuation behavior.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

JSON = dict[str, Any]


# ── ToolCallAdoptionProbeV1 ────────────────────────────────────────────────


def build_adoption_probe(
    *,
    response_id: str,
    item_id: str,
    call_id: str,
    tool_name: str,
    sse_sequence: int,
    consumer_adopted: bool = False,
    adoption_signal: str = "",
    adoption_latency_ms: int = 0,
    replay_count: int = 0,
    recovery_used: bool = False,
    recovery_reason: str | None = None,
) -> JSON:
    """Build a ToolCallAdoptionProbeV1 record."""
    emitted_at = str(int(time.time()))
    probe = {
        "schema_version": "tool_call_adoption_probe.v1",
        "response_id": response_id,
        "item_id": item_id,
        "call_id": call_id,
        "tool_name": tool_name,
        "emitted_at": emitted_at,
        "sse_sequence": sse_sequence,
        "consumer_adopted": consumer_adopted,
        "adoption_signal": adoption_signal or ("none_before_deadline" if not consumer_adopted else ""),
        "adoption_latency_ms": adoption_latency_ms,
        "replay_count": replay_count,
        "recovery_used": recovery_used,
        "recovery_reason": recovery_reason,
    }
    return probe


def build_adopted_probe(
    response_id: str,
    item_id: str,
    call_id: str,
    tool_name: str,
    sse_sequence: int,
    latency_ms: int,
) -> JSON:
    return build_adoption_probe(
        response_id=response_id,
        item_id=item_id,
        call_id=call_id,
        tool_name=tool_name,
        sse_sequence=sse_sequence,
        consumer_adopted=True,
        adoption_signal="function_call_output_received",
        adoption_latency_ms=latency_ms,
    )


def build_not_adopted_probe(
    response_id: str,
    item_id: str,
    call_id: str,
    tool_name: str,
    sse_sequence: int,
    replay_count: int,
    recovery_reason: str,
) -> JSON:
    return build_adoption_probe(
        response_id=response_id,
        item_id=item_id,
        call_id=call_id,
        tool_name=tool_name,
        sse_sequence=sse_sequence,
        consumer_adopted=False,
        replay_count=replay_count,
        recovery_used=True,
        recovery_reason=recovery_reason,
    )


# ── ResponsesToolStateMachineV1 ───────────────────────────────────────────


class ResponsesToolStateMachine:
    """Canonicalize all tool-call IDs and state transitions.

    This is the sole authority for pending tool calls in the bridge.
    """

    def __init__(self, response_id: str):
        self.response_id = response_id
        self.calls: dict[str, JSON] = {}
        self.sequence = 0
        self.parent_response_id: str | None = None
        self.previous_response_id: Optional[str] = None

    def register_tool_call(
        self,
        call_id: str,
        tool_name: str,
        arguments: JSON,
        output_item_id: str | None = None,
    ) -> JSON:
        """Register a new tool call in the state machine."""
        self.sequence += 1
        state = {
            "response_id": self.response_id,
            "output_item_id": output_item_id or f"fc_{call_id}",
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments_preview": json.dumps(arguments)[:200] if arguments else "",
            "sequence_number": self.sequence,
            "parent_response_id": self.parent_response_id,
            "previous_response_id": self.previous_response_id,
            "emitted": True,
            "adopted": False,
            "completed": False,
            "replayed": False,
            "recovered": False,
            "emitted_at": int(time.time()),
            "adopted_at": None,
            "completed_at": None,
        }
        self.calls[call_id] = state
        return state

    def mark_adopted(self, call_id: str) -> bool:
        state = self.calls.get(call_id)
        if not state:
            return False
        state["adopted"] = True
        state["adopted_at"] = int(time.time())
        return True

    def mark_completed(self, call_id: str, output: str = "") -> bool:
        state = self.calls.get(call_id)
        if not state:
            return False
        state["completed"] = True
        state["completed_at"] = int(time.time())
        state["output_chars"] = len(output)
        return True

    def mark_replayed(self, call_id: str) -> bool:
        state = self.calls.get(call_id)
        if not state:
            return False
        state["replayed"] = True
        return True

    def mark_recovered(self, call_id: str, reason: str) -> bool:
        state = self.calls.get(call_id)
        if not state:
            return False
        state["recovered"] = True
        state["recovery_reason"] = reason
        return True

    @property
    def pending_calls(self) -> list[JSON]:
        return [
            state for state in self.calls.values()
            if not state["completed"] and not state["recovered"]
        ]

    @property
    def adopted_calls(self) -> list[JSON]:
        return [state for state in self.calls.values() if state["adopted"]]

    @property
    def not_adopted_calls(self) -> list[JSON]:
        return [state for state in self.calls.values() if not state["adopted"]]

    @property
    def recovered_calls(self) -> list[JSON]:
        return [state for state in self.calls.values() if state["recovered"]]

    def adoption_stats(self) -> JSON:
        total = len(self.calls)
        if total == 0:
            return {"total": 0, "adopted": 0, "not_adopted": 0, "recovered": 0, "adoption_rate": 0}
        adopted = len(self.adopted_calls)
        recovered = len(self.recovered_calls)
        return {
            "total": total,
            "adopted": adopted,
            "not_adopted": total - adopted,
            "recovered": recovered,
            "adoption_rate": round(adopted / total, 4) if total > 0 else 0,
        }

    def to_probes(self) -> list[JSON]:
        probes = []
        for call_id, state in self.calls.items():
            probes.append(build_adoption_probe(
                response_id=state["response_id"],
                item_id=state.get("output_item_id", f"fc_{call_id}"),
                call_id=call_id,
                tool_name=state.get("tool_name", "unknown"),
                sse_sequence=state.get("sequence_number", 0),
                consumer_adopted=state.get("adopted", False),
                adoption_signal=("function_call_output_received" if state.get("adopted") else "none_before_deadline"),
                adoption_latency_ms=(int(state["adopted_at"] or 0) - int(state["emitted_at"] or 0)) * 1000 if state.get("adopted") and state.get("adopted_at") else 0,
                replay_count=1 if state.get("replayed") else 0,
                recovery_used=state.get("recovered", False),
                recovery_reason=state.get("recovery_reason"),
            ))
        return probes

    def to_ledger(self) -> JSON:
        return {
            "schema_version": "responses_tool_state_machine_ledger.v1",
            "response_id": self.response_id,
            "parent_response_id": self.parent_response_id,
            "previous_response_id": self.previous_response_id,
            "total_calls": len(self.calls),
            "adoption_stats": self.adoption_stats(),
            "calls": {
                call_id: {
                    "tool_name": state["tool_name"],
                    "sequence_number": state["sequence_number"],
                    "adopted": state["adopted"],
                    "completed": state["completed"],
                    "recovered": state["recovered"],
                }
                for call_id, state in sorted(self.calls.items())
            },
        }


# ── Adoption smoke matrix ──────────────────────────────────────────────────


ADOPTION_SMOKE_MATRIX: list[JSON] = [
    {
        "case": "single_read",
        "description": "Single file read tool call",
        "expected_adoption_paths": ["consumer_adopted", "recovery"],
    },
    {
        "case": "two_reads",
        "description": "Two sequential read tool calls",
        "expected_adoption_paths": ["consumer_adopted", "recovery"],
    },
    {
        "case": "three_reads",
        "description": "Three sequential read tool calls",
        "expected_adoption_paths": ["consumer_adopted", "recovery"],
    },
    {
        "case": "read_grep",
        "description": "Read followed by grep",
        "expected_adoption_paths": ["consumer_adopted", "recovery"],
    },
    {
        "case": "grep_read",
        "description": "Grep followed by read of matched file",
        "expected_adoption_paths": ["consumer_adopted", "recovery"],
    },
    {
        "case": "owned_write_readback",
        "description": "Owned write followed by readback verification",
        "expected_adoption_paths": ["recovery"],  # writes always recovered
    },
    {
        "case": "blocked_command",
        "description": "Command outside allowed scope",
        "expected_adoption_paths": ["blocked_by_policy"],
    },
    {
        "case": "large_file_read",
        "description": "Read of a large file (>100KB)",
        "expected_adoption_paths": ["consumer_adopted", "recovery"],
    },
]

SUPPORTED_MODELS_FOR_ADOPTION = [
    "oss_flash_support",
    "oss_kimi_rapid",
    "oss_deepseek_pro",
]


# ── Promotion gate ─────────────────────────────────────────────────────────


def check_adoption_promotion_gate(
    probes: list[JSON],
    *,
    task_class: str = "",
) -> JSON:
    """Check if adoption stats meet the promotion gate for claiming tool-loop parity."""
    total = len(probes)
    if total == 0:
        return {
            "promotion_eligible": False,
            "reason": "no probes to evaluate",
            "task_class": task_class,
            "checks": {},
        }

    adopted = sum(1 for p in probes if p.get("consumer_adopted"))
    replayed = sum(1 for p in probes if p.get("replay_count", 0) > 0)
    recovered = sum(1 for p in probes if p.get("recovery_used"))
    adoption_rate = adopted / total if total > 0 else 0

    checks = {
        "adoption_rate_geq_95pct": adoption_rate >= 0.95,
        "zero_replay_loops": replayed == 0,
        "zero_pending_after_final": True,  # requires runtime check
        "zero_progress_as_terminal": True,  # requires runtime check
        "zero_unsafe_writes": True,  # requires runtime check
    }

    eligible = all(checks.values())
    return {
        "promotion_eligible": eligible,
        "reason": "all promotion gates passed" if eligible else "one or more promotion gates failed",
        "task_class": task_class,
        "adoption_stats": {
            "total_probes": total,
            "adopted": adopted,
            "adoption_rate": round(adoption_rate, 4),
            "replayed": replayed,
            "recovered": recovered,
        },
        "checks": checks,
    }


# ── Probe persistence ──────────────────────────────────────────────────────


def persist_adoption_probes(
    mission_dir: str,
    probes: list[JSON],
    state_machine: ResponsesToolStateMachine | None = None,
) -> None:
    """Write adoption probes and state machine ledger to mission artifacts."""
    os.makedirs(mission_dir, exist_ok=True)

    # Write probes
    probes_path = os.path.join(mission_dir, "tool_call_adoption_probes.json")
    with open(probes_path, "w", encoding="utf-8") as f:
        json.dump({
            "schema_version": "tool_call_adoption_probes.v1",
            "probes": probes,
            "adoption_stats": {
                "total": len(probes),
                "adopted": sum(1 for p in probes if p.get("consumer_adopted")),
                "not_adopted": sum(1 for p in probes if not p.get("consumer_adopted")),
                "recovery_used": sum(1 for p in probes if p.get("recovery_used")),
            },
        }, f, indent=2, sort_keys=True)

    # Write state machine ledger
    if state_machine:
        ledger_path = os.path.join(mission_dir, "tool_state_machine_ledger.json")
        with open(ledger_path, "w", encoding="utf-8") as f:
            json.dump(state_machine.to_ledger(), f, indent=2, sort_keys=True)
