"""Native Runtime Certification Kernel.

Claim-bearing certification is event-log first: current mission claims reduce
MissionEventLog history into RunRecord state, then evaluate pure claim gates.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_oss.event_claim_gate import evaluate_event_claim_gate
from codex_oss.mission import InvalidHandoffError, extract_mission_v1_block, parse_mission_v1
from codex_oss.mission_event_log import MissionEventError, MissionEventLog, event_log_exists
from codex_oss.run_manifest import load_run_manifest
from codex_oss.run_record import build_run_record

JSON = dict[str, Any]

NATIVE_CERTIFICATION_SCHEMA_VERSION = "native_like_certification.v1"


def evaluate_handoff_authority(*, handoff_text: str = "", mission_json: JSON | None = None) -> JSON:
    """HandoffAuthorityGate: exactly one canonical MissionV1 authority source."""
    reasons: list[str] = []
    mission_id = ""
    active_blocks = 0
    ignored_examples = 0
    source = "mission_json_artifact" if isinstance(mission_json, dict) else "handoff_text"

    if isinstance(mission_json, dict):
        if mission_json.get("schema_version") == "oss_agent_mission.v1":
            active_blocks = 1
            mission_id = str(mission_json.get("mission_id", "") or "")
        else:
            reasons.append("mission_json_not_canonical_mission_v1")
    else:
        active_blocks, ignored_examples = _count_handoff_blocks(handoff_text)
        try:
            block = extract_mission_v1_block(handoff_text)
            if not block:
                reasons.append("mission_v1_handoff_missing")
            else:
                mission = parse_mission_v1(handoff_text)
                mission_id = str(getattr(mission, "mission_id", "") or "")
        except InvalidHandoffError as exc:
            reasons.append(str(exc))
        if active_blocks != 1:
            reasons.append(f"active_mission_v1_blocks={active_blocks}")

    ok = not reasons
    return {
        "schema_version": "handoff_authority_gate.v1",
        "ok": ok,
        "gate": "HandoffAuthorityGate",
        "source": source,
        "handoff_type": "MissionV1" if ok else "invalid_or_missing",
        "schema_checked": "oss_agent_mission.v1",
        "active_blocks": active_blocks,
        "ignored_examples": ignored_examples,
        "mission_id": mission_id,
        "reasons": reasons,
        "basis": "exactly_one_canonical_mission_v1" if ok else "; ".join(reasons),
    }


def classify_implementation_status(report: JSON | None) -> JSON:
    """Classify A5 reports into explicit authority-safe implementation statuses."""
    report = report if isinstance(report, dict) else {}
    status = str(report.get("status", "") or "").upper()
    verification = report.get("verification") if isinstance(report.get("verification"), list) else []
    rollback = report.get("rollback") if isinstance(report.get("rollback"), dict) else {}
    authority = report.get("implementation_authority") if isinstance(report.get("implementation_authority"), dict) else {}
    workspace_policy = report.get("workspace_policy") if isinstance(report.get("workspace_policy"), dict) else {}
    validation = report.get("validation") if isinstance(report.get("validation"), dict) else {}
    canonical_patch = report.get("canonical_patch_evidence") if isinstance(report.get("canonical_patch_evidence"), dict) else {}

    verification_passed = bool(verification) and all(isinstance(item, dict) and item.get("exit_code") == 0 for item in verification)
    rollback_available = bool(rollback.get("available")) and bool(rollback.get("artifact"))
    patch_built = bool(report.get("patch_artifact")) or bool(report.get("runtime_built_diff")) or bool(canonical_patch)
    main_workspace_mutated = bool(report.get("main_workspace_mutated"))
    explicit_workspace_policy = _explicit_workspace_apply_policy(report)
    reasons: list[str] = []

    if status == "VERIFIED" and verification_passed and rollback_available:
        if main_workspace_mutated and not explicit_workspace_policy:
            implementation_status = "failed_policy"
            reasons.append("workspace_mutation_without_explicit_apply_policy")
        elif str(authority.get("patch_authority", "") or "") != "bridge_runtime":
            implementation_status = "failed_policy"
            reasons.append("patch_authority_not_bridge_runtime")
        else:
            implementation_status = "verified"
    elif status in {"VALIDATED", "APPLIED_IN_ISOLATION", "APPLIED_IN_TEMP_PROJECT"} or (patch_built and not main_workspace_mutated):
        implementation_status = "patch_built_not_applied"
        if status == "VALIDATED":
            reasons.append("a4_or_non_mutating_validation_only")
    elif status in {"APPLIED_TO_WORKSPACE", "APPLIED_IN_ISOLATION"} and not verification_passed:
        implementation_status = "patch_applied_not_verified"
        reasons.append("verification_not_passed")
    elif status in {"FAILED", "INVALID", "ESCALATE", "VERIFICATION_FAILED"}:
        implementation_status = "failed_policy" if validation.get("status") in {"INVALID", "ESCALATE"} else "patch_applied_not_verified"
        reasons.append(f"terminal_status={status}")
    else:
        implementation_status = "not_applicable"
        reasons.append("no_implementation_report")

    return {
        "schema_version": "implementation_status.v1",
        "status": implementation_status,
        "terminal_status": status or "UNKNOWN",
        "verification_passed": verification_passed,
        "rollback_available": rollback_available,
        "runtime_built_diff": bool(report.get("runtime_built_diff", False)),
        "patch_authority": str(authority.get("patch_authority", "") or ""),
        "main_workspace_mutated": main_workspace_mutated,
        "explicit_apply_policy": explicit_workspace_policy,
        "reasons": reasons,
    }


def evaluate_patch_authority(*, report: JSON | None = None, mission_json: JSON | None = None) -> JSON:
    """PatchAuthorityGate: VERIFIED requires explicit apply, verification, rollback."""
    report = report if isinstance(report, dict) else {}
    mission_json = mission_json if isinstance(mission_json, dict) else {}
    status = classify_implementation_status(report)
    tier = str(mission_json.get("tier", "") or "").upper()
    is_implementation = tier in {"A4", "A5", "A6"} or bool(report.get("implementation_report_version"))
    reasons = list(status.get("reasons", []) or [])

    if not is_implementation:
        ok = True
        gate_status = "not_applicable"
        reasons = []
    else:
        gate_status = str(status.get("status", "") or "not_applicable")
        ok = gate_status == "verified" or (tier == "A4" and gate_status == "patch_built_not_applied")
        if str(report.get("status", "") or "").upper() == "VERIFIED":
            if not status.get("verification_passed"):
                reasons.append("verified_without_passing_verification")
            if not status.get("rollback_available"):
                reasons.append("verified_without_rollback_artifact")

    return {
        "schema_version": "patch_authority_gate.v1",
        "ok": ok,
        "gate": "PatchAuthorityGate",
        "implementation_status": gate_status,
        "implementation_status_detail": status,
        "tier": tier,
        "reasons": reasons,
        "basis": "patch_authority_satisfied" if ok else "; ".join(reasons),
    }


def certify_native_like_project(
    project_root: str,
    *,
    mission_id: str = "",
    run_id: str = "",
) -> JSON:
    """Build NativeLikeCertificationV1 and write JSON/Markdown artifacts."""
    project_root = os.path.abspath(project_root)
    run_id = run_id or mission_id
    if run_id and event_log_exists(project_root, run_id):
        certification = _event_backed_native_like_certification(project_root, run_id, target="native-like")
        _write_native_certification(project_root, certification)
        return certification
    certification = _claim_selector_required(project_root, "native-like")
    _write_native_certification(project_root, certification)
    return certification


def certify_desktop_gold_project(
    project_root: str,
    *,
    mission_id: str = "",
    run_id: str = "",
) -> JSON:
    project_root = os.path.abspath(project_root)
    run_id = run_id or mission_id
    if run_id and event_log_exists(project_root, run_id):
        certification = _event_backed_desktop_gold_certification(project_root, run_id)
        _write_desktop_gold_certification(project_root, certification)
        return certification
    certification = _claim_selector_required(project_root, "desktop-gold", schema_version="desktop_gold_certification.v1")
    _write_desktop_gold_certification(project_root, certification)
    return certification


def certify_oss_native_parity_project(
    project_root: str,
    *,
    mission_id: str = "",
    run_manifest_path: str = "",
) -> JSON:
    """Certify broad OSS native-like parity across covered proof lanes.

    This is intentionally aggregate and fail-closed. A Desktop Gold read mission
    does not imply tool-loop, implementation, recovery, or model coverage parity;
    each lane needs at least one mission with lane-specific authority artifacts.
    """
    project_root = os.path.abspath(project_root)
    if run_manifest_path:
        certification = _event_backed_aggregate_certification(project_root, run_manifest_path)
        _write_named_certification(project_root, "oss-native-parity", certification)
        return certification
    if mission_id and event_log_exists(project_root, mission_id):
        certification = _event_backed_single_parity_certification(project_root, mission_id)
        _write_named_certification(project_root, "oss-native-parity", certification)
        return certification
    certification = _claim_selector_required(project_root, "oss-native-parity", schema_version="oss_native_parity_certification.v1")
    _write_named_certification(project_root, "oss-native-parity", certification)
    return certification


def _event_backed_native_like_certification(project_root: str, run_id: str, *, target: str) -> JSON:
    record = _run_record_dict(project_root, run_id)
    gate = evaluate_event_claim_gate(record, target="desktop-gold")
    runtime_safe = bool(record.get("final_status")) or bool(record.get("task_spec"))
    desktop_native = runtime_safe and bool(gate.get("ok"))
    allowed_claims: list[str] = []
    disallowed_claims: list[str] = []
    if runtime_safe:
        allowed_claims.append("runtime-governed OSS subagent completed safely")
    else:
        disallowed_claims.append("runtime-governed OSS subagent completed safely")
    if desktop_native:
        allowed_claims.extend(gate.get("allowed_claims", []) or ["Desktop-native live-progress OSS subagent"])
    else:
        disallowed_claims.append("Desktop-native live-progress OSS subagent")
    return {
        "schema_version": NATIVE_CERTIFICATION_SCHEMA_VERSION,
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "mission_id": str(record.get("mission_id") or run_id),
        "run_id": run_id,
        "verdict": {
            "status": "CERTIFIED" if desktop_native else ("RUNTIME_ONLY" if runtime_safe else "UNCONFIRMED"),
            "runtime_safe": runtime_safe,
            "desktop_native": desktop_native,
        },
        "allowed_claims": allowed_claims,
        "disallowed_claims": disallowed_claims,
        "gates": {
            "event_claim_gate": gate,
            "run_record": {
                "schema_version": "run_record_gate.v1",
                "ok": True,
                "basis": "event_log_reduced_without_projection_authority",
                "source_event_hash": record.get("source_event_hash", ""),
            },
        },
        "run_record": record,
    }


def _event_backed_desktop_gold_certification(project_root: str, run_id: str) -> JSON:
    record = _run_record_dict(project_root, run_id)
    gate = evaluate_event_claim_gate(record, target="desktop-gold")
    certified = bool(gate.get("ok"))
    return {
        "schema_version": "desktop_gold_certification.v1",
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "desktop-gold",
        "mission_id": str(record.get("mission_id") or run_id),
        "run_id": run_id,
        "status": "PASS" if certified else "FAIL",
        "verdict": {"status": "CERTIFIED" if certified else "UNCONFIRMED", "desktop_gold": certified},
        "gates": {
            "event_claim_gate": gate,
            "run_record": {
                "schema_version": "run_record_gate.v1",
                "ok": True,
                "basis": "event_log_reduced_without_projection_authority",
                "source_event_hash": record.get("source_event_hash", ""),
            },
        },
        "allowed_claims": list(gate.get("allowed_claims", []) or []),
        "disallowed_claims": list(gate.get("disallowed_claims", []) or []),
        "run_record": record,
    }


def _event_backed_single_parity_certification(project_root: str, run_id: str) -> JSON:
    manifest = {
        "schema_version": "run_manifest.v1",
        "runs": [{"run_id": run_id, "mission_id": run_id, "lanes": ["desktop_progress", "tool_loop", "recovery", "implementation"]}],
    }
    return _event_backed_aggregate_from_manifest(project_root, manifest)


def _event_backed_aggregate_certification(project_root: str, run_manifest_path: str) -> JSON:
    return _event_backed_aggregate_from_manifest(project_root, load_run_manifest(run_manifest_path))


def _event_backed_aggregate_from_manifest(project_root: str, manifest: JSON) -> JSON:
    runs = manifest.get("runs") if isinstance(manifest.get("runs"), list) else []
    lane_basis: dict[str, list[JSON]] = {lane: [] for lane in ["desktop_progress", "tool_loop", "recovery", "implementation"]}
    excluded: list[JSON] = []
    reports: list[JSON] = []
    for item in runs:
        run_id = str(item.get("run_id") or "")
        lanes = [str(lane) for lane in item.get("lanes", []) if str(lane)]
        try:
            record = _run_record_dict(project_root, run_id)
        except (MissionEventError, ValueError) as exc:
            excluded.append({"run_id": run_id, "reason": str(exc)})
            continue
        reports.append(_event_mission_parity_report(record))
        for lane in lanes:
            if lane == "desktop_progress":
                gate = evaluate_event_claim_gate(record, target="desktop-gold")
            elif lane == "tool_loop":
                gate = evaluate_event_claim_gate(record, target="tool-loop")
            elif lane == "recovery":
                gate = evaluate_event_claim_gate(record, target="tool-loop")
                if gate.get("ok") and not any((call.get("recovery_event_id") or call.get("fail_closed")) for call in (record.get("tool_calls") or {}).get("items", [])):
                    gate = {**gate, "ok": False, "missing_evidence": ["recovery_event_missing"]}
            elif lane == "implementation":
                gate = evaluate_event_claim_gate(record, target="implementation")
            else:
                continue
            if gate.get("ok"):
                lane_basis.setdefault(lane, []).append({
                    "run_id": run_id,
                    "mission_id": record.get("mission_id", run_id),
                    "basis_event_ids": gate.get("basis_event_ids", []),
                })
    required_lanes = ["desktop_progress", "tool_loop", "recovery", "implementation"]
    gates = {
        lane: {
            "schema_version": "native_parity_gate.v1",
            "ok": bool(lane_basis.get(lane)),
            "gate": f"{lane}_event_gate",
            "basis": "event_history_lane_proven" if lane_basis.get(lane) else f"{lane}_missing",
            "runs": lane_basis.get(lane, []),
        }
        for lane in required_lanes
    }
    coverage_missing = (manifest.get("coverage") or {}).get("missing_lanes", []) if isinstance(manifest.get("coverage"), dict) else []
    for lane in coverage_missing:
        if lane in gates:
            gates[lane]["ok"] = False
            gates[lane]["basis"] = f"{lane}_missing_from_run_manifest"
    gates = {
        "desktop_progress_parity": gates["desktop_progress"],
        "tool_loop_parity": gates["tool_loop"],
        "recovery_parity": gates["recovery"],
        "implementation_parity": gates["implementation"],
        "model_mission_coverage": {
            "schema_version": "native_parity_gate.v1",
            "ok": True,
            "gate": "ModelMissionCoverageGate",
            "basis": "event_manifest_explicit_coverage",
            "models": sorted({model for report in reports for model in _listish((report.get("coverage") or {}).get("models"))}),
            "mission_classes": sorted({klass for report in reports for klass in _listish((report.get("coverage") or {}).get("mission_classes"))}),
        },
        "desktop_progress": gates["desktop_progress"],
        "tool_loop": gates["tool_loop"],
        "recovery": gates["recovery"],
        "implementation": gates["implementation"],
    }
    certified = all(bool(gates[name].get("ok")) for name in ("desktop_progress_parity", "tool_loop_parity", "recovery_parity", "implementation_parity", "model_mission_coverage")) and not excluded
    allowed_claims = []
    disallowed_claims = []
    if certified:
        allowed_claims.append("Supported runtime-controlled MissionV1 OSS subagents behave native-like across the covered parity matrix.")
    else:
        disallowed_claims.append("Supported runtime-controlled MissionV1 OSS subagents have full native parity across all lanes.")
    certification = {
        "schema_version": "oss_native_parity_certification.v1",
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "oss-native-parity",
        "mission_id": "",
        "status": "PASS" if certified else "PARTIAL",
        "verdict": {"status": "CERTIFIED" if certified else "PARTIAL", "oss_native_parity": certified},
        "gates": gates,
        "allowed_claims": allowed_claims,
        "disallowed_claims": disallowed_claims,
        "run_manifest": manifest,
        "excluded_runs": excluded,
        "mission_report_count": len(reports),
        "mission_reports_sample": reports[:10],
    }
    certification.update(_targeted_parity_summary(reports, str(runs[0].get("run_id") or "") if len(runs) == 1 else ""))
    return certification


def _event_mission_parity_report(record: JSON) -> JSON:
    desktop_gate = evaluate_event_claim_gate(record, target="desktop-gold")
    tool_gate = evaluate_event_claim_gate(record, target="tool-loop")
    implementation_gate = evaluate_event_claim_gate(record, target="implementation")
    route = record.get("route_authority") if isinstance(record.get("route_authority"), dict) else {}
    task = record.get("task_spec") if isinstance(record.get("task_spec"), dict) else {}
    implementation = record.get("implementation") if isinstance(record.get("implementation"), dict) else {}
    desktop = record.get("desktop_observation") if isinstance(record.get("desktop_observation"), dict) else {}
    tool_calls = record.get("tool_calls") if isinstance(record.get("tool_calls"), dict) else {}
    tool_items = tool_calls.get("items") if isinstance(tool_calls.get("items"), list) else []
    recovery_present = any(
        isinstance(call, dict) and (call.get("recovery_event_id") or call.get("fail_closed"))
        for call in tool_items
    )
    recovery_ok = bool(tool_gate.get("ok")) and recovery_present
    tool_status = "RECOVERED" if recovery_present else ("PASS" if tool_gate.get("ok") else "MISSING")
    mission_id = str(record.get("mission_id") or record.get("run_id") or "")
    desktop_basis = "desktop_gold_passed" if desktop_gate.get("ok") else "; ".join(desktop_gate.get("missing_evidence", []) or [])
    for method in desktop.get("diagnostic_capture_methods", []) or []:
        if "read_thread_export_missing" in str(method):
            desktop_basis = "read_thread_export_missing"
    return {
        "schema_version": "mission_native_parity_report.v1",
        "mission_id": mission_id,
        "desktop_gold": {"ok": bool(desktop_gate.get("ok")), "basis": desktop_basis},
        "spawned_transcript_authority": {"ok": bool(desktop.get("ok")), "basis": "raw_desktop_child_transcript" if desktop.get("ok") else "desktop_transcript_missing"},
        "tool_loop": {
            "schema_version": "tool_loop_parity_lane.v1",
            "ok": bool(tool_gate.get("ok")),
            "status": tool_status,
            "pending_tool_calls_emitted": len(tool_items),
            "probe_total": len(tool_items),
            "desktop_gold_required": True,
            "basis": "tool_calls_adopted_or_recovered_with_desktop_witness" if tool_gate.get("ok") else "tool_loop_parity_not_proven",
        },
        "implementation": {
            "schema_version": "implementation_parity_lane.v1",
            "ok": bool(implementation_gate.get("ok")),
            "tier": str(task.get("tier") or ""),
            "implementation_status": "verified" if implementation.get("verification_passed") else "not_applicable",
            "patch_authority_ok": bool(implementation.get("ok")),
            "desktop_gold_required": True,
            "basis": "implementation_patch_authority_with_desktop_witness" if implementation_gate.get("ok") else "implementation_parity_not_proven",
        },
        "recovery": {
            "schema_version": "recovery_parity_lane.v1",
            "ok": recovery_ok,
            "adoption_status": "RECOVERED" if recovery_present else "MISSING",
            "recovery_status": "RECOVERED" if recovery_present else "MISSING",
            "desktop_gold_required": True,
            "recovery_artifact_ok": recovery_present,
            "basis": "recovery_or_fail_closed_with_desktop_witness" if recovery_ok else "recovery_parity_not_proven",
        },
        "coverage": {
            "schema_version": "mission_parity_coverage.v1",
            "models": [str(route.get("model_alias") or "")] if route.get("model_alias") else [],
            "mission_classes": [str(task.get("tier") or task.get("mode") or "")] if task.get("tier") or task.get("mode") else [],
        },
        "run_record": record,
    }


def _run_record_dict(project_root: str, run_id: str) -> JSON:
    events = MissionEventLog.for_project(project_root, run_id).read_events(verify=True)
    return build_run_record(events).to_dict()


def _claim_selector_required(project_root: str, target: str, *, schema_version: str = NATIVE_CERTIFICATION_SCHEMA_VERSION) -> JSON:
    return {
        "schema_version": schema_version,
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "mission_id": "",
        "status": "FAIL",
        "verdict": {"status": "UNCONFIRMED"},
        "allowed_claims": [],
        "disallowed_claims": ["Claim-bearing certification requires an explicit run_id, mission_id, or run manifest."],
        "gates": {
            "claim_selector": {
                "schema_version": "claim_selector_gate.v1",
                "ok": False,
                "gate": "ClaimSelectorGate",
                "basis": "explicit_run_selector_required",
                "missing_evidence": ["run_id_or_mission_id_or_run_manifest_required"],
            }
        },
    }


def _runtime_only_allowed_claims(mission_reports: list[JSON]) -> list[str]:
    claims: list[str] = []
    for report in mission_reports:
        implementation = report.get("implementation") if isinstance(report.get("implementation"), dict) else {}
        tier = str(implementation.get("tier") or "").upper()
        if (
            tier in {"A4", "A5", "A6"}
            and bool(implementation.get("patch_authority_ok"))
            and str(implementation.get("implementation_status") or "").lower() == "verified"
        ):
            claims.append("A5 bounded implementation verified under runtime authority in isolated temporary project copy")
    return sorted(set(claims))


def _targeted_parity_summary(mission_reports: list[JSON], mission_id: str) -> JSON:
    if not mission_id or len(mission_reports) != 1:
        return {}
    report = mission_reports[0]
    implementation = report.get("implementation") if isinstance(report.get("implementation"), dict) else {}
    desktop_gold = report.get("desktop_gold") if isinstance(report.get("desktop_gold"), dict) else {}
    spawned_authority = report.get("spawned_transcript_authority") if isinstance(report.get("spawned_transcript_authority"), dict) else {}
    patch_ok = bool(implementation.get("patch_authority_ok"))
    desktop_ok = bool(desktop_gold.get("ok"))
    basis = "raw_desktop_child_transcript" if desktop_ok and spawned_authority.get("ok") else str(desktop_gold.get("basis") or "desktop_transcript_missing")
    out = {
        "runtime_execution": "PASS" if patch_ok else "FAIL",
        "patch_authority": "PASS" if patch_ok else "FAIL",
        "implementation_status": str(implementation.get("implementation_status") or "not_applicable").upper(),
        "desktop_observation": "PASS" if desktop_ok else "FAIL",
        "desktop_gold": desktop_ok,
        "basis": basis,
    }
    if not desktop_ok:
        out["next_required_artifact"] = "spawned_transcript_authority.json"
    if patch_ok and not desktop_ok:
        disallowed = list(out.get("disallowed_claims", []) or [])
        out["remaining_disallowed_claims"] = [
            "A5 implementation parity in Codex Desktop",
            "full native implementation-agent parity",
        ]
    return out


def _count_handoff_blocks(text: str) -> tuple[int, int]:
    import re

    blocks = re.findall(r"<OSS_HANDOFF_JSON>(.*?)</OSS_HANDOFF_JSON>", text or "", re.DOTALL)
    for match in re.finditer(r"(?<!<)OSS_HANDOFF_JSON\s*:", text or ""):
        tail = (text or "")[match.end():].lstrip()
        if tail.startswith("```"):
            lines = tail.splitlines()
            if lines:
                tail = "\n".join(lines[1:])
        try:
            _, end = json.JSONDecoder().raw_decode(tail)
        except json.JSONDecodeError:
            continue
        blocks.append(tail[:end])
    active = 0
    ignored = 0
    for block in blocks:
        try:
            raw = json.loads(block.strip())
        except json.JSONDecodeError:
            ignored += 1
            continue
        if isinstance(raw, dict) and raw.get("schema_version") == "oss_agent_mission.v1":
            from codex_oss.mission import is_placeholder_mission_v1_example
            if is_placeholder_mission_v1_example(raw):
                ignored += 1
                continue
            active += 1
        else:
            ignored += 1
    return active, ignored


def _explicit_workspace_apply_policy(report: JSON) -> bool:
    execution_mode = str(report.get("execution_mode", "") or "")
    policy = report.get("workspace_policy") if isinstance(report.get("workspace_policy"), dict) else {}
    if execution_mode in {"workspace", "workspace_explicit", "workspace_low_risk", "critical_workspace_certified"}:
        return bool(
            policy.get("allow_direct_workspace_apply")
            or policy.get("allow_critical_workspace_apply")
            or policy.get("certification_required")
        )
    return False


def _listish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _write_native_certification(project_root: str, report: JSON) -> None:
    root = Path(project_root) / ".codex-oss" / "certifications"
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "native-like.json"
    md_path = root / "native-like.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_render_native_certification_markdown(report), encoding="utf-8")


def _write_desktop_gold_certification(project_root: str, report: JSON) -> None:
    _write_named_certification(project_root, "desktop-gold", report)


def _write_named_certification(project_root: str, name: str, report: JSON) -> None:
    root = Path(project_root) / ".codex-oss" / "certifications"
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / f"{name}.json"
    md_path = root / f"{name}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_render_native_certification_markdown(report), encoding="utf-8")


def _render_native_certification_markdown(report: JSON) -> str:
    lines = [
        "# Native-Like Certification",
        "",
        f"- Mission: {report.get('mission_id', '')}",
        f"- Verdict: {(report.get('verdict') or {}).get('status', '')}",
        "",
        "## Allowed Claims",
        "",
    ]
    allowed = report.get("allowed_claims") if isinstance(report.get("allowed_claims"), list) else []
    if allowed:
        lines.extend(f"- {claim}" for claim in allowed)
    else:
        lines.append("- none")
    lines.extend(["", "## Disallowed Claims", ""])
    disallowed = report.get("disallowed_claims") if isinstance(report.get("disallowed_claims"), list) else []
    if disallowed:
        lines.extend(f"- {claim}" for claim in disallowed)
    else:
        lines.append("- none")
    lines.extend(["", "## Gates", ""])
    for name, gate in (report.get("gates") or {}).items():
        status = "PASS" if gate.get("ok") else "FAIL"
        lines.append(f"- {status} `{name}`: {gate.get('basis', '')}")
    lines.append("")
    return "\n".join(lines)
