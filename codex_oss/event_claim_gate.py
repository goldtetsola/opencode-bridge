"""Pure claim predicates over RunRecord dictionaries."""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]


def evaluate_event_claim_gate(run_record: JSON, *, target: str) -> JSON:
    desktop = run_record.get("desktop_observation") if isinstance(run_record.get("desktop_observation"), dict) else {}
    tool_calls = run_record.get("tool_calls") if isinstance(run_record.get("tool_calls"), dict) else {}
    implementation = run_record.get("implementation") if isinstance(run_record.get("implementation"), dict) else {}
    route = run_record.get("route_authority") if isinstance(run_record.get("route_authority"), dict) else {}
    task = run_record.get("task_spec") if isinstance(run_record.get("task_spec"), dict) else {}
    reasons: list[str] = []
    allowed: list[str] = []
    disallowed: list[str] = []
    basis_event_ids: list[str] = []
    native_route = str(route.get("route_class") or task.get("mode") or "").startswith(("managed", "bounded", "guided"))
    raw_route = str(task.get("mode") or "").startswith("raw") or str(task.get("tier") or "").upper() == "RAW"

    if target in {"desktop-gold", "native-like"}:
        if raw_route or not native_route:
            reasons.append("desktop_route_authority_missing")
        if not desktop.get("ok"):
            reasons.append("desktop_progress_before_final_missing")
        else:
            basis_event_ids.extend([str(item) for item in desktop.get("basis_event_ids", [])])
            allowed.append("Mission runtime produced Desktop-observed pre-final progress before final.")
        if reasons:
            disallowed.append("MissionV1 OSS subagent produced certified Desktop Gold progress.")
    elif target == "tool-loop":
        if not desktop.get("ok"):
            reasons.append("desktop_progress_before_final_missing")
        if not tool_calls.get("all_resolved"):
            reasons.append("tool_loop_resolution_missing")
        if raw_route:
            reasons.append("raw_direct_route_research_only")
        if not reasons:
            allowed.append("Tool-loop calls were adopted or recovered with Desktop witness evidence.")
            basis_event_ids.extend([str(item) for item in tool_calls.get("basis_event_ids", [])])
        else:
            disallowed.append("Arbitrary OSS native tool-loop parity.")
    elif target == "implementation":
        if not desktop.get("ok"):
            reasons.append("desktop_progress_before_final_missing")
        if not implementation.get("ok"):
            reasons.append("implementation_runtime_apply_or_verification_missing")
        if raw_route:
            reasons.append("raw_direct_route_research_only")
        if not reasons:
            allowed.append("Runtime-owned implementation apply and verification were Desktop-observed.")
        else:
            disallowed.append("All OSS implementation agents are production-ready.")
    else:
        reasons.append(f"unsupported_event_claim_target:{target}")

    ok = not reasons
    return {
        "schema_version": "event_claim_gate.v1",
        "target": target,
        "ok": ok,
        "status": "CERTIFIED" if ok else "UNCONFIRMED",
        "allowed_claims": allowed,
        "disallowed_claims": disallowed,
        "basis_event_ids": basis_event_ids,
        "missing_evidence": reasons,
    }
