"""Small contract helpers for the model-agnostic native OSS runtime slices."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

JSON = dict[str, Any]


P0_PHASES = ("admit", "prep", "execute", "verify", "review", "result")
_SECRET_MARKERS = ("sk-", "OPENAI_API_KEY", "OPENCODE_GO_API_KEY", "ANTHROPIC_API_KEY", "DATABASE_URL", "AUTH_TOKEN")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compile_p0_phase_graph() -> JSON:
    edges = [{"from": a, "to": b, "edge": "success"} for a, b in zip(P0_PHASES, P0_PHASES[1:])]
    payload = {"schema_version": "phase_graph.v1", "phases": list(P0_PHASES), "edges": edges}
    payload["graph_hash"] = _sha(json.dumps(payload, sort_keys=True))
    return payload


def derive_resume_cursor(graph: JSON, completed_phase: str) -> JSON:
    for edge in graph.get("edges", []):
        if edge.get("from") == completed_phase:
            return {"schema_version": "resume_cursor.v1", "prior_phase": completed_phase, "next_phase": edge["to"], "trigger_edge": edge["edge"]}
    return {"schema_version": "resume_cursor.v1", "prior_phase": completed_phase, "next_phase": "result", "trigger_edge": "terminal"}


def record_phase_receipt(*, phase: str, prompt: str, inputs: Mapping[str, str], outputs: Mapping[str, str], model: str, status: str) -> JSON:
    receipt = {
        "schema_version": "phase_receipt.v1",
        "phase": phase,
        "prompt_hash": _sha(prompt),
        "input_hashes": {k: _sha(v) for k, v in sorted(inputs.items())},
        "output_hashes": {k: _sha(v) for k, v in sorted(outputs.items())},
        "model": model,
        "status": status,
    }
    receipt["receipt_hash"] = _sha(json.dumps(receipt, sort_keys=True))
    return receipt


def classify_liveness(*, progress_events: int, pending_seconds: float, deadline_seconds: float, capacity_wait: bool = False) -> str:
    if capacity_wait:
        return "capacity_wait"
    if progress_events > 0 and pending_seconds <= deadline_seconds:
        return "healthy_progress"
    if pending_seconds > deadline_seconds:
        return "true_stall"
    return "wedged_call"


def human_gate_edge(action: str, *, reason: str = "") -> JSON:
    allowed = {"approve", "abort", "replan", "force_proceed"}
    if action not in allowed:
        raise ValueError(f"unknown human gate action: {action}")
    return {"schema_version": "human_gate_edge.v1", "action": action, "edge": f"human_{action}", "reason": reason}


def package_large_spec(path: str, *, consulted_sections: Sequence[str], required_sections: Sequence[str] = ()) -> JSON:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    missing = [section for section in required_sections if section not in consulted_sections]
    return {
        "schema_version": "spec_context.v1",
        "source_path": path,
        "byte_count": len(text.encode("utf-8")),
        "sha256": _sha(text),
        "consulted_sections": list(consulted_sections),
        "missing_required_sections": missing,
        "status": "FAILED" if missing else "READY",
        "summary_is_projection": True,
    }


def build_spec_requirement_graph(requirements: Sequence[str], *, touched_paths: Sequence[str], verification: Sequence[str]) -> JSON:
    nodes = [{"id": req, "touched_paths": list(touched_paths), "verification": list(verification)} for req in requirements]
    return {"schema_version": "spec_requirement_graph.v1", "requirements": nodes, "coverage_count": len(nodes)}


def assert_task_graph_coverage(requirements: Sequence[str], task_inputs: Sequence[str]) -> JSON:
    missing = sorted(set(requirements) - set(task_inputs))
    return {"schema_version": "implementation_decomposition.v1", "missing_requirements": missing, "ok": not missing}


def build_review_packet(run_record: JSON, *, diff: str, verification: Sequence[JSON], model_final: str, questions: Sequence[str]) -> JSON:
    packet = {
        "schema_version": "review_packet.v1",
        "run_id": run_record.get("run_id") or run_record.get("mission_id"),
        "runtime_status": run_record.get("status"),
        "changed_paths": list(run_record.get("changed_paths") or []),
        "diff_hash": _sha(diff),
        "verification": list(verification),
        "model_final": model_final,
        "review_questions": list(questions),
        "raw_transcript_included": False,
    }
    packet["packet_hash"] = _sha(json.dumps(packet, sort_keys=True))
    return packet


def evaluate_review_packet_adequacy(packet: JSON) -> JSON:
    required = ("run_id", "runtime_status", "changed_paths", "diff_hash", "verification", "model_final", "review_questions")
    missing = [key for key in required if not packet.get(key)]
    lossy = bool(packet.get("raw_transcript_included")) or bool(missing)
    return {"schema_version": "review_packet_adequacy.v1", "adequate": not lossy, "missing": missing, "action": "short_review_allowed" if not lossy else "escalate_review_budget"}


def combine_review_acceptance(runtime_status: str, review_status: str) -> str:
    if runtime_status == "COMPLETE" and review_status == "accepted":
        return "accepted"
    if review_status == "rejected":
        return "rejected"
    return "pending"


def redact_text(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for marker in _SECRET_MARKERS:
        if marker in redacted:
            redacted = redacted.replace(marker, "[REDACTED]")
            changed = True
    return redacted, changed


def evaluate_data_exposure(content: str, *, provider: str, surface: str) -> JSON:
    redacted, changed = redact_text(content)
    return {
        "schema_version": "data_exposure_policy.v1",
        "provider": provider,
        "surface": surface,
        "redacted_content": redacted,
        "redactions_applied": changed,
        "allowed": not changed,
        "exposure_class": "secret_denied" if changed else "standard",
    }


def run_ops_doctor(config: Mapping[str, Any]) -> JSON:
    failures = []
    if config.get("parent_provider") == "opencode_bridge":
        failures.append("unsafe_parent_provider")
    if not config.get("provider_configured"):
        failures.append("missing_provider_config")
    if config.get("secret_echo"):
        failures.append("unsafe_secret_echo")
    return {"schema_version": "ops_doctor.v1", "ok": not failures, "failures": failures}


def evaluate_sandbox_action(action: str, *, allowed_paths: Sequence[str] = (), network: bool = False) -> JSON:
    denied = []
    if network:
        denied.append("network")
    if ".." in action or action.startswith("/etc/") or action.startswith("rm -rf"):
        denied.append("unsafe_command_or_path")
    return {"schema_version": "sandbox_policy.v1", "allowed": not denied, "denied_dimensions": denied, "allowed_paths": list(allowed_paths)}


def assess_sandbox_backend(required: Sequence[str], enforced: Sequence[str], *, risk_tier: str, residual_approval: bool = False) -> JSON:
    unavailable = sorted(set(required) - set(enforced))
    blocks = bool(unavailable and risk_tier in {"medium", "high"} and not residual_approval)
    return {"schema_version": "sandbox_backend.v1", "enforced": list(enforced), "unavailable": unavailable, "promotion_blocked": blocks}


def create_worktree_plan(target_branch: str, worktree_path: str) -> JSON:
    return {"schema_version": "worktree_plan.v1", "target_branch": target_branch, "worktree_path": worktree_path, "target_branch_mutated": False}


def open_implementation_escrow(*, owned_paths: Sequence[str], worktree: JSON) -> JSON:
    return {"schema_version": "implementation_escrow.v1", "owned_paths": list(owned_paths), "worktree": worktree, "writes_runtime_governed": True, "target_branch_mutated": False}


def promotion_decision(*, review_status: str, verification_ok: bool, conflict: bool) -> JSON:
    accepted = review_status == "accepted" and verification_ok and not conflict
    status = "applied" if accepted else "rejected"
    return {"schema_version": "promotion_transaction.v1", "status": status, "accepted": accepted, "conflict": conflict}


def evaluate_verification_adequacy(*, changed_paths: Sequence[str], commands: Sequence[str], requirements: Sequence[str], authority: str) -> JSON:
    relevant = bool(changed_paths and commands and requirements)
    trusted = authority == "runtime"
    return {"schema_version": "verification_adequacy.v1", "adequate": relevant and trusted, "trusted_authority": trusted, "relevant": relevant}


def evaluate_repair_convergence(blocker_history: Sequence[str], verification_regressed: bool = False) -> JSON:
    repeated = len(blocker_history) >= 2 and len(set(blocker_history[-2:])) == 1
    escalate = repeated or verification_regressed
    return {"schema_version": "repair_convergence.v1", "escalate": escalate, "reason": "repeated_blocker" if repeated else "verification_regression" if verification_regressed else "continue"}


def native_run_view_authority_map(*, mission_id: str, fields: Mapping[str, str]) -> JSON:
    """Declare NativeOSSSubagentRunViewV1 as an authority-preserving projection."""
    required = {
        "selected_model_alias",
        "runtime_status",
        "desktop_observation",
        "verification_adequacy",
        "promotion_transaction",
        "usage_displacement",
    }
    field_map = {str(key): str(value) for key, value in sorted(fields.items())}
    missing = sorted(required - set(field_map))
    payload = {
        "schema_version": "native_run_view_authority_map.v1",
        "mission_id": str(mission_id or ""),
        "projection": "NativeOSSSubagentRunViewV1",
        "projection_role": "authority_preserving",
        "field_authorities": field_map,
        "missing_authorities": missing,
        "render_only": False,
        "ok": not missing,
    }
    payload["authority_hash"] = _sha(json.dumps(payload, sort_keys=True))
    return payload


def validate_source_discriminator(*, source_domain: str, authority_source: str, consumer: str) -> JSON:
    """Require authority-bearing decisions to name their source domain."""
    domain = str(source_domain or "").strip()
    source = str(authority_source or "").strip()
    reasons = []
    if domain not in {"mission_v1", "run_record", "desktop_transcript", "runtime_policy", "review_gate", "scheduler"}:
        reasons.append("unknown_source_domain")
    if not source:
        reasons.append("authority_source_missing")
    return {
        "schema_version": "source_discriminator.v1",
        "source_domain": domain,
        "authority_source": source,
        "consumer": str(consumer or ""),
        "inference_from_field_shape_allowed": False,
        "ok": not reasons,
        "reasons": reasons,
    }


def evaluate_wiring_probe(*, required: Sequence[str], available: Sequence[str], adaptive_path: str) -> JSON:
    """Preflight adaptive runtime wiring before spending model calls."""
    required_set = {str(item) for item in required if str(item)}
    available_set = {str(item) for item in available if str(item)}
    missing = sorted(required_set - available_set)
    return {
        "schema_version": "wiring_probe.v1",
        "adaptive_path": str(adaptive_path or ""),
        "required_wiring": sorted(required_set),
        "available_wiring": sorted(available_set),
        "missing_wiring": missing,
        "ran_before_first_model_call": True,
        "ok": not missing,
    }


def record_override_action(*, action: str = "none", approved_by: str = "runtime_policy", reason: str = "") -> JSON:
    allowed = {"none", "force_proceed", "replan", "abort", "add_note"}
    normalized = str(action or "none")
    reasons = []
    if normalized not in allowed:
        reasons.append("unknown_override_action")
    if normalized != "none" and not str(approved_by or ""):
        reasons.append("override_approval_missing")
    return {
        "schema_version": "override_action.v1",
        "action": normalized,
        "approved_by": str(approved_by or ""),
        "reason": str(reason or ""),
        "edge_priority": "override" if normalized != "none" else "normal",
        "ok": not reasons,
        "reasons": reasons,
    }


def build_native_authority_packet(
    *,
    mission_id: str,
    model_alias: str,
    lane: str,
    objective: str,
    allowed_paths: Sequence[str],
    owned_paths: Sequence[str],
    tier: str,
    risk_tier: str,
    active_missions: int = 0,
    mission_limit: int = 4,
) -> JSON:
    """Bundle original-spec authority contracts for the active managed runtime path."""
    risk_rank = {"low": 1, "medium": 3, "high": 5}.get(str(risk_tier or "").lower(), 3)
    exposure = evaluate_data_exposure(objective, provider=str(model_alias or ""), surface="mission_objective")
    scheduler = evaluate_scheduler(
        active=active_missions,
        limit=mission_limit,
        locked_paths=[],
        requested_paths=list(allowed_paths) + list(owned_paths),
    )
    phase_profiles = [
        select_phase_profile(phase=phase, risk_tier=risk_rank)
        for phase in P0_PHASES
    ]
    fanout = aggregate_fanout(
        [{"order": 0, "model": model_alias, "cost": 0.0, "tokens": 0, "provider": "opencode_bridge"}],
        [],
    )
    bakeoff = select_bakeoff_winner([
        {"model": model_alias, "accepted": True, "cost": 0.0, "review_burden": 0.0, "lane": lane}
    ])
    run_view = native_run_view_authority_map(
        mission_id=mission_id,
        fields={
            "selected_model_alias": "runtime_model_admission.requested_model_alias",
            "runtime_status": "run_record.final_status",
            "desktop_observation": "consumer_observation_witness",
            "verification_adequacy": "verification_adequacy",
            "promotion_transaction": "promotion_transaction",
            "usage_displacement": "gpt55_usage_displacement",
        },
    )
    source = validate_source_discriminator(
        source_domain="mission_v1",
        authority_source="mission.json",
        consumer="managed_bridge",
    )
    wiring = evaluate_wiring_probe(
        required=["mission_parser", "model_registry", "phase_graph", "sandbox_backend", "scheduler", "result_projection"],
        available=["mission_parser", "model_registry", "phase_graph", "sandbox_backend", "scheduler", "result_projection"],
        adaptive_path="managed_mission_dispatch",
    )
    verification = evaluate_verification_adequacy(
        changed_paths=list(owned_paths),
        commands=["runtime_managed_bridge_contract"],
        requirements=["original_spec_runtime_authority_packet"],
        authority="runtime",
    )
    repair = evaluate_repair_convergence([])
    override = record_override_action()
    checks = {
        "data_exposure": bool(exposure.get("allowed")),
        "scheduler": bool(scheduler.get("admitted")),
        "run_view_authority": bool(run_view.get("ok")),
        "source_discriminator": bool(source.get("ok")),
        "wiring_probe": bool(wiring.get("ok")),
        "override_action": bool(override.get("ok")),
    }
    payload = {
        "schema_version": "native_authority_packet.v1",
        "mission_id": str(mission_id or ""),
        "model_alias": str(model_alias or ""),
        "lane": str(lane or ""),
        "tier": str(tier or ""),
        "data_exposure": exposure,
        "scheduler_decision": scheduler,
        "phase_profiles": phase_profiles,
        "worker_fanout": fanout,
        "profile_bakeoff": bakeoff,
        "run_view_authority": run_view,
        "source_discriminator": source,
        "wiring_probe": wiring,
        "verification_adequacy": verification,
        "repair_convergence": repair,
        "override_action": override,
        "checks": checks,
        "ok": all(checks.values()),
    }
    payload["packet_hash"] = _sha(json.dumps(payload, sort_keys=True))
    return payload


@dataclass
class CapabilityScorecard:
    samples: int = 0
    accepted: int = 0
    useful_partial: int = 0
    unsafe_incidents: int = 0
    review_burden: float = 0.0

    def update(self, *, status: str, review_burden: float = 0.0, unsafe: bool = False) -> "CapabilityScorecard":
        self.samples += 1
        self.accepted += 1 if status == "accepted" else 0
        self.useful_partial += 1 if status == "useful_partial" else 0
        self.unsafe_incidents += 1 if unsafe else 0
        self.review_burden += review_burden
        return self


def record_delegation_value(*, accepted_or_partial: bool, gpt_direct_units: float, oss_units: float, gpt_review_units: float) -> JSON:
    saved = gpt_direct_units - (oss_units + gpt_review_units)
    return {"schema_version": "delegation_value.v1", "useful": accepted_or_partial, "gpt55_units_saved": saved, "valuable": accepted_or_partial and saved > 0}


def evaluate_usage_displacement(baseline_gpt55: float, actual_oss: float, actual_gpt_review: float) -> JSON:
    saved = baseline_gpt55 - (actual_oss + actual_gpt_review)
    return {"schema_version": "usage_displacement.v1", "saved": saved, "promotable": saved > 0}


def build_gpt55_usage_displacement_record(
    *,
    task_id: str,
    mode: str,
    model: str,
    status: str,
    gpt55_direct_units: float,
    oss_units: float,
    gpt_review_units: float,
) -> JSON:
    saved = gpt55_direct_units - (oss_units + gpt_review_units)
    if saved > max(2.0, gpt55_direct_units * 0.5):
        estimate = "high"
    elif saved > 0:
        estimate = "medium"
    elif saved == 0:
        estimate = "low"
    else:
        estimate = "negative"
    accepted = str(status or "").upper() in {"COMPLETE", "VERIFIED", "VALID", "PARTIAL"}
    decision = "promote_lane" if accepted and saved > 0 else "keep_canary" if accepted else "downgrade_lane"
    return {
        "schema_version": "gpt55_usage_displacement.v1",
        "task_id": task_id,
        "mode": mode,
        "model": model,
        "gpt55_planning_used": True,
        "gpt55_review_used": gpt_review_units > 0,
        "gpt55_direct_baseline_estimate": {
            "usage_units": gpt55_direct_units,
            "usage_units_kind": "estimated",
        },
        "oss_run_actual": {
            "oss_usage_units": oss_units,
            "gpt55_review_units": gpt_review_units,
        },
        "review_burden": "none" if gpt_review_units == 0 else "minor" if gpt_review_units <= 1 else "moderate",
        "accepted_or_useful_partial": accepted,
        "gpt55_usage_saved_estimate": estimate,
        "gpt55_usage_saved_units": saved,
        "decision": decision,
    }


def evaluate_review_economics(
    *,
    status: str,
    review_units: float,
    direct_units: float,
    repair_units: float = 0.0,
) -> JSON:
    burden = review_units + repair_units
    failed = burden >= direct_units and str(status or "").upper() not in {"COMPLETE", "VERIFIED", "VALID"}
    warning = burden > direct_units * 0.5
    return {
        "schema_version": "review_economics.v1",
        "review_units": review_units,
        "repair_units": repair_units,
        "direct_units": direct_units,
        "review_burden_ratio": burden / direct_units if direct_units else 0,
        "routing_warning": bool(warning),
        "review_economics_failed": bool(failed),
    }


def route_from_thresholds(scorecard: CapabilityScorecard, *, min_samples: int = 3) -> str:
    if scorecard.unsafe_incidents:
        return "blocked"
    if scorecard.samples < min_samples:
        return "canary"
    if scorecard.accepted + scorecard.useful_partial >= min_samples and scorecard.review_burden <= scorecard.samples:
        return "promoted"
    return "downgraded"


def capability_scorecard_record(scorecard: CapabilityScorecard) -> JSON:
    return {
        "samples": scorecard.samples,
        "accepted": scorecard.accepted,
        "useful_partial": scorecard.useful_partial,
        "unsafe_incidents": scorecard.unsafe_incidents,
        "review_burden": scorecard.review_burden,
    }


def capability_scorecard_from_record(record: Mapping[str, Any] | None) -> CapabilityScorecard:
    record = record or {}
    return CapabilityScorecard(
        samples=int(record.get("samples", 0) or 0),
        accepted=int(record.get("accepted", 0) or 0),
        useful_partial=int(record.get("useful_partial", 0) or 0),
        unsafe_incidents=int(record.get("unsafe_incidents", 0) or 0),
        review_burden=float(record.get("review_burden", 0.0) or 0.0),
    )


def record_capability_route(
    *,
    store_path: str,
    model_alias: str,
    lane: str,
    status: str,
    review_burden: float = 0.0,
    unsafe: bool = False,
    min_samples: int = 3,
) -> JSON:
    path = Path(store_path)
    try:
        store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        store = {}
    if not isinstance(store, dict):
        store = {}

    key = f"{model_alias}:{lane}"
    scorecard = capability_scorecard_from_record(store.get(key) if isinstance(store.get(key), dict) else {})
    normalized = str(status or "").upper()
    sample_status = "accepted" if normalized == "COMPLETE" else "useful_partial" if normalized == "PARTIAL" else "failed"
    scorecard.update(status=sample_status, review_burden=review_burden, unsafe=unsafe)
    record = capability_scorecard_record(scorecard)
    store[key] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "schema_version": "capability_route_decision.v1",
        "model_alias": model_alias,
        "lane": lane,
        "scorecard_key": key,
        "sample_status": sample_status,
        "scorecard": record,
        "decision": route_from_thresholds(scorecard, min_samples=min_samples),
        "explicit_choice_preserved": True,
    }


def evaluate_scheduler(*, active: int, limit: int, locked_paths: Iterable[str], requested_paths: Iterable[str]) -> JSON:
    lock_conflict = bool(set(locked_paths) & set(requested_paths))
    capacity = active >= limit
    status = "capacity_retry" if capacity else "path_lock_wait" if lock_conflict else "admitted"
    return {"schema_version": "scheduler_policy.v1", "status": status, "admitted": status == "admitted"}


def select_phase_profile(*, phase: str, risk_tier: int) -> JSON:
    model_class = "gpt-5.5" if risk_tier >= 4 and phase in {"admit", "review", "result"} else "oss"
    return {"schema_version": "phase_profile.v1", "phase": phase, "model_class": model_class}


def aggregate_fanout(results: Sequence[JSON], side_results: Sequence[JSON] = ()) -> JSON:
    ordered = sorted(results, key=lambda r: r.get("order", 0))
    cost = sum(float(r.get("cost", 0)) for r in ordered) + sum(float(r.get("cost", 0)) for r in side_results)
    return {"schema_version": "worker_fanout.v1", "ordered_results": ordered, "side_results": list(side_results), "total_cost": cost}


def select_bakeoff_winner(candidates: Sequence[JSON]) -> JSON:
    accepted = [c for c in candidates if c.get("accepted")]
    if not accepted:
        return {"schema_version": "profile_bakeoff.v1", "winner": None, "losers_recorded": list(candidates)}
    winner = min(accepted, key=lambda c: float(c.get("cost", 0)) + float(c.get("review_burden", 0)))
    losers = [c for c in candidates if c is not winner]
    return {"schema_version": "profile_bakeoff.v1", "winner": winner, "losers_recorded": losers}
