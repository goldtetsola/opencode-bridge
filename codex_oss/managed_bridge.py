"""Bridge adapter for A2/A3 managed investigation missions.

This module keeps the HTTP handler out of the mission runtime. It owns only the
translation from a Responses request body to a validated runtime report.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict

from codex_oss.decision_trace import append_decision, write_decision_trace
from codex_oss.fast_path import run_deterministic_fast_path
from codex_oss.implementation import run_implementation_mission
from codex_oss.ledger import EvidenceLedger
from codex_oss.mission import InvalidHandoffError, _build_mission, extract_mission_v1_block
from codex_oss.model_registry import AdmissionError, ModelRegistry
from codex_oss.mission_authority_artifacts import (
    write_adoption_or_recovery,
    write_canonical_evidence_artifacts,
    write_mission_authority_artifacts,
)
from codex_oss.claim_graph import refresh_claim_graph
from codex_oss.answer_graph import build_investigation_plan, refresh_answer_graph
from codex_oss.runtime.loop import run_loop
from codex_oss.runtime.policy import (
    acquire_mission_slot,
    build_deterministic_partial_report,
    release_mission_slot,
)
from codex_oss.visible_commentary import VisibleCommentarySink
from codex_oss.native_work_ux import native_final_report_text, native_work_contract_text
from codex_oss.native_runtime_contracts import (
    assess_sandbox_backend,
    build_native_authority_packet,
    build_gpt55_usage_displacement_record,
    compile_p0_phase_graph,
    create_worktree_plan,
    derive_resume_cursor,
    evaluate_review_economics,
    open_implementation_escrow,
    record_capability_route,
)

JSON = Dict[str, Any]

RUNTIME_MODEL_ALIASES = {
    "mission-a2-kimi": "ocg-kimi-k2.6",
    "mission-a3-kimi": "ocg-kimi-k2.6",
    "mission-a2-deepseek": "ocg-deepseek-v4-pro",
    "mission-a3-deepseek": "ocg-deepseek-v4-pro",
    "mission-a2-flash": "ocg-deepseek-v4-flash",
    "mission-a3-flash": "ocg-deepseek-v4-flash",
    "mission-a4-kimi": "ocg-kimi-k2.6",
    "mission-a4-deepseek": "ocg-deepseek-v4-pro",
    "mission-a4-flash": "ocg-deepseek-v4-flash",
    "mission-a5-kimi": "ocg-kimi-k2.6",
    "mission-a5-deepseek": "ocg-deepseek-v4-pro",
    "mission-a5-flash": "ocg-deepseek-v4-flash",
    "mission-a6-kimi": "ocg-kimi-k2.6",
    "mission-a6-deepseek": "ocg-deepseek-v4-pro",
    "mission-a6-flash": "ocg-deepseek-v4-flash",
}

RUNTIME_MODEL_FALLBACKS = {
    "mission-a2-kimi": ["ocg-deepseek-v4-flash"],
    "mission-a3-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a2-deepseek": ["ocg-deepseek-v4-flash", "ocg-kimi-k2.6"],
    "mission-a3-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a2-flash": ["ocg-kimi-k2.6"],
    "mission-a3-flash": ["ocg-kimi-k2.6"],
    "mission-a4-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a4-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a4-flash": ["ocg-kimi-k2.6"],
    "mission-a5-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a5-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a5-flash": ["ocg-kimi-k2.6"],
    "mission-a6-kimi": ["ocg-deepseek-v4-pro"],
    "mission-a6-deepseek": ["ocg-kimi-k2.6"],
    "mission-a6-flash": ["ocg-kimi-k2.6"],
}

RUNTIME_MODEL_REGISTRY = ModelRegistry.from_runtime_aliases(
    RUNTIME_MODEL_ALIASES,
    fallbacks=RUNTIME_MODEL_FALLBACKS,
)

RUNTIME_AUTONOMY_PROFILES = {
    "mission-a2-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a3-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a2-kimi": {"max_tool_budget": 10, "max_time_seconds": 90},
    "mission-a3-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a2-deepseek": {"max_tool_budget": 12, "max_time_seconds": 120},
    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a4-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a4-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a4-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a5-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a5-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a5-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a6-kimi": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a6-deepseek": {"max_tool_budget": 24, "max_time_seconds": 180},
    "mission-a6-flash": {"max_tool_budget": 10, "max_time_seconds": 120},
}


@dataclass
class ManagedMissionResult:
    handled: bool
    report_text: str = ""
    status: str = "PARTIAL"
    mission_id: str = ""


def extract_handoff_from_body(body: JSON) -> str:
    """Extract active handoff text from a Responses request body.

    User content is the authoritative handoff source. System/developer text may
    contain examples or installed repo guidance, so use it only when no user
    handoff marker is present.
    """
    user_text = _extract_text_for_roles(body, ("user",))
    if "OSS_HANDOFF_JSON" in user_text:
        return user_text
    return _extract_text_for_roles(body, ("system", "developer", "user"))


def _extract_text_for_roles(body: JSON, roles: tuple[str, ...]) -> str:
    text = ""
    for item in body.get("input", []):
        role = item.get("role", "")
        content = item.get("content", "")
        if role not in roles or not content:
            continue
        if isinstance(content, str):
            text += content + "\n"
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text += part.get("text", "") + "\n"
    return text


def _mission_v1_from_native_task_contract(text: str, raw_model_alias: str) -> JSON | None:
    """Build a concrete MissionV1 object from a native-style task contract.

    This keeps spawned Desktop prompts human-readable while preserving runtime
    MissionV1 authority internally.
    """
    fields = _task_contract_fields(text)
    goal = fields.get("GOAL") or fields.get("OBJECTIVE") or ""
    if not goal:
        return None
    tier = _tier_for_runtime_alias(raw_model_alias, fields.get("TASK TYPE", ""))
    read_only_paths = _split_contract_paths(fields.get("READ-ONLY PATHS", ""))
    owned_paths = _split_contract_paths(fields.get("OWNED PATHS", ""))
    if tier in {"A4", "A5", "A6"} and not owned_paths:
        return None
    allowed_paths = sorted({*read_only_paths, *owned_paths})
    if tier in {"A2", "A3"} and not allowed_paths:
        return None
    mission_id = fields.get("MISSION ID") or f"native_task_{int(time.time())}"
    write_allowed = tier in {"A5", "A6"}
    mode_map = {
        "A2": "guided_exploration",
        "A3": "managed_investigation",
        "A4": "patch_proposal",
        "A5": "bounded_implementation",
        "A6": "critical_implementation",
    }
    return {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": tier,
        "mode": mode_map[tier],
        "objective": goal.rstrip("."),
        "risk_tier": "low",
        "write_allowed": write_allowed,
        "allowed_roots": [],
        "allowed_paths": allowed_paths,
        "read_only_paths": read_only_paths or allowed_paths,
        "owned_paths": owned_paths,
        "forbidden_roots": [],
        "allowed_tool_classes": ["read", "search", "list", "safe_git"],
        "tool_budget": 8 if tier == "A2" else 14,
        "time_budget_seconds": 90 if tier == "A2" else 150,
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "objective_style": "open_investigation" if tier in {"A2", "A3"} else "implementation",
        "evidence_collection_mode": "agenda_guided",
        "exploration_policy": {
            "after_required_floor": "allow_model_exploration",
            "min_optional_actions_after_floor": 0,
            "max_optional_actions_after_floor": 1,
            "require_contradiction_search": False,
        },
        "answer_obligations": _answer_obligations_from_contract(fields),
        "must_inspect": read_only_paths[:1] or allowed_paths[:1],
        "report_schema": "managed_investigation_report.v1" if tier in {"A2", "A3"} else "implementation_report.v1",
        "required_outputs": [
            "files_inspected",
            "commands_run",
            "findings",
            "uncertainties",
            "confidence",
            "caveats",
            "escalation_recommendation",
        ],
    }


def _task_contract_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().upper()
            if key in {
                "ROLE",
                "GOAL",
                "OBJECTIVE",
                "MISSION ID",
                "TASK TYPE",
                "OWNED PATHS",
                "READ-ONLY PATHS",
                "DO NOT TOUCH",
                "VERIFICATION STEPS",
                "DELIVERABLE",
                "COMPLETION RULE",
                "ESCALATION RULE",
            }:
                fields[key] = value.strip()
                current = key
                continue
        if current:
            fields[current] = (fields[current] + " " + line).strip()
    return fields


def _split_contract_paths(value: str) -> list[str]:
    value = str(value or "").replace("Do not edit files.", "")
    if not value or value.lower().startswith("none"):
        return []
    out: list[str] = []
    for part in value.split(","):
        cleaned = part.strip().strip(".;")
        if cleaned and cleaned.lower() != "none":
            out.append(cleaned)
    return out


def _answer_obligations_from_contract(fields: dict[str, str]) -> list[JSON]:
    deliverable = fields.get("DELIVERABLE", "")
    obligations = []
    for idx, part in enumerate([p.strip().strip(".;") for p in deliverable.split(",") if p.strip()], start=1):
        obligations.append({"id": f"deliverable_{idx}", "question": f"Provide {part}."})
    return obligations or [
        {"id": "files", "question": "Which files were inspected?"},
        {"id": "evidence", "question": "What evidence supports the answer?"},
        {"id": "caveats", "question": "What uncertainty remains?"},
    ]


def _tier_for_runtime_alias(raw_model_alias: str, task_type: str) -> str:
    alias = str(raw_model_alias or "").lower()
    task = str(task_type or "").lower()
    for tier in ("a6", "a5", "a4", "a3", "a2"):
        if f"mission-{tier}" in alias:
            return tier.upper()
    if "implementation" in task:
        return "A5"
    if "patch" in task:
        return "A4"
    return "A3"


def _lane_for_mission_tier(tier: str) -> str:
    normalized = str(tier or "").upper()
    if normalized in {"A4", "A5", "A6"}:
        return "implementation"
    return "scout"


def should_handle_managed_mission_body(body: JSON, raw_model_alias: str) -> bool:
    """Return whether the runtime entrypoint will own this request.

    The HTTP bridge uses this as a preflight so it can start SSE before the
    synchronous mission loop begins, without stealing ordinary upstream turns.
    Keep this predicate in lockstep with run_managed_mission_from_body().
    """
    handoff = extract_handoff_from_body(body)
    runtime_alias = raw_model_alias in RUNTIME_MODEL_ALIASES
    try:
        mission_block = extract_mission_v1_block(handoff) if handoff else None
    except InvalidHandoffError:
        return True

    if not mission_block or "oss_agent_mission.v1" not in mission_block:
        return runtime_alias

    try:
        raw = json.loads(mission_block)
    except Exception:
        return True

    tier = str(raw.get("tier") or "")
    if tier in ("A2", "A3", "A4", "A5", "A6"):
        return True
    return runtime_alias


def run_managed_mission_from_body(
    body: JSON,
    raw_model_alias: str,
    log_fn: Callable[..., None],
    call_payload_fn: Callable[[JSON, float], JSON],
    map_model_fn: Callable[[str], str],
    request_deadline: float,
    commentary_callback: Callable[[JSON], None] | None = None,
) -> ManagedMissionResult:
    """Run an A2/A3 MissionV1 if present; return handled=False otherwise."""
    user_contract_text = _extract_text_for_roles(body, ("user",))
    runtime_alias = raw_model_alias in RUNTIME_MODEL_ALIASES
    if runtime_alias and "OSS_HANDOFF_JSON" not in user_contract_text:
        handoff = ""
    else:
        handoff = extract_handoff_from_body(body)
    try:
        mission_block = extract_mission_v1_block(handoff) if handoff else None
    except InvalidHandoffError as exc:
        log_fn("mission_entrypoint_invalid", error=str(exc))
        return ManagedMissionResult(
            handled=True,
            status="FAILED",
            report_text=native_final_report_text(_failure_report("unknown", f"invalid entrypoint: {exc}")),
        )

    synthesized_raw: JSON | None = None
    if (not mission_block or "oss_agent_mission.v1" not in mission_block) and runtime_alias:
        synthesized_raw = _mission_v1_from_native_task_contract(user_contract_text, raw_model_alias)

    if (not mission_block and not synthesized_raw) or (mission_block and "oss_agent_mission.v1" not in mission_block):
        if runtime_alias:
            return ManagedMissionResult(
                handled=True,
                status="FAILED",
                report_text=native_final_report_text(_failure_report(
                    "unknown",
                    "runtime-controlled OSS agent requires a MissionV1 handoff or native task contract",
                )),
            )
        return ManagedMissionResult(handled=False)

    mission_id = "unknown"
    try:
        raw = synthesized_raw if synthesized_raw is not None else json.loads(mission_block)
        mission = _build_mission(raw)
        mission_id = mission.mission_id
        try:
            admission = RUNTIME_MODEL_REGISTRY.admit(raw_model_alias, lane=_lane_for_mission_tier(mission.tier))
        except AdmissionError as exc:
            log_fn("mission_entrypoint_model_admission_failed", model_alias=raw_model_alias, error=str(exc))
            return ManagedMissionResult(
                handled=True,
                status="FAILED",
                report_text=native_final_report_text(_failure_report(mission_id, f"model admission failed: {exc}")),
            )
        mission.decision_trace = []
        mission.runtime_model_alias = raw_model_alias
        mission.runtime_model_admission = admission.to_record()
        mission.phase_graph = compile_p0_phase_graph()
        mission.resume_cursor = derive_resume_cursor(mission.phase_graph, "admit")
        sandbox_required = ["path", "process", "network"]
        sandbox_enforced = ["path"] if mission.tier in ("A2", "A3") else ["path", "process"]
        mission.sandbox_backend = assess_sandbox_backend(
            sandbox_required,
            sandbox_enforced,
            risk_tier=mission.risk_tier,
            residual_approval=mission.tier in ("A2", "A3"),
        )
        worktree_plan = create_worktree_plan(
            target_branch=str(getattr(mission, "mission_id", "mission")),
            worktree_path=os.path.join(".codex-oss", "worktrees", str(getattr(mission, "mission_id", "mission"))),
        )
        mission.implementation_escrow = open_implementation_escrow(
            owned_paths=list(getattr(mission, "owned_paths", []) or []),
            worktree=worktree_plan,
        )
        mission.native_authority_packet = build_native_authority_packet(
            mission_id=mission.mission_id,
            model_alias=raw_model_alias,
            lane=_lane_for_mission_tier(mission.tier),
            objective=str(getattr(mission, "objective", "") or ""),
            allowed_paths=list(getattr(mission, "allowed_paths", []) or []),
            owned_paths=list(getattr(mission, "owned_paths", []) or []),
            tier=str(getattr(mission, "tier", "") or ""),
            risk_tier=str(getattr(mission, "risk_tier", "") or ""),
        )
        if not bool(mission.native_authority_packet.get("ok")):
            log_fn(
                "mission_entrypoint_native_authority_packet_blocked",
                mission_id=mission.mission_id,
                checks=mission.native_authority_packet.get("checks", {}),
            )
            return ManagedMissionResult(
                handled=True,
                status="FAILED",
                mission_id=mission.mission_id,
                report_text=native_final_report_text(_failure_report(
                    mission.mission_id,
                    f"native_authority_packet insufficient: {mission.native_authority_packet}",
                )),
            )
        if bool(mission.sandbox_backend.get("promotion_blocked")) and mission.tier in ("A4", "A5", "A6"):
            log_fn(
                "mission_entrypoint_sandbox_backend_blocked",
                mission_id=mission.mission_id,
                sandbox_backend=mission.sandbox_backend,
            )
            return ManagedMissionResult(
                handled=True,
                status="FAILED",
                mission_id=mission.mission_id,
                report_text=native_final_report_text(_failure_report(
                    mission.mission_id,
                    f"sandbox_backend insufficient for implementation lane: {mission.sandbox_backend}",
                )),
            )
        mission.runtime_handoff_raw = "" if synthesized_raw is not None else handoff
        mission.runtime_task_contract_raw = user_contract_text if synthesized_raw is not None else ""
        append_decision(
            mission,
            decision_type="entry_validation",
            result="accepted",
            policy="EntryPointExtractionPolicyV1",
            reason="MissionV1 handoff parsed and validated",
            source_module="codex_oss/managed_bridge.py",
            input_payload={
                "tier": mission.tier,
                "mode": mission.mode,
                "runtime_alias": raw_model_alias,
                "model_admission": admission.to_record(),
                "phase_graph_hash": mission.phase_graph.get("graph_hash"),
                "sandbox_backend": mission.sandbox_backend,
                "implementation_escrow": mission.implementation_escrow,
                "native_authority_packet": mission.native_authority_packet,
            },
        )
        effective_deadline = _managed_runtime_deadline(mission, request_deadline)
        _apply_runtime_autonomy_profile(mission, raw_model_alias, effective_deadline)
        log_fn("mission_dispatch", tier=mission.tier, mode=mission.mode, mission_id=mission.mission_id)

        if mission.tier not in ("A2", "A3"):
            if mission.tier not in ("A4", "A5", "A6"):
                return ManagedMissionResult(handled=False)

        ledger = EvidenceLedger(mission_id=mission.mission_id, tool_budget_remaining=mission.tool_budget)

        def call_model(messages, tools, timeout, model_alias_override=None):
            selected_alias = str(model_alias_override or raw_model_alias or "ocg-kimi-k2.6")
            primary = RUNTIME_MODEL_ALIASES.get(selected_alias, selected_alias)
            candidates = [primary]
            for fallback in RUNTIME_MODEL_FALLBACKS.get(selected_alias, []):
                if fallback not in candidates:
                    candidates.append(fallback)
            last_exc: Exception | None = None
            deadline = time.monotonic() + max(1.0, float(timeout or 1.0))
            for idx, reasoning_model in enumerate(candidates):
                remaining = deadline - time.monotonic()
                if remaining <= 1.0:
                    break
                payload = {
                    "model": map_model_fn(reasoning_model),
                    "messages": messages,
                    "stream": False,
                    "tools": [],
                }
                if model_alias_override:
                    payload["_codex_force_non_stream"] = True
                if idx:
                    log_fn("mission_model_fallback_attempt", mission_id=mission.mission_id, model=payload["model"])
                try:
                    response = call_payload_fn(payload, remaining)
                    mission.last_reasoning_model = payload["model"]
                    history = list(getattr(mission, "reasoning_model_history", []) or [])
                    history.append(payload["model"])
                    mission.reasoning_model_history = history
                    mission.fallback_model_used = bool(getattr(mission, "fallback_model_used", False) or idx)
                    if idx:
                        log_fn("mission_model_fallback_ok", mission_id=mission.mission_id, model=payload["model"])
                    return response
                except Exception as exc:
                    last_exc = exc
                    if idx:
                        log_fn(
                            "mission_model_fallback_failed",
                            mission_id=mission.mission_id,
                            model=payload["model"],
                            error=str(exc),
                        )
                    else:
                        log_fn(
                            "mission_model_primary_failed",
                            mission_id=mission.mission_id,
                            model=payload["model"],
                            error=str(exc),
                        )
            if last_exc:
                raise last_exc
            raise RuntimeError("no runtime reasoning model candidates")

        if mission.tier in ("A4", "A5", "A6"):
            acquired, slot_reason = acquire_mission_slot(mission)
            if not acquired:
                append_decision(
                    mission,
                    decision_type="scheduler",
                    result="rejected",
                    policy="ConcurrencyPolicyV1",
                    reason=slot_reason,
                    source_module="codex_oss/runtime/policy.py",
                    input_payload={"tier": mission.tier, "apply_mode": getattr(mission, "apply_mode", "")},
                )
                report = build_deterministic_partial_report(mission, ledger, f"capacity_timeout:{slot_reason}")
                report["status"] = "FAILED"
                return ManagedMissionResult(
                    handled=True,
                    status="FAILED",
                    mission_id=mission.mission_id,
                    report_text=native_final_report_text(report),
                )
            append_decision(
                mission,
                decision_type="scheduler",
                result="accepted",
                policy="ConcurrencyPolicyV1",
                reason="mission acquired a scheduler slot",
                source_module="codex_oss/runtime/policy.py",
                input_payload={"tier": mission.tier, "apply_mode": getattr(mission, "apply_mode", "")},
            )
            try:
                artifact_dir = os.path.join(os.getcwd(), ".codex-oss", "missions", mission.mission_id)
                commentary = VisibleCommentarySink(
                    mission.mission_id,
                    artifact_dir,
                    mode=os.getenv("OSS_VISIBLE_TRACE", "summary"),
                    stream_callback=commentary_callback,
                    max_events=int(os.getenv("OSS_VISIBLE_TRACE_MAX_EVENTS", "40") or 40),
                    max_event_chars=int(os.getenv("OSS_VISIBLE_TRACE_MAX_EVENT_CHARS", "500") or 500),
                )
                result = run_implementation_mission(
                    mission=mission,
                    raw_model_alias=raw_model_alias,
                    handoff=handoff,
                    call_model=call_model,
                    timeout=effective_deadline,
                    project_root=os.getcwd(),
                    commentary=commentary,
                )
                commentary.close({
                    "status": str(result.get("status", "FAILED")),
                    "mission_id": mission.mission_id,
                    "confidence": "HIGH" if str(result.get("status", "")).upper() in {"VERIFIED", "VALIDATED"} else "MEDIUM",
                    "closure_source": "runtime_controlled_implementation",
                })
            finally:
                release_mission_slot(mission.mission_id)
            return ManagedMissionResult(
                handled=True,
                status=str(result.get("status", "FAILED")),
                mission_id=mission.mission_id,
                report_text=str(result.get("text", "")),
            )

        fast_path_result = run_deterministic_fast_path(mission, ledger)
        if fast_path_result is not None:
            append_decision(
                mission,
                decision_type="fast_path",
                result="used",
                policy="DeterministicFastPathV1",
                reason=f"Objective satisfied by deterministic {fast_path_result.get('fast_path', 'fast_path')}",
                source_module="codex_oss/fast_path.py",
                input_payload={"objective_type": fast_path_result.get("fast_path", "")},
            )
            report = fast_path_result.get("report", {})
            if isinstance(report, dict):
                artifact_dir = os.path.join(os.getcwd(), ".codex-oss", "missions", mission.mission_id)
                commentary = VisibleCommentarySink(
                    mission.mission_id,
                    artifact_dir,
                    mode=os.getenv("OSS_VISIBLE_TRACE", "summary"),
                    stream_callback=commentary_callback,
                    max_events=int(os.getenv("OSS_VISIBLE_TRACE_MAX_EVENTS", "40") or 40),
                    max_event_chars=int(os.getenv("OSS_VISIBLE_TRACE_MAX_EVENT_CHARS", "500") or 500),
                )
                commentary.emit(
                    "mission_started",
                    "Mission accepted",
                    native_work_contract_text(mission),
                    phase="PLAN",
                    source="runtime",
                    metadata={"ux_shape": "native_work_contract.v1"},
                )
                commentary.emit(
                    "deterministic_fast_path_used",
                    "Deterministic answer found",
                    "The runtime answered this objective from a deterministic fast path, so no exploratory model loop was needed.",
                    phase="REPORT",
                    source="runtime",
                )
                _attach_visible_commentary_paths(mission, report)
                commentary.emit(
                    "mission_completed",
                    "Mission completed",
                    f"Final status: {fast_path_result.get('status', 'PARTIAL')}. Final report and evidence artifacts have been persisted.",
                    phase="REPORT",
                    source="runtime",
                )
                commentary.close(report)
                _attach_commentary_delivery_summary(mission, report)
                final_status = _write_readonly_mission_artifacts(mission, ledger, report, str(fast_path_result.get("status", "PARTIAL")))
            else:
                final_status = str(fast_path_result.get("status", "PARTIAL"))
            return ManagedMissionResult(
                handled=True,
                status=final_status,
                mission_id=mission.mission_id,
                report_text=native_final_report_text(report),
            )

        artifact_dir = os.path.join(os.getcwd(), ".codex-oss", "missions", mission.mission_id)
        commentary = VisibleCommentarySink(
            mission.mission_id,
            artifact_dir,
            mode=os.getenv("OSS_VISIBLE_TRACE", "summary"),
            stream_callback=commentary_callback,
            max_events=int(os.getenv("OSS_VISIBLE_TRACE_MAX_EVENTS", "40") or 40),
            max_event_chars=int(os.getenv("OSS_VISIBLE_TRACE_MAX_EVENT_CHARS", "500") or 500),
        )
        result = run_loop(
            mission,
            ledger,
            call_model,
            [],
            None,
            mission.allowed_roots,
            mission.allowed_paths,
            request_deadline=effective_deadline,
            commentary=commentary,
        )
        report = result.get("report", {})
        if isinstance(report, dict):
            _attach_visible_commentary_paths(mission, report)
            commentary.close(report)
            _attach_commentary_delivery_summary(mission, report)
            final_status = _write_readonly_mission_artifacts(mission, ledger, report, str(result.get("status", "PARTIAL")))
        else:
            final_status = str(result.get("status", "PARTIAL"))
        report_text = native_final_report_text(report) if isinstance(report, dict) else str(report)
        return ManagedMissionResult(
            handled=True,
            status=final_status,
            mission_id=mission.mission_id,
            report_text=report_text,
        )
    except InvalidHandoffError as exc:
        log_fn("mission_invalid", error=str(exc), mission_id=mission_id)
        return ManagedMissionResult(
            handled=True,
            status="FAILED",
            mission_id=mission_id,
            report_text=native_final_report_text(_failure_report(mission_id, f"invalid OSS handoff schema: {exc}")),
        )
    except Exception as exc:
        log_fn("mission_crash", error=str(exc), mission_id=mission_id, trace=traceback.format_exc(limit=12))
        report = build_deterministic_partial_report(_MissionStub(mission_id), None, f"runtime_crash:{exc}")
        report["status"] = "FAILED"
        report["caveats"].append("Runtime returned a terminal report instead of falling through to legacy continuation.")
        return ManagedMissionResult(
            handled=True,
            status="FAILED",
            mission_id=mission_id,
            report_text=native_final_report_text(report),
        )


def _failure_report(mission_id: str, reason: str) -> JSON:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "FAILED",
        "confidence": "LOW",
        "report_source": "runtime_entrypoint",
        "runtime_model_alias": "",
        "explorer_model": "",
        "finalizer_model": None,
        "fallback_model_used": False,
        "files_inspected": [],
        "commands_run": [],
        "findings": [],
        "uncertainties": [],
        "caveats": [reason],
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": [],
    }


class _MissionStub:
    def __init__(self, mission_id: str):
        self.mission_id = mission_id


def _attach_visible_commentary_paths(mission: Any, report: dict) -> None:
    mission_id = str(getattr(mission, "mission_id", "mission_unknown") or "mission_unknown")
    report["visible_commentary_path"] = f".codex-oss/missions/{mission_id}/visible_commentary.jsonl"
    report["summary_path"] = f".codex-oss/missions/{mission_id}/summary.md"
    report["commentary_delivery_path"] = f".codex-oss/missions/{mission_id}/commentary_delivery.json"


def _attach_commentary_delivery_summary(mission: Any, report: dict) -> None:
    mission_id = str(getattr(mission, "mission_id", "mission_unknown") or "mission_unknown")
    delivery_path = os.path.join(os.getcwd(), ".codex-oss", "missions", mission_id, "commentary_delivery.json")
    try:
        with open(delivery_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return

    state_counts: dict[str, int] = {}
    events = payload.get("events") or {}
    values = events.values() if isinstance(events, dict) else events
    for event in values:
        if not isinstance(event, dict):
            continue
        for state, value in (event.get("states") or {}).items():
            if value is True:
                state_counts[state] = state_counts.get(state, 0) + 1

    delivery_summary = dict(payload.get("delivery_summary") or {})
    delivery_summary.update(
        {
            "consumer_observed": state_counts.get("consumer_observed", 0),
            "rendered_before_final": state_counts.get("rendered_before_final", 0),
            "stream_enqueued": state_counts.get("stream_enqueued", 0),
            "sse_emitted": state_counts.get("sse_emitted", 0),
        }
    )
    report["commentary_delivery_summary"] = delivery_summary


def _write_readonly_mission_artifacts(mission: Any, ledger: Any, report: dict, status: str) -> str:
    try:
        from codex_oss.runtime.autonomy import grade_trace
    except Exception:
        grade_trace = None

    root = os.getcwd()
    mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
    artifact_dir = os.path.join(root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    write_decision_trace(root, mission_id, mission)

    mission_payload = {
        "mission_id": mission_id,
        "tier": getattr(mission, "tier", ""),
        "mode": getattr(mission, "mode", ""),
        "apply_mode": getattr(mission, "apply_mode", ""),
        "objective": getattr(mission, "objective", ""),
        "objective_style": getattr(mission, "objective_style", ""),
        "risk_tier": getattr(mission, "risk_tier", ""),
        "allowed_roots": list(getattr(mission, "allowed_roots", []) or []),
        "allowed_paths": list(getattr(mission, "allowed_paths", []) or []),
        "phase_policy_enabled": bool(getattr(mission, "phase_policy_enabled", True)),
        "progress_policy_enabled": bool(getattr(mission, "progress_policy_enabled", True)),
        "objective_spec": dict(getattr(mission, "objective_spec", {}) or {}) if isinstance(getattr(mission, "objective_spec", None), dict) else None,
        "sufficiency_policy": dict(getattr(mission, "sufficiency_policy", {}) or {}),
        "answer_obligations": list(getattr(mission, "answer_obligations", []) or []),
        "must_inspect": list(getattr(mission, "must_inspect", []) or []),
        "evidence_collection_mode": str(getattr(mission, "evidence_collection_mode", "") or ""),
        "exploration_policy": dict(getattr(mission, "exploration_policy", {}) or {}),
        "source_requirements": list(getattr(mission, "source_requirements", []) or []),
        "closure_policy": dict(getattr(mission, "closure_policy", {}) or {}),
        "closer_model": str(getattr(mission, "closer_model", "") or ""),
        "fallback_closer_model": str(getattr(mission, "fallback_closer_model", "") or ""),
        "runtime_model_alias": str(getattr(mission, "runtime_model_alias", "") or ""),
        "runtime_model_admission": dict(getattr(mission, "runtime_model_admission", {}) or {}),
        "phase_graph": dict(getattr(mission, "phase_graph", {}) or {}),
        "resume_cursor": dict(getattr(mission, "resume_cursor", {}) or {}),
        "sandbox_backend": dict(getattr(mission, "sandbox_backend", {}) or {}),
        "implementation_escrow": dict(getattr(mission, "implementation_escrow", {}) or {}),
        "native_authority_packet": dict(getattr(mission, "native_authority_packet", {}) or {}),
    }
    ledger_payload = {
        "mission_id": mission_id,
        "tool_budget_remaining": int(getattr(ledger, "tool_budget_remaining", 0) or 0),
        "total_bytes_read": int(getattr(ledger, "total_bytes_read", 0) or 0),
        "redactions_applied": bool(getattr(ledger, "redactions_applied", False)),
        "duplicate_actions_blocked": int(getattr(ledger, "duplicate_actions_blocked", 0) or 0),
        "risk_flags": list(getattr(ledger, "risk_flags", []) or []),
        "files_inspected": [
            {
                "path": path,
                "complete": bool(entry.complete),
                "full_content_cached": bool(entry.full_content_cached),
                "chars_total": int(entry.chars_total),
                "chars_returned": int(entry.chars_returned),
                "sha256": entry.sha256,
                "tool": entry.tool,
                "extract_refs": [extract.get("id") for extract in (entry.extracts or []) if isinstance(extract, dict)],
            }
            for path, entry in (getattr(ledger, "files_inspected", {}) or {}).items()
        ],
        "commands_run": [
            {
                "tool": entry.tool,
                "args": dict(entry.args or {}),
                "exit_code": int(entry.exit_code),
                "matches_count": int(entry.matches_count),
                "stdout_sha256": entry.stdout_sha256,
                "extract_refs": [extract.get("id") for extract in (entry.extracts or []) if isinstance(extract, dict)],
            }
            for entry in (getattr(ledger, "commands_run", []) or [])
        ],
        "action_trace_count": len(getattr(ledger, "action_trace", []) or []),
    }
    claim_graph = refresh_claim_graph(mission, ledger, report=report, reason="artifact_write", persist=False)
    answer_graph = refresh_answer_graph(mission, ledger, claim_graph=claim_graph, report=report, reason="artifact_write", persist=False)
    status = _reconcile_readonly_runtime_entitlement(report, status, answer_graph)
    routing_decision = record_capability_route(
        store_path=os.path.join(root, ".codex-oss", "capability_scorecards.json"),
        model_alias=str(getattr(mission, "runtime_model_alias", "") or ""),
        lane=str((getattr(mission, "runtime_model_admission", {}) or {}).get("lane") or _lane_for_mission_tier(getattr(mission, "tier", ""))),
        status=status,
        review_burden=0.0,
        unsafe=bool(getattr(ledger, "risk_flags", []) or []),
    )
    mission.routing_decision = routing_decision
    report["routing_decision"] = routing_decision
    mission_payload["routing_decision"] = routing_decision
    usage_displacement = build_gpt55_usage_displacement_record(
        task_id=mission_id,
        mode=str(getattr(mission, "mode", "") or ""),
        model=str(getattr(mission, "runtime_model_alias", "") or ""),
        status=status,
        gpt55_direct_units=float(os.getenv("OSS_GPT55_DIRECT_BASELINE_UNITS", "2.0")),
        oss_units=float(os.getenv("OSS_RUN_USAGE_UNITS", "0.5")),
        gpt_review_units=float(os.getenv("OSS_GPT55_REVIEW_UNITS", "0.5")),
    )
    review_economics = evaluate_review_economics(
        status=status,
        review_units=float(os.getenv("OSS_GPT55_REVIEW_UNITS", "0.5")),
        direct_units=float(os.getenv("OSS_GPT55_DIRECT_BASELINE_UNITS", "2.0")),
        repair_units=0.0,
    )
    mission_payload["usage_displacement"] = usage_displacement
    mission_payload["review_economics"] = review_economics
    report["usage_displacement"] = usage_displacement
    report["review_economics"] = review_economics
    report["native_authority_packet"] = dict(getattr(mission, "native_authority_packet", {}) or {})
    coverage_graph = dict(answer_graph.get("coverage_graph", {}) or getattr(ledger, "coverage_graph", {}) or {})
    evidence_agenda = dict(answer_graph.get("evidence_agenda", {}) or getattr(ledger, "evidence_agenda", {}) or {})
    investigation_plan = build_investigation_plan(mission)
    trace_grading = grade_trace(ledger, report) if grade_trace else {
        "trace_grading_version": "1.0",
        "labels": ["unavailable"],
        "reasons": ["grade_trace import failed"],
        "counts": {},
    }
    summary_lines = [
        f"# Mission {mission_id}",
        "",
        f"- Status: {status}",
        f"- Objective: {getattr(mission, 'objective', '')}",
        f"- Objective Style: {getattr(mission, 'objective_style', '')}",
        f"- Trace Labels: {', '.join(trace_grading.get('labels', []))}",
        f"- Budget Remaining: {getattr(ledger, 'tool_budget_remaining', 0)}",
        f"- Claim Graph Main Claims: {claim_graph.get('sufficiency', {}).get('main_claim_count', 0)}",
        f"- Answer Obligations: {answer_graph.get('sufficiency', {}).get('required_answered', 0)}/{answer_graph.get('sufficiency', {}).get('required_total', 0)}",
        f"- Missing Required Sources: {', '.join(answer_graph.get('sufficiency', {}).get('missing_required_sources', []) or []) or '(none)'}",
        f"- Closure Source: {report.get('closure_source', report.get('report_source', ''))}",
        "",
        "## Findings",
    ]
    findings = report.get("findings", []) or []
    if findings:
        for finding in findings:
            if isinstance(finding, dict):
                summary_lines.append(f"- {finding.get('claim', '(missing claim)')}")
    else:
        summary_lines.append("- (none)")
    summary_lines.extend(["", "## Caveats"])
    caveats = report.get("caveats", []) or []
    if caveats:
        for caveat in caveats:
            summary_lines.append(f"- {caveat}")
    else:
        summary_lines.append("- (none)")
    summary_lines.extend(["", "## Trace Reasons"])
    for reason in trace_grading.get("reasons", []) or []:
        summary_lines.append(f"- {reason}")

    _write_json(os.path.join(artifact_dir, "mission.json"), mission_payload)
    write_mission_authority_artifacts(
        artifact_dir=artifact_dir,
        mission=mission,
        summary_payload=mission_payload,
        validation={
            "tier": getattr(mission, "tier", ""),
            "mode": getattr(mission, "mode", ""),
            "runtime_model_alias": getattr(mission, "runtime_model_alias", ""),
        },
    )
    _write_json(os.path.join(artifact_dir, "ledger.json"), ledger_payload)
    _write_json(os.path.join(artifact_dir, "report.json"), report)
    _write_json(os.path.join(artifact_dir, "claim_graph.json"), claim_graph)
    _write_json(os.path.join(artifact_dir, "investigation_plan.json"), investigation_plan)
    _write_json(os.path.join(artifact_dir, "answer_graph.json"), answer_graph)
    _write_json(os.path.join(artifact_dir, "coverage_graph.json"), coverage_graph)
    _write_json(os.path.join(artifact_dir, "evidence_agenda.json"), evidence_agenda)
    _write_json(os.path.join(artifact_dir, "trace_grading.json"), trace_grading)
    write_canonical_evidence_artifacts(
        artifact_dir=artifact_dir,
        mission_id=mission_id,
        task_class="read_only",
        answer_graph=answer_graph,
        coverage_graph=coverage_graph,
        claim_graph=claim_graph,
        ledger_payload=ledger_payload,
        report=report,
    )
    write_adoption_or_recovery(
        artifact_dir=artifact_dir,
        mission_id=mission_id,
        route_class="managed_read_only",
        pending_tool_calls_emitted=0,
        runtime_recovery_used=False,
    )
    _write_text(os.path.join(artifact_dir, "trace.jsonl"), _readonly_trace_jsonl(ledger, report))
    visible_trace_path = os.path.join(artifact_dir, "visible_commentary.jsonl")
    if not os.path.exists(visible_trace_path):
        _write_text(os.path.join(artifact_dir, "summary.md"), "\n".join(summary_lines) + "\n")
    return status


def _reconcile_readonly_runtime_entitlement(report: dict, status: str, answer_graph: dict[str, Any]) -> str:
    """Ensure MissionV1 read-only PARTIAL has runtime-owned insufficiency support.

    If answer-graph coverage says the runtime can complete, a model-authored
    PARTIAL cannot remain terminal merely because model finalization was weak.
    Runtime truth owns the final status.
    """
    proposed = str(status or report.get("status", "PARTIAL") or "PARTIAL").upper()
    suff = dict(answer_graph.get("sufficiency", {}) or {})
    entitlement = dict(suff.get("closure_entitlement", {}) or {})
    coverage = dict(answer_graph.get("coverage_graph", {}).get("coverage_status", {}) or {})
    can_complete = bool(
        entitlement.get("can_return_complete")
        or suff.get("can_close")
        or suff.get("can_complete")
        or suff.get("coverage_complete")
        or coverage.get("can_complete")
        or coverage.get("coverage_complete")
    )
    reasons = _readonly_insufficiency_reasons(suff)
    decision = {
        "schema_version": "runtime_entitlement_reconciliation.v1",
        "canonical_evidence_can_complete": can_complete,
        "input_status": proposed,
        "runtime_insufficiency_reasons": reasons,
        "decision": "unchanged",
    }
    if proposed == "PARTIAL" and can_complete and not reasons:
        proposed = "COMPLETE"
        report["status"] = "COMPLETE"
        decision["decision"] = "promoted_partial_to_complete"
        decision["reason"] = "answer_graph_can_complete_without_runtime_insufficiency"
        report.setdefault("caveats", []).append(
            "Runtime promoted model PARTIAL to COMPLETE because required evidence and answer coverage were complete."
        )
    elif proposed == "COMPLETE" and (not can_complete or reasons):
        proposed = "PARTIAL"
        report["status"] = "PARTIAL"
        decision["decision"] = "demoted_complete_to_partial"
        decision["reason"] = "answer_graph_cannot_complete_with_runtime_insufficiency"
        report.setdefault("caveats", []).append(
            "Runtime demoted COMPLETE to PARTIAL because required evidence or source coverage was incomplete."
        )
    elif proposed == "PARTIAL" and not reasons:
        decision["decision"] = "partial_supported_by_runtime_unknown_insufficiency"
        decision["runtime_insufficiency_reasons"] = ["runtime_can_complete_false_without_detailed_reason"]
    elif proposed == "PARTIAL":
        decision["decision"] = "partial_supported_by_runtime_insufficiency"
    decision["effective_status"] = proposed
    report["runtime_entitlement_reconciliation"] = decision
    return proposed


def _readonly_insufficiency_reasons(sufficiency: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in (
        "missing_required_sources",
        "contradicted_obligations",
        "blocked_obligations",
        "insufficient_evidence_obligations",
    ):
        values = sufficiency.get(key, [])
        if values:
            reasons.append(f"{key}:{','.join(str(item) for item in values)}")
    required_total = int(sufficiency.get("required_obligations", sufficiency.get("required_total", 0)) or 0)
    answered = int(sufficiency.get("answered_obligations", sufficiency.get("required_answered", 0)) or 0)
    if required_total and answered < required_total:
        reasons.append(f"required_obligations_unanswered:{answered}/{required_total}")
    recommended = str(sufficiency.get("recommended_status", "") or "")
    if recommended and recommended not in {"COMPLETE", ""}:
        reasons.append(f"answer_graph_recommended_status:{recommended}")
    return reasons


def _readonly_trace_jsonl(ledger: Any, report: dict) -> str:
    raw_entries = list(getattr(ledger, "action_trace", []) or [])
    entries = [_trace_entry_payload(entry) for entry in raw_entries]
    if not entries:
        entries = []
        for idx, command in enumerate(getattr(ledger, "commands_run", []) or [], start=1):
            entries.append({
                "turn": idx,
                "phase": "VERIFY",
                "action_type": "tool_call",
                "tool_name": getattr(command, "tool", ""),
                "raw_arguments": dict(getattr(command, "args", {}) or {}),
                "normalized_arguments": dict(getattr(command, "args", {}) or {}),
                "runtime_decision": "allowed",
                "decision_reason": "tool executed",
                "tool_result_summary": {
                    "exit_code": int(getattr(command, "exit_code", 0) or 0),
                    "matches_count": int(getattr(command, "matches_count", 0) or 0),
                },
                "why_not_report_yet": "Runtime gathered required evidence before finalizing the report.",
                "report_status": str(report.get("status", "") or ""),
                "report_source": str(report.get("report_source", "") or ""),
            })
    return "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)


def _trace_entry_payload(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "__dict__"):
        payload = {}
        for key, value in vars(entry).items():
            if isinstance(value, dict):
                payload[key] = dict(value)
            elif isinstance(value, list):
                payload[key] = list(value)
            else:
                payload[key] = value
        return payload
    return {"value": str(entry)}


def _write_json(path: str, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True))


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _managed_runtime_deadline(mission: Any, request_deadline: float) -> float:
    """Pick the runtime deadline for internally-managed A2/A3 missions.

    Runtime-backed agents do their whole plan-act-observe loop inside one
    provider request. The default bridge deadline is deliberately conservative
    for legacy continuation calls, but A3 missions may explicitly request a
    larger local runtime budget. Cap it so user-provided MissionV1 cannot turn
    into an unbounded request.
    """
    mission_time = float(getattr(mission, "time_budget_seconds", request_deadline) or request_deadline)
    max_deadline = float(os.getenv("RUNTIME_MISSION_MAX_DEADLINE_SECONDS", "180"))
    return min(max(float(request_deadline), mission_time), max_deadline)


def _apply_runtime_autonomy_profile(mission: Any, raw_model_alias: str, effective_deadline: float) -> None:
    """Apply model-specific adaptive autonomy caps after mission validation.

    MissionV1 remains the user/request contract. The profile is the runtime's
    local safety envelope for different reasoning engines: Flash gets short
    support budgets, Kimi gets normal A3 budgets, and DeepSeek gets more room
    for hypothesis-driven exploration when the mission deadline allows it.
    """
    profile = RUNTIME_AUTONOMY_PROFILES.get(raw_model_alias, {})
    if not profile:
        return

    requested_budget = int(getattr(mission, "tool_budget_requested", getattr(mission, "tool_budget", 0)) or 0)
    profile_budget = int(profile.get("max_tool_budget", getattr(mission, "tool_budget", 0)) or 0)
    reserve = float(os.getenv("DETERMINISTIC_PARTIAL_RESERVE_SECONDS", "5"))
    estimated_turn = float(os.getenv("ESTIMATED_A3_TURN_SECONDS", "8"))
    deadline_budget = max(1, int((max(0.0, effective_deadline - reserve)) // max(1.0, estimated_turn)))
    effective_budget = min(requested_budget or getattr(mission, "tool_budget", 1), profile_budget, deadline_budget)

    requested_time = int(getattr(mission, "time_budget_seconds", effective_deadline) or effective_deadline)
    profile_time = int(profile.get("max_time_seconds", requested_time) or requested_time)
    effective_time = int(min(requested_time, profile_time, effective_deadline))

    reasons = []
    if effective_budget < requested_budget:
        if profile_budget < requested_budget:
            reasons.append("model_profile")
        if deadline_budget < min(requested_budget, profile_budget):
            reasons.append("request_deadline")
    existing_reason = str(getattr(mission, "budget_reduction_reason", "") or "")
    if existing_reason and effective_budget < requested_budget:
        reasons.insert(0, existing_reason)

    mission.tool_budget = effective_budget
    mission.tool_budget_effective = effective_budget
    mission.time_budget_seconds = effective_time
    mission.budget_reduction_reason = "+".join(dict.fromkeys(r for r in reasons if r))
