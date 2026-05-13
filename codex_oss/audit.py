"""Mission artifact auditing for runtime-backed OSS tasks."""

from __future__ import annotations

import json
import os
from typing import Any


def audit_mission(project_root: str, mission_id: str) -> dict[str, Any]:
    mission_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    checks: list[dict[str, Any]] = []
    if not os.path.isdir(mission_dir):
        return {
            "ok": False,
            "mission_id": mission_id,
            "mission_dir": mission_dir,
            "checks": [{"name": "mission_dir", "ok": False, "message": "Mission artifact directory is missing"}],
        }

    mission = _read_json_if_exists(os.path.join(mission_dir, "mission.json"))
    ledger = _read_json_if_exists(os.path.join(mission_dir, "ledger.json"))
    report = _read_json_if_exists(os.path.join(mission_dir, "report.json"))
    validation = _read_json_if_exists(os.path.join(mission_dir, "validation.json"))
    verification = _read_json_if_exists(os.path.join(mission_dir, "verification.json"))
    certification = _read_json_if_exists(os.path.join(mission_dir, "certification.json"))
    trace_grading = _read_json_if_exists(os.path.join(mission_dir, "trace_grading.json"))
    decision_trace = _read_json_if_exists(os.path.join(mission_dir, "decision_trace.json"))
    claim_graph = _read_json_if_exists(os.path.join(mission_dir, "claim_graph.json"))
    answer_graph = _read_json_if_exists(os.path.join(mission_dir, "answer_graph.json"))
    coverage_graph = _read_json_if_exists(os.path.join(mission_dir, "coverage_graph.json"))
    evidence_agenda = _read_json_if_exists(os.path.join(mission_dir, "evidence_agenda.json"))
    investigation_plan = _read_json_if_exists(os.path.join(mission_dir, "investigation_plan.json"))
    summary_path = os.path.join(mission_dir, "summary.md")
    trace_path = os.path.join(mission_dir, "trace.jsonl")
    patch_path = os.path.join(mission_dir, "patch.diff")
    rollback_path = os.path.join(mission_dir, "rollback.diff")
    readiness_path = os.path.join(mission_dir, "implementation_readiness_graph.json")

    checks.append(_check("mission_json", isinstance(mission, dict), "mission.json present"))
    checks.append(_check("report_json", isinstance(report, dict), "report.json present"))
    checks.append(_check("decision_trace_json", isinstance(decision_trace, dict), "decision_trace.json present"))

    tier = str(mission.get("tier", "") or "") if isinstance(mission, dict) else ""
    implementation_tier = tier in {"A4", "A5", "A6"}
    if implementation_tier:
        checks.append(_check("validation_json", isinstance(validation, dict), "validation.json present"))
        checks.append(_check("patch_artifact", os.path.exists(patch_path), "patch.diff present"))
        checks.append(_check("rollback_artifact", os.path.exists(rollback_path), "rollback.diff present"))
        checks.append(_check("implementation_readiness_graph_json", os.path.exists(readiness_path), "implementation_readiness_graph.json present"))
        coverage_path = os.path.join(mission_dir, "implementation_coverage_graph.json")
        if os.path.exists(coverage_path):
            checks.append(_check("implementation_coverage_graph_json", True, "implementation_coverage_graph.json present"))
        checks.append(_check("verification_json", isinstance(verification, list), "verification.json present"))
        checks.append(_check("ledger_json", isinstance(ledger, dict), "ledger.json present"))
        checks.append(_check("trace_jsonl", os.path.exists(trace_path), "trace.jsonl present"))
        checks.append(_check("summary_md", os.path.exists(summary_path), "summary.md present"))
    else:
        checks.append(_check("ledger_json", isinstance(ledger, dict), "ledger.json present"))
        checks.append(_check("trace_jsonl", os.path.exists(trace_path), "trace.jsonl present"))
        checks.append(_check("summary_md", os.path.exists(summary_path), "summary.md present"))
        checks.append(_check("trace_grading_json", isinstance(trace_grading, dict), "trace_grading.json present"))
        checks.append(_check("claim_graph_json", isinstance(claim_graph, dict), "claim_graph.json present"))
        if str((mission or {}).get("objective_style", "") or "") == "open_investigation":
            checks.append(_check("investigation_plan_json", isinstance(investigation_plan, dict), "investigation_plan.json present"))
            checks.append(_check("answer_graph_json", isinstance(answer_graph, dict), "answer_graph.json present"))
            checks.append(_check("coverage_graph_json", isinstance(coverage_graph, dict), "coverage_graph.json present"))
            checks.append(_check("evidence_agenda_json", isinstance(evidence_agenda, dict), "evidence_agenda.json present"))

    if isinstance(report, dict) and isinstance(validation, dict):
        checks.append(_check(
            "changed_files_match",
            sorted(report.get("changed_files", []) or []) == sorted(validation.get("changed_files", []) or []),
            "report/validation changed_files match",
        ))
        checks.append(_check(
            "report_status_consistent",
            str(report.get("status", "") or "") != "",
            "report status present",
        ))
        envelope = report.get("completion_envelope", {}) or {}
        env_status = envelope.get("final_status", "")
        report_status = str(report.get("status", "") or "")
        checks.append(_check(
            "report_envelope_consistent",
            not env_status or report_status == env_status,
            f"report.status ({report_status}) matches completion_envelope.final_status ({env_status})" if (not env_status or report_status == env_status) else f"report.status ({report_status}) != completion_envelope.final_status ({env_status})",
        ))
        checks.append(_check(
            "semantic_review_recorded",
            "semantic_review_ok" in report or "semantic_review_ok" in validation,
            "semantic review recorded",
        ))
        checks.append(_check(
            "semantic_review_score_recorded",
            "semantic_review_score" in report or "semantic_review_score" in validation,
            "semantic review score recorded",
        ))
        checks.append(_check(
            "verification_policy_recorded",
            "verification_plan_ok" in report or "verification_plan_ok" in validation,
            "verification policy recorded",
        ))
        checks.append(_check(
            "verification_plan_score_recorded",
            "verification_plan_score" in report or "verification_plan_score" in validation,
            "verification plan score recorded",
        ))
        checks.append(_check(
            "verification_scope_recorded",
            "verification_scope" in report,
            "verification scope recorded",
        ))
        checks.append(_check(
            "implementation_readiness_recorded",
            "implementation_readiness" in report and "implementation_readiness_graph" in validation,
            "implementation readiness recorded in report and validation",
        ))
        checks.append(_check(
            "implementation_coverage_recorded",
            True,
            "implementation coverage recorded in validation" if "implementation_coverage_graph" in validation else "implementation coverage (legacy: not yet recorded)",
        ))
        checks.append(_check(
            "semantic_review_recorded",
            True,
            "semantic review recorded in validation" if "semantic_review" in validation else "semantic review (legacy: not yet recorded)",
        ))
        checks.append(_check(
            "rollback_recorded",
            isinstance(report.get("rollback"), dict) and bool(report.get("rollback", {}).get("artifact")),
            "rollback artifact recorded in report",
        ))
        checks.append(_check(
            "implementation_trace_present",
            os.path.exists(trace_path),
            "implementation trace.jsonl present",
        ))
        if str(report.get("status", "")).upper() == "VERIFIED":
            verification_ok = isinstance(verification, list) and verification and all(
                isinstance(item, dict) and item.get("exit_code") == 0 for item in verification
            )
            checks.append(_check("verified_has_green_verification", verification_ok, "VERIFIED reports have successful verification"))
        if str(report.get("status", "")).upper() == "VERIFICATION_FAILED":
            checks.append(_check(
                "rollback_caveat_present",
                any("rolled back" in str(caveat).lower() for caveat in report.get("caveats", []) or []),
                "rollback caveat present for verification failure",
            ))

    if isinstance(mission, dict) and isinstance(report, dict):
        workspace_policy = mission.get("workspace_apply_policy", {}) if isinstance(mission.get("workspace_apply_policy"), dict) else {}
        checks.append(_check(
            "mission_id_matches",
            str(mission.get("mission_id", "")) == mission_id,
            "mission.json mission_id matches directory",
        ))
        checks.append(_check(
            "workspace_mode_recorded",
            "apply_mode" in mission if implementation_tier else True,
            "mission records apply_mode",
        ))
        certification_required = bool(workspace_policy.get("certification_required")) or str(mission.get("apply_mode", "")) == "critical_workspace_certified"
        if certification_required:
            checks.append(_check(
                "certification_json",
                isinstance(certification, dict),
                "certification.json present when certification is required",
            ))
        if certification_required and isinstance(certification, dict):
            checks.append(_check(
                "certification_reviewers_recorded",
                bool(certification.get("reviewer_models")),
                "certification reviewer models recorded",
            ))
            checks.append(_check(
                "certification_threshold_recorded",
                "min_reviewer_approvals" in certification,
                "minimum reviewer approval threshold recorded",
            ))
            checks.append(_check(
                "certification_proof_grade_recorded",
                str(certification.get("proof_grade", "") or "") in {"PROOF_GRADED", "BASIC_APPROVED", "UNCONFIRMED"},
                "certification proof grade recorded",
            ))
            if workspace_policy.get("require_isolated_preflight"):
                checks.append(_check(
                    "preflight_recorded",
                    isinstance(certification.get("preflight"), dict),
                    "isolated preflight recorded",
                ))
                checks.append(_check(
                    "preflight_ok",
                    bool((certification.get("preflight") or {}).get("ok")),
                    "isolated preflight passed",
                ))
            if workspace_policy.get("require_rollback_proof"):
                checks.append(_check(
                    "rollback_proof_recorded",
                    isinstance(certification.get("rollback_proof"), dict),
                    "rollback proof recorded",
                ))
                checks.append(_check(
                    "rollback_proof_ok",
                    bool((certification.get("rollback_proof") or {}).get("ok")),
                    "rollback proof passed",
                ))
            invariant_commands = workspace_policy.get("invariant_commands") if isinstance(workspace_policy.get("invariant_commands"), list) else []
            if invariant_commands:
                checks.append(_check(
                    "invariants_recorded",
                    isinstance(certification.get("invariant_results"), list),
                    "invariant results recorded",
                ))
                checks.append(_check(
                    "invariants_green",
                    all(isinstance(item, dict) and item.get("exit_code") == 0 for item in (certification.get("invariant_results") or [])),
                    "all invariant commands passed",
                ))
            if str(report.get("status", "")).upper() in {"VERIFIED", "APPLIED_TO_WORKSPACE"}:
                checks.append(_check(
                    "certification_approved",
                    bool(certification.get("approved_for_workspace_apply")),
                    "workspace apply certification approved",
                ))
    if isinstance(mission, dict) and isinstance(report, dict) and not implementation_tier:
        checks.append(_check(
            "readonly_status_present",
            str(report.get("status", "") or "") != "",
            "read-only report status present",
        ))
        checks.append(_check(
            "readonly_trace_labels_recorded",
            isinstance(trace_grading, dict) and bool(trace_grading.get("labels")),
            "read-only trace grading labels recorded",
        ))
        checks.append(_check(
            "readonly_claim_graph_sufficiency_recorded",
            isinstance(claim_graph, dict) and isinstance(claim_graph.get("sufficiency"), dict),
            "read-only claim graph sufficiency recorded",
        ))
        checks.append(_check(
            "readonly_files_match_ledger",
            isinstance(ledger, dict) and isinstance(report.get("files_inspected", []), list),
            "read-only report has ledger-backed file evidence structure",
        ))
        if str(mission.get("objective_style", "") or "") == "open_investigation":
            checks.append(_check(
                "open_claim_graph_phase_history_recorded",
                isinstance(claim_graph, dict) and bool(claim_graph.get("phase_history")),
                "open-investigation claim graph phase history recorded",
            ))
            checks.append(_check(
                "open_answer_graph_sufficiency_recorded",
                isinstance(answer_graph, dict) and isinstance(answer_graph.get("sufficiency"), dict),
                "open-investigation answer graph sufficiency recorded",
            ))
            checks.append(_check(
                "open_answer_graph_obligations_recorded",
                isinstance(answer_graph, dict) and bool(answer_graph.get("required_obligations")),
                "open-investigation answer obligations recorded",
            ))
            checks.append(_check(
                "open_closure_source_recorded",
                str(report.get("closure_source", "") or "") != "",
                "open-investigation closure source recorded",
            ))
            checks.append(_check(
                "open_coverage_graph_status_recorded",
                isinstance(coverage_graph, dict) and isinstance(coverage_graph.get("coverage_status"), dict),
                "open-investigation coverage status recorded",
            ))
            checks.append(_check(
                "open_evidence_agenda_items_recorded",
                isinstance(evidence_agenda, dict) and isinstance(evidence_agenda.get("items"), list),
                "open-investigation evidence agenda recorded",
            ))
            checks.append(_check(
                "open_answer_graph_missing_sources_recorded",
                isinstance(answer_graph, dict) and isinstance((answer_graph.get("sufficiency") or {}).get("missing_required_sources"), list),
                "open-investigation missing required sources recorded",
            ))

    return {
        "ok": all(bool(item.get("ok")) for item in checks),
        "mission_id": mission_id,
        "mission_dir": mission_dir,
        "checks": checks,
    }


def _read_json_if_exists(path: str) -> Any:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _check(name: str, ok: bool, message: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "message": message}
