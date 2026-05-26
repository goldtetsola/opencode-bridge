"""NativeExperienceContractV1 — end-to-end UX acceptance contract.

A spawned OSS subagent is "native-like" only if all required dimensions pass.
This contract is the product claim gate, not a collection of implemented components.
"""

from __future__ import annotations

import json
import os
from typing import Any

JSON = dict[str, Any]

# ═══════════════════════════════════════════════════════════════════════════
# NativeExperienceContractV1
# ═══════════════════════════════════════════════════════════════════════════


def build_native_experience_contract(
    *,
    mission_id: str,
    task_class: str,  # "read_floor" | "search_floor" | "bounded_implementation" | "implementation_recovery"
    runtime_truth: JSON | None = None,
    model_contribution: JSON | None = None,
    visible_progress: JSON | None = None,
    tool_loop: JSON | None = None,
    safety: JSON | None = None,
    artifacts: JSON | None = None,
) -> JSON:
    """Build a NativeExperienceContractV1 for evaluation."""

    return {
        "schema_version": "native_experience_contract.v1",
        "mission_id": mission_id,
        "task_class": task_class,
        "runtime_truth": runtime_truth or {
            "status_authority": "runtime",
            "evidence_authority": "runtime",
            "write_authority": "runtime",
            "final_status_source": "canonical_report",
        },
        "model_contribution": model_contribution or {
            "final_narration_required": True,
            "narrative_validated": True,
            "model_may_own_status": False,
        },
        "visible_progress": visible_progress or {
            "min_pre_final_commentary_events": 3,
            "required_event_classes": [
                "mission_or_action_start",
                "tool_or_evidence_progress",
                "closure_or_verification_progress",
            ],
            "must_be_observed_by_spawned_subagent_consumer": True,
        },
        "tool_loop": tool_loop or {
            "pending_after_final": False,
            "replay_loop_count": 0,
            "adoption_or_recovery_recorded": True,
        },
        "safety": safety or {
            "no_hidden_cot": True,
            "no_secret_output": True,
            "no_raw_file_dump": True,
            "no_unauthorized_write": True,
        },
        "artifacts": artifacts or {
            "visible_commentary_jsonl": True,
            "summary_md": True,
            "canonical_evidence": True,
            "adoption_probes": True,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# Contract evaluation — bronze/silver/gold/platinum
# ═══════════════════════════════════════════════════════════════════════════


def evaluate_native_experience(
    contract: JSON,
    *,
    artifacts_exist: dict[str, bool] | None = None,
    commentary_events_count: int = 0,
    commentary_event_classes: set[str] | None = None,
    commentary_observed: bool = False,
    commentary_rendered_before_final: bool = False,
    model_narrative_valid: bool = False,
    runtime_owns_status: bool = True,
    pending_after_final: bool = False,
    replay_loops: int = 0,
    writes_outside_scope: bool = False,
    adoption_recorded: bool = False,
    secrets_leaked: bool = False,
    raw_cot_exposed: bool = False,
) -> JSON:
    """Evaluate a NativeExperienceContractV1 against observed evidence.

    Returns bronze/silver/gold/platinum pass/fail with detailed dimensions.
    """

    dims: JSON = {}

    # ── Bronze: runtime-safe ──
    dims["bronze_runtime_safe"] = (
        runtime_owns_status
        and not writes_outside_scope
        and not secrets_leaked
        and not raw_cot_exposed
    )

    # ── Silver: natural report ──
    dims["silver_natural_report"] = (
        dims["bronze_runtime_safe"]
        and model_narrative_valid
        and not pending_after_final
    )

    # ── Gold: user-visible UX ──
    min_events = contract.get("visible_progress", {}).get("min_pre_final_commentary_events", 3)
    required_classes = set(contract.get("visible_progress", {}).get("required_event_classes", []))
    event_classes = commentary_event_classes or set()

    dims["gold_visible_ux"] = (
        dims["silver_natural_report"]
        and commentary_events_count >= min_events
        and required_classes.issubset(event_classes)
        and commentary_observed
        and commentary_rendered_before_final
    )

    # ── Platinum: tool-loop parity ──
    dims["platinum_tool_loop"] = (
        dims["silver_natural_report"]
        and replay_loops == 0
        and not pending_after_final
        and adoption_recorded
    )

    # Determine overall level
    if dims["platinum_tool_loop"]:
        level = "platinum"
    elif dims["gold_visible_ux"]:
        level = "gold"
    elif dims["silver_natural_report"]:
        level = "silver"
    elif dims["bronze_runtime_safe"]:
        level = "bronze"
    else:
        level = "none"

    # Identify failed dimensions
    failed = [name for name, ok in dims.items() if not ok]
    missing_evidence = _missing_evidence(dims, contract, artifacts_exist or {})

    return {
        "schema_version": "native_experience_evaluation.v1",
        "mission_id": contract.get("mission_id", ""),
        "task_class": contract.get("task_class", ""),
        "level": level,
        "bronze_pass": dims["bronze_runtime_safe"],
        "silver_pass": dims["silver_natural_report"],
        "gold_pass": dims["gold_visible_ux"],
        "platinum_pass": dims["platinum_tool_loop"],
        "dimensions": dims,
        "failed_dimensions": failed,
        "missing_evidence": missing_evidence,
    }


def _missing_evidence(
    dims: JSON,
    contract: JSON,
    artifacts_exist: dict[str, bool],
) -> list[str]:
    missing = []

    if not dims.get("bronze_runtime_safe"):
        missing.append("runtime_truth_not_verified")
    if not dims.get("silver_natural_report"):
        missing.append("model_narrative_not_valid_or_pending_after_final")
    if not dims.get("gold_visible_ux"):
        missing.append("commentary_not_observed_or_rendered_before_final")
    if not dims.get("platinum_tool_loop"):
        missing.append("tool_loop_parity_not_achieved")

    expected_artifacts = contract.get("artifacts", {})
    for name, expected in expected_artifacts.items():
        if expected and not artifacts_exist.get(name, False):
            missing.append(f"artifact_missing:{name}")

    return missing


# ═══════════════════════════════════════════════════════════════════════════
# CommentaryDeliveryV1 — delivery state machine
# ═══════════════════════════════════════════════════════════════════════════


COMMENTARY_DELIVERY_STATES = [
    "created",
    "sanitized",
    "stream_enqueued",
    "sse_emitted",
    "consumer_observed",
    "rendered_before_final",
    "reconciled_with_summary",
    "failed",
]

TERMINAL_DELIVERY_STATES = {"rendered_before_final", "reconciled_with_summary", "failed"}


class CommentaryDeliveryTracker:
    """Tracks delivery state for each commentary event through the full pipeline."""

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self.events: dict[str, JSON] = {}

    def create_event(
        self,
        event_type: str,
        message: str,
        *,
        event_id: str | None = None,
        phase: str = "",
    ) -> str:
        """Register a new commentary event. Returns event_id."""
        import time
        eid = event_id or f"evt_{len(self.events):04d}"
        self.events[eid] = {
            "schema_version": "commentary_delivery.v1",
            "event_id": eid,
            "mission_id": self.mission_id,
            "event_type": event_type,
            "message": message[:500],
            "phase": phase,
            "safe_for_user": True,
            "states": {s: False for s in COMMENTARY_DELIVERY_STATES},
            "failure_reason": None,
        }
        self.events[eid]["states"]["created"] = True
        self.events[eid]["states"]["created_at"] = int(time.time())
        return eid

    def transition(self, event_id: str, to_state: str, *, failure_reason: str | None = None) -> bool:
        """Transition an event to a new delivery state."""
        event = self.events.get(event_id)
        if not event:
            return False
        if to_state not in COMMENTARY_DELIVERY_STATES:
            return False
        event["states"][to_state] = True
        import time
        event["states"][f"{to_state}_at"] = int(time.time())
        if failure_reason:
            event["failure_reason"] = failure_reason
        return True

    def mark_sanitized(self, event_id: str) -> bool:
        return self.transition(event_id, "sanitized")

    def mark_stream_enqueued(self, event_id: str) -> bool:
        return self.transition(event_id, "stream_enqueued")

    def mark_sse_emitted(self, event_id: str) -> bool:
        return self.transition(event_id, "sse_emitted")

    def mark_observed(self, event_id: str) -> bool:
        return self.transition(event_id, "consumer_observed")

    def mark_rendered(self, event_id: str) -> bool:
        return self.transition(event_id, "rendered_before_final")

    def mark_reconciled(self, event_id: str) -> bool:
        return self.transition(event_id, "reconciled_with_summary")

    def mark_failed(self, event_id: str, reason: str) -> bool:
        return self.transition(event_id, "failed", failure_reason=reason)

    @property
    def all_emitted(self) -> bool:
        return all(
            e["states"].get("sse_emitted", False)
            for e in self.events.values()
        )

    @property
    def all_rendered(self) -> bool:
        return all(
            e["states"].get("rendered_before_final", False)
            for e in self.events.values()
        )

    @property
    def delivery_summary(self) -> JSON:
        total = len(self.events)
        if total == 0:
            return {"total": 0, "emitted": 0, "rendered": 0, "failed": 0}
        emitted = sum(1 for e in self.events.values() if e["states"].get("sse_emitted"))
        rendered = sum(1 for e in self.events.values() if e["states"].get("rendered_before_final"))
        failed = sum(1 for e in self.events.values() if e["states"].get("failed"))
        return {
            "total": total,
            "emitted": emitted,
            "rendered": rendered,
            "failed": failed,
            "delivery_rate": round(rendered / total, 4) if total > 0 else 0,
        }

    def to_json(self) -> JSON:
        return {
            "schema_version": "commentary_delivery_tracker.v1",
            "mission_id": self.mission_id,
            "delivery_summary": self.delivery_summary,
            "events": {eid: dict(event) for eid, event in sorted(self.events.items())},
        }

    def persist(self, mission_dir: str) -> None:
        """Write delivery tracker to mission artifacts."""
        os.makedirs(mission_dir, exist_ok=True)
        path = os.path.join(mission_dir, "commentary_delivery.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=2, sort_keys=True)


# ═══════════════════════════════════════════════════════════════════════════
# Commentary event class classification
# ═══════════════════════════════════════════════════════════════════════════


EVENT_CLASS_MAP: dict[str, str] = {
    "mission_started": "mission_or_action_start",
    "mission_completed": "closure_or_verification_progress",
    "mission_failed": "closure_or_verification_progress",
    "mission_partial": "closure_or_verification_progress",
    "mission_escalated": "closure_or_verification_progress",
    "read_floor_detected": "tool_or_evidence_progress",
    "server_side_read_started": "tool_or_evidence_progress",
    "server_side_read_completed": "tool_or_evidence_progress",
    "read_failures": "tool_or_evidence_progress",
    "evidence_from_history": "tool_or_evidence_progress",
    "grep_actions_recovered": "tool_or_evidence_progress",
    "ls_actions_recovered": "tool_or_evidence_progress",
    "model_action_requested": "tool_or_evidence_progress",
    "tool_action_started": "tool_or_evidence_progress",
    "tool_result_summary": "tool_or_evidence_progress",
    "model_finalizer_started": "closure_or_verification_progress",
    "model_finalizer_succeeded": "closure_or_verification_progress",
    "model_finalizer_failed": "closure_or_verification_progress",
    "deterministic_fallback_used": "closure_or_verification_progress",
    "runtime_redirect": "closure_or_verification_progress",
    "patch_intent_received": "tool_or_evidence_progress",
    "patch_validation_started": "tool_or_evidence_progress",
    "patch_validation_passed": "tool_or_evidence_progress",
    "patch_validation_failed": "tool_or_evidence_progress",
    "verification_passed": "closure_or_verification_progress",
    "verification_failed": "closure_or_verification_progress",
    "workspace_apply_started": "tool_or_evidence_progress",
    "isolated_apply_started": "tool_or_evidence_progress",
    "runtime_closure_started": "closure_or_verification_progress",
    "coverage_update": "tool_or_evidence_progress",
}


def classify_commentary_event(event_type: str) -> str:
    """Classify a commentary event into one of the required event classes."""
    return EVENT_CLASS_MAP.get(event_type, "tool_or_evidence_progress")


def extract_event_classes(events: list[JSON]) -> set[str]:
    """Extract the set of event classes present in a list of commentary events."""
    classes: set[str] = set()
    for event in events:
        if isinstance(event, dict):
            event_type = str(event.get("event_type", "") or "")
            classes.add(classify_commentary_event(event_type))
    return classes
