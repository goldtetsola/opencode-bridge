"""Native Runtime Certification Kernel.

This module is deliberately artifact-first: it certifies only what runtime and
consumer evidence can prove, and it fails closed for Desktop-native claims.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_oss.desktop_native_verifier import verify_desktop_native_ux
from codex_oss.desktop_observation import capture_thread_observation
from codex_oss.mission import InvalidHandoffError, extract_mission_v1_block, parse_mission_v1
from codex_oss.mission_authority_artifacts import ensure_derived_desktop_gold_artifacts
from codex_oss.route_authority import route_allows_desktop_gold

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


def evaluate_runtime_truth(*, report: JSON | None = None, canonical_evidence: JSON | None = None) -> JSON:
    """RuntimeTruthGate: terminal status cannot exceed canonical evidence."""
    report = report if isinstance(report, dict) else {}
    canonical_evidence = canonical_evidence if isinstance(canonical_evidence, dict) else {}
    status = str(report.get("status", "") or "").upper()
    reasons: list[str] = []

    entitlement = canonical_evidence.get("status_entitlement") if isinstance(canonical_evidence.get("status_entitlement"), dict) else {}
    coverage = canonical_evidence.get("coverage_status") if isinstance(canonical_evidence.get("coverage_status"), dict) else {}
    missing_required = _listish(canonical_evidence.get("missing_required_sources"))
    blocked = _listish(coverage.get("blocked_obligations"))
    sufficiency = report.get("sufficiency") if isinstance(report.get("sufficiency"), dict) else {}
    reconciliation = report.get("runtime_entitlement_reconciliation") if isinstance(report.get("runtime_entitlement_reconciliation"), dict) else {}

    if status == "COMPLETE":
        if entitlement and not bool(entitlement.get("can_complete", False)):
            reasons.append("complete_without_canonical_status_entitlement")
        if missing_required:
            reasons.append("complete_with_missing_required_sources")
        if blocked:
            reasons.append("complete_with_blocked_obligations")
        if sufficiency and not bool(sufficiency.get("enough_evidence_to_report", True)):
            reasons.append("complete_with_insufficient_answer_graph")
        if reconciliation.get("decision") == "demoted_complete_to_partial":
            reasons.append("complete_after_runtime_reconciliation_demoted")

    ok = not reasons
    return {
        "schema_version": "runtime_truth_gate.v1",
        "ok": ok,
        "gate": "RuntimeTruthGate",
        "terminal_status": status or "UNKNOWN",
        "can_complete": bool(entitlement.get("can_complete", status == "COMPLETE" and not reasons)),
        "missing_required_sources": missing_required,
        "blocked_obligations": blocked,
        "reconciliation_decision": str(reconciliation.get("decision", "") or ""),
        "reasons": reasons,
        "basis": "terminal_status_matches_canonical_evidence" if ok else "; ".join(reasons),
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


def evaluate_desktop_observation(
    *,
    project_root: str,
    mission_id: str,
    transcript_path: str = "",
    route_authority: JSON | None = None,
) -> JSON:
    """DesktopObservationGate: Desktop Gold requires actual consumer witness."""
    reasons: list[str] = []
    if not transcript_path:
        reasons.append("desktop_transcript_missing")
        return {
            "schema_version": "desktop_observation_gate.v1",
            "ok": False,
            "gate": "DesktopObservationGate",
            "mission_id": mission_id,
            "transcript_path": transcript_path,
            "desktop_gold_pass": False,
            "runtime_progress_emission": _runtime_progress_emission(project_root, mission_id),
            "missing_evidence": ["desktop_native_unproven", *reasons],
            "basis": "; ".join(reasons),
        }
    transcript_payload = _read_json(Path(transcript_path))
    if transcript_payload:
        capture_thread_observation(
            project_root=project_root,
            mission_id=mission_id,
            transcript_path=transcript_path,
            thread_id=str(transcript_payload.get("thread_id", "") or ""),
            spawned_agent_id=str(transcript_payload.get("spawned_agent_id", "") or transcript_payload.get("agent_id", "") or ""),
        )

    report = verify_desktop_native_ux(
        transcript_path=transcript_path,
        mission_id=mission_id,
        project_root=project_root,
        route_authority=route_authority,
    )
    missing = list(report.get("missing_evidence", []) or [])
    return {
        "schema_version": "desktop_observation_gate.v1",
        "ok": bool(report.get("ok")) and bool(report.get("desktop_gold_pass")),
        "gate": "DesktopObservationGate",
        "mission_id": mission_id,
        "transcript_path": transcript_path,
        "desktop_gold_pass": bool(report.get("desktop_gold_pass")),
        "runtime_progress_emission": _runtime_progress_emission(project_root, mission_id),
        "consumer_observation_witness": report.get("consumer_observation_witness", {}),
        "desktop_verifier": report,
        "missing_evidence": missing,
        "basis": "desktop_consumer_observation_witness_passed" if report.get("ok") else "; ".join(missing),
    }


def certify_native_like_project(
    project_root: str,
    *,
    mission_id: str = "",
    handoff_path: str = "",
    transcript_path: str = "",
    route_authority_path: str = "",
) -> JSON:
    """Build NativeLikeCertificationV1 and write JSON/Markdown artifacts."""
    project_root = os.path.abspath(project_root)
    mission_id = mission_id or _latest_mission_id(project_root)
    mission_dir = Path(project_root) / ".codex-oss" / "missions" / mission_id if mission_id else None
    mission_json = _first_json(
        [
            mission_dir / "mission_canonical.json" if mission_dir else None,
            mission_dir / "mission.json" if mission_dir else None,
        ]
    )
    report = _read_json(mission_dir / "report.json") if mission_dir else {}
    canonical_evidence = _first_json(
        [
            mission_dir / "canonical_evidence_bundle.json" if mission_dir else None,
            mission_dir / "canonical_read_evidence.json" if mission_dir else None,
            mission_dir / "canonical_patch_evidence.json" if mission_dir else None,
        ]
    )
    handoff_text = _handoff_text_for_gate(mission_dir, handoff_path)
    route_authority = _read_json(Path(route_authority_path)) if route_authority_path else _read_json(mission_dir / "route_authority.json") if mission_dir else None

    handoff_gate = evaluate_handoff_authority(
        handoff_text=handoff_text,
        mission_json=mission_json if not handoff_text else None,
    )
    runtime_gate = evaluate_runtime_truth(report=report, canonical_evidence=canonical_evidence)
    patch_gate = evaluate_patch_authority(report=report, mission_json=mission_json)
    desktop_gate = evaluate_desktop_observation(
        project_root=project_root,
        mission_id=mission_id,
        transcript_path=transcript_path,
        route_authority=route_authority,
    )

    gates = {
        "handoff_authority": handoff_gate,
        "runtime_truth": runtime_gate,
        "patch_authority": patch_gate,
        "desktop_observation": desktop_gate,
    }
    runtime_safe = all(bool(gates[name].get("ok")) for name in ("handoff_authority", "runtime_truth", "patch_authority"))
    desktop_native = runtime_safe and bool(desktop_gate.get("ok"))
    allowed_claims: list[str] = []
    disallowed_claims: list[str] = []
    if runtime_safe:
        allowed_claims.append("runtime-governed OSS subagent completed safely")
    else:
        disallowed_claims.append("runtime-governed OSS subagent completed safely")
    if desktop_native:
        allowed_claims.append("Desktop-native live-progress OSS subagent")
    else:
        disallowed_claims.append("Desktop-native live-progress OSS subagent")

    certification = {
        "schema_version": NATIVE_CERTIFICATION_SCHEMA_VERSION,
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "native-like",
        "mission_id": mission_id,
        "verdict": {
            "status": "CERTIFIED" if desktop_native else ("RUNTIME_ONLY" if runtime_safe else "UNCONFIRMED"),
            "runtime_safe": runtime_safe,
            "desktop_native": desktop_native,
        },
        "allowed_claims": allowed_claims,
        "disallowed_claims": disallowed_claims,
        "gates": gates,
    }
    _write_native_certification(project_root, certification)
    return certification


def certify_desktop_gold_project(
    project_root: str,
    *,
    mission_id: str = "",
    transcript_path: str = "",
    route_authority_path: str = "",
) -> JSON:
    project_root = os.path.abspath(project_root)
    mission_id = mission_id or _latest_mission_id(project_root)
    mission_dir = Path(project_root) / ".codex-oss" / "missions" / mission_id if mission_id else None
    if mission_dir:
        ensure_derived_desktop_gold_artifacts(mission_dir)
    mission_json = _first_json(
        [
            mission_dir / "mission_canonical.json" if mission_dir else None,
            mission_dir / "mission.json" if mission_dir else None,
        ]
    )
    report = _read_json(mission_dir / "report.json") if mission_dir else {}
    canonical_evidence = _first_json(
        [
            mission_dir / "canonical_evidence_bundle.json" if mission_dir else None,
            mission_dir / "canonical_read_evidence.json" if mission_dir else None,
            mission_dir / "canonical_patch_evidence.json" if mission_dir else None,
        ]
    )
    handoff_gate = evaluate_handoff_authority(
        handoff_text=_handoff_text_for_gate(mission_dir, ""),
        mission_json=mission_json if not _has_handoff_text(mission_dir) else None,
    )
    runtime_gate = evaluate_runtime_truth(report=report, canonical_evidence=canonical_evidence)
    route_authority = _read_json(Path(route_authority_path)) if route_authority_path else _read_json(mission_dir / "route_authority.json") if mission_dir else {}
    route_ok = route_allows_desktop_gold(route_authority)
    route_gate = {
        "schema_version": "route_authority_gate.v1",
        "ok": route_ok,
        "gate": "RouteAuthorityGate",
        "basis": "mission_route_allows_desktop_gold" if route_ok else "desktop_route_authority_missing",
        "route_authority": route_authority or {},
    }
    transcript_path = transcript_path or str(mission_dir / "desktop_thread_transcript.json") if mission_dir else ""
    if transcript_path and mission_dir and Path(transcript_path).exists():
        capture_thread_observation(
            project_root=project_root,
            mission_id=mission_id,
            transcript_path=transcript_path,
            thread_id=str((_read_json(Path(transcript_path))).get("thread_id", "") or ""),
        )
    desktop_report = verify_desktop_native_ux(
        transcript_path=transcript_path,
        mission_id=mission_id,
        project_root=project_root,
        route_authority=route_authority,
    ) if transcript_path else {
        "ok": False,
        "consumer_observation_witness": {},
        "missing_evidence": ["desktop_transcript_missing"],
        "final_claim_gate": {},
    }
    consumer_gate = {
        "schema_version": "consumer_observation_gate.v1",
        "ok": bool((desktop_report.get("consumer_observation_witness") or {}).get("ok")),
        "gate": "ConsumerObservationGate",
        "basis": "consumer_observation_witness_passed" if bool((desktop_report.get("consumer_observation_witness") or {}).get("ok")) else "consumer_observation_witness_missing_or_failed",
        "witness": desktop_report.get("consumer_observation_witness", {}),
    }
    reconciliation = _read_json(mission_dir / "commentary_delivery_reconciliation.json") if mission_dir else {}
    reconciliation_gate = {
        "schema_version": "commentary_reconciliation_gate.v1",
        "ok": bool(reconciliation.get("pass") or reconciliation.get("ok")),
        "gate": "CommentaryReconciliationGate",
        "basis": "commentary_event_ids_reconciled" if bool(reconciliation.get("pass") or reconciliation.get("ok")) else "commentary_delivery_reconciliation_missing_or_failed",
        "reconciliation": reconciliation,
    }
    adoption = _read_json(mission_dir / "adoption_or_recovery.json") if mission_dir else {}
    adoption_status = str(adoption.get("status") or "").upper()
    adoption_gate = {
        "schema_version": "adoption_or_recovery_gate.v1",
        "ok": adoption_status in {"PASS", "RECOVERED", "NOT_APPLICABLE"},
        "gate": "AdoptionOrRecoveryGate",
        "status": adoption_status or "MISSING",
        "basis": f"adoption_or_recovery_{adoption_status.lower()}" if adoption_status in {"PASS", "RECOVERED", "NOT_APPLICABLE"} else "adoption_or_recovery_missing_or_failed",
        "adoption_or_recovery": adoption,
    }
    render_proof = (desktop_report.get("final_claim_gate") or {}).get("render_surface_proof") if isinstance(desktop_report.get("final_claim_gate"), dict) else {}
    render_gate = {
        "schema_version": "render_surface_proof_gate.v1",
        "ok": bool((render_proof or {}).get("ok")),
        "gate": "RenderSurfaceProofGate",
        "basis": str((render_proof or {}).get("basis") or "no_render_surface_proof"),
        "render_surface_proof": render_proof or {},
    }
    gates = {
        "route_authority": route_gate,
        "handoff_authority": handoff_gate,
        "runtime_evidence": runtime_gate,
        "consumer_observation": consumer_gate,
        "commentary_reconciliation": reconciliation_gate,
        "adoption_or_recovery": adoption_gate,
        "render_surface_proof": render_gate,
    }
    certified = all(bool(gate.get("ok")) for gate in gates.values())
    allowed_claims: list[str] = []
    disallowed_claims: list[str] = [
        "Arbitrary OSS native tool-loop parity.",
        "All OSS implementation agents are production-ready.",
    ]
    if certified:
        allowed_claims.extend([
            "MissionV1 OSS subagent produced Desktop-observed pre-final progress before final.",
            "Runtime-owned evidence and report reconciled with Desktop transcript.",
        ])
    else:
        disallowed_claims.insert(0, "MissionV1 OSS subagent produced certified Desktop Gold progress.")
    certification = {
        "schema_version": "desktop_gold_certification.v1",
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "desktop-gold",
        "mission_id": mission_id,
        "status": "PASS" if certified else "FAIL",
        "verdict": {
            "status": "CERTIFIED" if certified else "UNCONFIRMED",
            "desktop_gold": certified,
        },
        "gates": gates,
        "allowed_claims": allowed_claims,
        "disallowed_claims": disallowed_claims,
        "desktop_verifier": desktop_report,
    }
    _write_desktop_gold_certification(project_root, certification)
    return certification


def certify_oss_native_parity_project(
    project_root: str,
    *,
    mission_id: str = "",
) -> JSON:
    """Certify broad OSS native-like parity across covered proof lanes.

    This is intentionally aggregate and fail-closed. A Desktop Gold read mission
    does not imply tool-loop, implementation, recovery, or model coverage parity;
    each lane needs at least one mission with lane-specific authority artifacts.
    """
    project_root = os.path.abspath(project_root)
    mission_dirs = _mission_dirs(project_root, mission_id)
    mission_reports = [_mission_parity_report(project_root, mission_dir) for mission_dir in mission_dirs]

    desktop_missions = [item for item in mission_reports if (item.get("desktop_gold") or {}).get("ok")]
    tool_loop_missions = [item for item in mission_reports if (item.get("tool_loop") or {}).get("ok")]
    implementation_missions = [item for item in mission_reports if (item.get("implementation") or {}).get("ok")]
    recovery_missions = [item for item in mission_reports if (item.get("recovery") or {}).get("ok")]
    model_names = sorted({model for item in mission_reports for model in _listish((item.get("coverage") or {}).get("models"))})
    mission_classes = sorted({klass for item in mission_reports for klass in _listish((item.get("coverage") or {}).get("mission_classes"))})

    gates = {
        "desktop_progress_parity": _aggregate_gate(
            "DesktopProgressParityGate",
            desktop_missions,
            "desktop_progress_parity_proven",
            "desktop_progress_parity_missing",
        ),
        "tool_loop_parity": _aggregate_gate(
            "ToolLoopParityGate",
            tool_loop_missions,
            "tool_loop_adoption_or_recovery_proven",
            "tool_loop_parity_missing",
        ),
        "implementation_parity": _aggregate_gate(
            "ImplementationParityGate",
            implementation_missions,
            "runtime_patch_implementation_with_desktop_witness_proven",
            "implementation_parity_missing",
        ),
        "recovery_parity": _aggregate_gate(
            "RecoveryParityGate",
            recovery_missions,
            "recovery_or_fail_closed_proven",
            "recovery_parity_missing",
        ),
        "model_mission_coverage": {
            "schema_version": "native_parity_gate.v1",
            "ok": len(model_names) >= 2 and len(mission_classes) >= 2,
            "gate": "ModelMissionCoverageGate",
            "basis": "multi_model_multi_mission_coverage" if len(model_names) >= 2 and len(mission_classes) >= 2 else "model_mission_coverage_insufficient",
            "models": model_names,
            "mission_classes": mission_classes,
            "min_models": 2,
            "min_mission_classes": 2,
        },
    }

    allowed_claims: list[str] = []
    disallowed_claims: list[str] = []
    if gates["desktop_progress_parity"]["ok"]:
        allowed_claims.append("Covered MissionV1 OSS subagents have certified Desktop pre-final progress parity.")
    else:
        disallowed_claims.append("Desktop progress parity for covered MissionV1 OSS subagents.")
    if gates["tool_loop_parity"]["ok"]:
        allowed_claims.append("Covered MissionV1 OSS tool-loop calls are adopted or recovered with Desktop witness evidence.")
    else:
        disallowed_claims.append("Arbitrary OSS native tool-loop parity.")
    if gates["implementation_parity"]["ok"]:
        allowed_claims.append("Covered runtime-controlled OSS implementation lanes have native-like patch/verify/rollback behavior.")
    else:
        disallowed_claims.append("All OSS implementation agents are production-ready.")
    if gates["recovery_parity"]["ok"]:
        allowed_claims.append("Covered OSS recovery paths recover or fail closed under injected/recorded failure.")
    else:
        disallowed_claims.append("OSS recovery paths behave native-like across failures.")

    native_allowed = bool(allowed_claims)
    runtime_only_claims = _runtime_only_allowed_claims(mission_reports)
    for claim in runtime_only_claims:
        if claim not in allowed_claims:
            allowed_claims.append(claim)

    certified = all(bool(gate.get("ok")) for gate in gates.values())
    if certified:
        allowed_claims.append("Supported runtime-controlled MissionV1 OSS subagents behave native-like across the covered parity matrix.")
    else:
        disallowed_claims.append("Supported runtime-controlled MissionV1 OSS subagents have full native parity across all lanes.")

    certification = {
        "schema_version": "oss_native_parity_certification.v1",
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "oss-native-parity",
        "mission_id": mission_id,
        "status": "PASS" if certified else ("PARTIAL" if native_allowed else "FAIL"),
        "verdict": {
            "status": "CERTIFIED" if certified else ("PARTIAL" if native_allowed else "UNCONFIRMED"),
            "oss_native_parity": certified,
        },
        "gates": gates,
        "allowed_claims": allowed_claims,
        "disallowed_claims": disallowed_claims,
        "mission_report_count": len(mission_reports),
        "passing_mission_reports": [
            item for item in mission_reports
            if any(bool((item.get(key) or {}).get("ok")) for key in ("desktop_gold", "tool_loop", "implementation", "recovery"))
        ],
        "mission_reports_sample": mission_reports[:10],
    }
    certification.update(_targeted_parity_summary(mission_reports, mission_id))
    _write_named_certification(project_root, "oss-native-parity", certification)
    return certification


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


def _runtime_progress_emission(project_root: str, mission_id: str) -> JSON:
    path = Path(project_root) / ".codex-oss" / "missions" / mission_id / "commentary_delivery.json"
    payload = _read_json(path)
    events = payload.get("events") if isinstance(payload, dict) else {}
    if not isinstance(events, dict):
        events = {}
    emitted = 0
    for event in events.values():
        if isinstance(event, dict):
            states = event.get("states") if isinstance(event.get("states"), dict) else {}
            if states.get("sse_emitted") or states.get("stream_enqueued"):
                emitted += 1
    return {
        "schema_version": "runtime_progress_emission.v1",
        "path": str(path),
        "artifact_present": path.exists(),
        "emitted_count": emitted,
        "ok": emitted >= 1,
    }


def _read_json(path: Path | None) -> JSON:
    if path is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _handoff_text_for_gate(mission_dir: Path | None, handoff_path: str) -> str:
    candidates: list[Path] = []
    if handoff_path:
        candidates.append(Path(handoff_path))
    if mission_dir:
        candidates.extend([mission_dir / "mission_handoff_raw.txt", mission_dir / "handoff.md"])
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            return text
    return ""


def _has_handoff_text(mission_dir: Path | None) -> bool:
    return bool(_handoff_text_for_gate(mission_dir, ""))


def _mission_dirs(project_root: str, mission_id: str = "") -> list[Path]:
    root = Path(project_root) / ".codex-oss" / "missions"
    if mission_id:
        path = root / mission_id
        return [path] if path.exists() and path.is_dir() else []
    if not root.exists():
        return []
    return sorted([path for path in root.iterdir() if path.is_dir()], key=lambda path: path.name)


def _mission_parity_report(project_root: str, mission_dir: Path) -> JSON:
    mission_id = mission_dir.name
    ensure_derived_desktop_gold_artifacts(mission_dir)
    mission_json = _first_json([mission_dir / "mission_canonical.json", mission_dir / "mission.json"])
    report = _read_json(mission_dir / "report.json")
    route_authority = _read_json(mission_dir / "route_authority.json")
    transcript_path = mission_dir / "desktop_thread_transcript.json"
    if transcript_path.exists():
        capture_thread_observation(
            project_root=project_root,
            mission_id=mission_id,
            transcript_path=str(transcript_path),
            thread_id=str(_read_json(transcript_path).get("thread_id", "") or ""),
            spawned_agent_id=str(_read_json(transcript_path).get("spawned_agent_id", "") or _read_json(transcript_path).get("agent_id", "") or ""),
        )
    desktop_report = verify_desktop_native_ux(
        transcript_path=str(transcript_path),
        mission_id=mission_id,
        project_root=project_root,
        route_authority=route_authority,
    ) if transcript_path.exists() else _missing_desktop_transcript_report(mission_dir)
    spawned_authority = _read_json(mission_dir / "spawned_transcript_authority.json")
    adoption = _read_json(mission_dir / "adoption_or_recovery.json")
    probes = _read_json(mission_dir / "tool_call_adoption_probes.json")
    patch_gate = evaluate_patch_authority(report=report, mission_json=mission_json)
    recovery = _read_json(mission_dir / "recovery_proof.json")
    coverage = _mission_coverage(mission_json, report, route_authority)
    return {
        "schema_version": "mission_native_parity_report.v1",
        "mission_id": mission_id,
        "desktop_gold": {"ok": bool(desktop_report.get("ok")), "basis": "desktop_gold_passed" if desktop_report.get("ok") else "; ".join(desktop_report.get("missing_evidence", []) or [])},
        "spawned_transcript_authority": spawned_authority,
        "tool_loop": _tool_loop_gate(adoption, probes, desktop_report),
        "implementation": _implementation_lane_gate(patch_gate, desktop_report, mission_json),
        "recovery": _recovery_lane_gate(adoption, recovery, desktop_report),
        "coverage": coverage,
    }


def _missing_desktop_transcript_report(mission_dir: Path) -> JSON:
    capture_failure = _read_json(mission_dir / "capture_failure.json")
    spawn_receipt = _read_json(mission_dir / "spawn_receipt.json")
    missing = ["desktop_transcript_missing"]
    if capture_failure:
        reason = str(capture_failure.get("reason") or "")
        if reason:
            missing.append(reason)
    elif spawn_receipt.get("agent_id"):
        missing.append("read_thread_not_attempted")
        capture_failure = {
            "schema_version": "desktop_capture_failure.v1",
            "desktop_observation": "FAIL",
            "reason": "read_thread_not_attempted",
            "agent_id": str(spawn_receipt.get("agent_id") or ""),
            "attempted_read_method": "codex_app.read_thread(threadId=agent_id)",
            "list_threads_used": False,
            "next_action": "call codex_app.read_thread(threadId=agent_id) and rerun spawn-lifecycle finalize",
        }
    return {
        "ok": False,
        "missing_evidence": missing,
        "desktop_capture_failure": capture_failure,
        "spawn_receipt": spawn_receipt,
    }


def _tool_loop_gate(adoption: JSON, probes: JSON, desktop_report: JSON) -> JSON:
    status = str(adoption.get("status") or "").upper()
    stats = probes.get("adoption_stats") if isinstance(probes.get("adoption_stats"), dict) else {}
    total = int(stats.get("total", 0) or 0)
    probe_items = probes.get("probes") if isinstance(probes.get("probes"), list) else []
    all_resolved = total > 0 and all(
        isinstance(probe, dict) and (probe.get("consumer_adopted") or probe.get("recovery_used"))
        for probe in probe_items
    )
    ok = bool(desktop_report.get("ok")) and status in {"PASS", "RECOVERED"} and all_resolved
    return {
        "schema_version": "tool_loop_parity_lane.v1",
        "ok": ok,
        "status": status or "MISSING",
        "pending_tool_calls_emitted": int(adoption.get("pending_tool_calls_emitted", total) or 0),
        "probe_total": total,
        "desktop_gold_required": True,
        "basis": "tool_calls_adopted_or_recovered_with_desktop_witness" if ok else "tool_loop_parity_not_proven",
    }


def _implementation_lane_gate(patch_gate: JSON, desktop_report: JSON, mission_json: JSON) -> JSON:
    tier = str(mission_json.get("tier", "") or "").upper()
    implementation_status = str(patch_gate.get("implementation_status", "") or "")
    ok = tier in {"A4", "A5", "A6"} and bool(patch_gate.get("ok")) and bool(desktop_report.get("ok"))
    return {
        "schema_version": "implementation_parity_lane.v1",
        "ok": ok,
        "tier": tier,
        "implementation_status": implementation_status,
        "patch_authority_ok": bool(patch_gate.get("ok")),
        "desktop_gold_required": True,
        "basis": "implementation_patch_authority_with_desktop_witness" if ok else "implementation_parity_not_proven",
    }


def _recovery_lane_gate(adoption: JSON, recovery: JSON, desktop_report: JSON) -> JSON:
    adoption_status = str(adoption.get("status") or "").upper()
    recovery_status = str(recovery.get("status") or "").upper()
    recovery_artifact_ok = adoption_status == "RECOVERED" or recovery_status in {"RECOVERED", "FAIL_CLOSED"}
    ok = bool(desktop_report.get("ok")) and recovery_artifact_ok
    return {
        "schema_version": "recovery_parity_lane.v1",
        "ok": ok,
        "adoption_status": adoption_status or "MISSING",
        "recovery_status": recovery_status or "MISSING",
        "desktop_gold_required": True,
        "recovery_artifact_ok": recovery_artifact_ok,
        "basis": "recovery_or_fail_closed_with_desktop_witness" if ok else "recovery_parity_not_proven",
        "recovery_proof": recovery,
    }


def _mission_coverage(mission_json: JSON, report: JSON, route_authority: JSON) -> JSON:
    model = str(route_authority.get("model_alias") or report.get("runtime_model_alias") or report.get("explorer_model") or "")
    tier = str(mission_json.get("tier") or report.get("tier") or "")
    mode = str(mission_json.get("mode") or report.get("mode") or "")
    mission_class = tier or mode or "unknown"
    return {
        "schema_version": "mission_parity_coverage.v1",
        "models": [model] if model else [],
        "mission_classes": [mission_class] if mission_class and mission_class != "unknown" else [],
    }


def _aggregate_gate(gate_name: str, passing_missions: list[JSON], pass_basis: str, fail_basis: str) -> JSON:
    return {
        "schema_version": "native_parity_gate.v1",
        "ok": bool(passing_missions),
        "gate": gate_name,
        "basis": pass_basis if passing_missions else fail_basis,
        "mission_ids": [str(item.get("mission_id", "")) for item in passing_missions],
        "count": len(passing_missions),
    }


def _first_json(paths: list[Path | None]) -> JSON:
    for path in paths:
        payload = _read_json(path)
        if payload:
            return payload
    return {}


def _latest_mission_id(project_root: str) -> str:
    root = Path(project_root) / ".codex-oss" / "missions"
    if not root.exists():
        return ""
    dirs = [path for path in root.iterdir() if path.is_dir()]
    if not dirs:
        return ""
    return max(dirs, key=lambda path: path.stat().st_mtime).name


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
