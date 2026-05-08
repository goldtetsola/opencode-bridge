"""Mission metrics aggregation for runtime-backed OSS tasks."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Any

from .audit import audit_mission


def summarize_missions(
    project_root: str,
    tier_filter: str | None = None,
    proof_only: bool = False,
    operational_only: bool = False,
    current_only: bool = False,
) -> dict[str, Any]:
    missions_root = os.path.join(project_root, ".codex-oss", "missions")
    if not os.path.isdir(missions_root):
        return {
            "summary_version": "1.0",
            "project_root": project_root,
            "total_missions": 0,
            "missions": [],
            "promotion_evidence": _promotion_evidence({}),
        }

    mission_ids = sorted(
        name for name in os.listdir(missions_root)
        if os.path.isdir(os.path.join(missions_root, name))
    )
    missions: list[dict[str, Any]] = []
    for mission_id in mission_ids:
        mission_dir = os.path.join(missions_root, mission_id)
        mission = _read_json_if_exists(os.path.join(mission_dir, "mission.json")) or {}
        if proof_only and not bool(mission.get("promotion_proof")):
            continue
        if operational_only and not bool(mission.get("operational_burnin")):
            continue
        if current_only and not (bool(mission.get("promotion_proof")) or bool(mission.get("operational_burnin"))):
            continue
        if tier_filter and str(mission.get("tier", "")) != tier_filter:
            continue
        report = _read_json_if_exists(os.path.join(mission_dir, "report.json")) or {}
        validation = _read_json_if_exists(os.path.join(mission_dir, "validation.json")) or {}
        certification = _read_json_if_exists(os.path.join(mission_dir, "certification.json")) or {}
        trace_grading = _read_json_if_exists(os.path.join(mission_dir, "trace_grading.json")) or {}
        audited = audit_mission(project_root, mission_id)
        missions.append({
            "mission_id": mission_id,
            "tier": str(mission.get("tier", "") or ""),
            "apply_mode": str(mission.get("apply_mode", "") or ""),
            "report_status": str(report.get("status", "") or ""),
            "validation_status": str(validation.get("status", "") or ""),
            "report_source": str(report.get("report_source", "") or ""),
            "proposal_source": str(report.get("proposal_source", "") or validation.get("proposal_source", "") or ""),
            "certification_status": str(certification.get("status", "") or ""),
            "certification_proof_grade": str(certification.get("proof_grade", "") or ""),
            "certified": bool(certification.get("approved_for_workspace_apply", False)),
            "main_workspace_mutated": bool(report.get("main_workspace_mutated", False)),
            "semantic_review_score": int(report.get("semantic_review_score", validation.get("semantic_review_score", 0)) or 0),
            "verification_plan_score": int(report.get("verification_plan_score", validation.get("verification_plan_score", 0)) or 0),
            "gpt_review_required": bool(report.get("gpt_review_required", True)),
            "trace_labels": list(trace_grading.get("labels", []) or []),
            "audit_ok": bool(audited.get("ok", False)),
            "promotion_proof": bool(mission.get("promotion_proof")),
            "operational_burnin": bool(mission.get("operational_burnin")),
        })

    summary = _summarize_rows(project_root, missions)
    summary["promotion_evidence"] = _promotion_evidence(summary)
    summary["claim_status"] = build_claim_status(summary)
    return summary


def build_claim_status(summary: dict[str, Any]) -> dict[str, Any]:
    evidence = summary.get("promotion_evidence", {}) if isinstance(summary.get("promotion_evidence"), dict) else {}
    supported = sorted(key for key, value in evidence.items() if isinstance(value, dict) and value.get("status") == "SUPPORTED")
    unconfirmed = sorted(key for key, value in evidence.items() if isinstance(value, dict) and value.get("status") != "SUPPORTED")
    return {
        "claim_status_version": "1.0",
        "all_supported": not unconfirmed and bool(evidence),
        "supported_claims": supported,
        "unconfirmed_claims": unconfirmed,
    }


def broad_low_risk_runtime_status(summary: dict[str, Any]) -> dict[str, Any]:
    impl = summary.get("implementation", {}) if isinstance(summary.get("implementation"), dict) else {}
    supported = (
        int(impl.get("count", 0) or 0) >= 4
        and int(impl.get("verified_count", 0) or 0) >= 3
        and int(summary.get("eligible_audit_fail_count", 0) or 0) == 0
        and float(impl.get("semantic_review_score_avg", 0.0) or 0.0) >= 70.0
        and float(impl.get("verification_plan_score_avg", 0.0) or 0.0) >= 20.0
    )
    return {
        "status": "SUPPORTED" if supported else "UNCONFIRMED",
        "basis": (
            f"implementation_count={impl.get('count', 0)} "
            f"verified_count={impl.get('verified_count', 0)} "
            f"eligible_audit_fail_count={summary.get('eligible_audit_fail_count', 0)} "
            f"semantic_review_score_avg={impl.get('semantic_review_score_avg', 0.0)} "
            f"verification_plan_score_avg={impl.get('verification_plan_score_avg', 0.0)}"
        ),
    }


def _summarize_rows(project_root: str, missions: list[dict[str, Any]]) -> dict[str, Any]:
    eligible_tiers = {"A2", "A3", "A4", "A5", "A6"}
    by_tier: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "audit_ok_count": 0,
        "report_statuses": Counter(),
        "validation_statuses": Counter(),
        "apply_modes": Counter(),
    })
    report_sources = Counter()
    proposal_sources = Counter()
    certification_statuses = Counter()
    certification_proof_grades = Counter()
    trace_labels = Counter()

    eligible_rows = [row for row in missions if row["tier"] in eligible_tiers]
    implementation_rows = [row for row in eligible_rows if row["tier"] in {"A4", "A5", "A6"}]
    readonly_rows = [row for row in eligible_rows if row["tier"] in {"A2", "A3"}]
    legacy_rows = [row for row in missions if row["tier"] not in eligible_tiers]

    for row in missions:
        entry = by_tier[row["tier"]]
        entry["count"] += 1
        entry["audit_ok_count"] += int(bool(row["audit_ok"]))
        entry["report_statuses"][row["report_status"]] += 1
        entry["validation_statuses"][row["validation_status"]] += 1
        entry["apply_modes"][row["apply_mode"]] += 1
        report_sources[row["report_source"]] += 1
        proposal_sources[row["proposal_source"]] += 1
        if row["certification_status"]:
            certification_statuses[row["certification_status"]] += 1
        if row["certification_proof_grade"]:
            certification_proof_grades[row["certification_proof_grade"]] += 1
        for label in row["trace_labels"]:
            trace_labels[label] += 1

    return {
        "summary_version": "1.0",
        "project_root": project_root,
        "total_missions": len(missions),
        "eligible_missions": len(eligible_rows),
        "legacy_or_incomplete_missions": len(legacy_rows),
        "missions": missions,
        "audit_ok_count": sum(1 for row in missions if row["audit_ok"]),
        "audit_fail_count": sum(1 for row in missions if not row["audit_ok"]),
        "eligible_audit_ok_count": sum(1 for row in eligible_rows if row["audit_ok"]),
        "eligible_audit_fail_count": sum(1 for row in eligible_rows if not row["audit_ok"]),
        "by_tier": {
            tier: {
                "count": data["count"],
                "audit_ok_count": data["audit_ok_count"],
                "report_statuses": dict(data["report_statuses"]),
                "validation_statuses": dict(data["validation_statuses"]),
                "apply_modes": dict(data["apply_modes"]),
            }
            for tier, data in sorted(by_tier.items())
        },
        "report_sources": dict(report_sources),
        "proposal_sources": dict(proposal_sources),
        "certification_statuses": dict(certification_statuses),
        "certification_proof_grades": dict(certification_proof_grades),
        "trace_labels": dict(trace_labels),
        "implementation": {
            "count": len(implementation_rows),
            "audit_ok_count": sum(1 for row in implementation_rows if row["audit_ok"]),
            "verified_count": sum(1 for row in implementation_rows if row["report_status"] == "VERIFIED"),
            "workspace_mutation_count": sum(1 for row in implementation_rows if row["main_workspace_mutated"]),
            "certified_count": sum(1 for row in implementation_rows if row["certified"]),
            "proof_graded_certified_count": sum(
                1
                for row in implementation_rows
                if row["certified"] and row["certification_proof_grade"] == "PROOF_GRADED"
            ),
            "semantic_review_score_avg": _avg(row["semantic_review_score"] for row in implementation_rows),
            "verification_plan_score_avg": _avg(row["verification_plan_score"] for row in implementation_rows),
        },
        "read_only": {
            "count": len(readonly_rows),
            "audit_ok_count": sum(1 for row in readonly_rows if row["audit_ok"]),
            "complete_count": sum(1 for row in readonly_rows if row["report_status"] == "COMPLETE"),
            "productive_exploration_count": sum(1 for row in readonly_rows if "productive_exploration" in row["trace_labels"]),
        },
    }


def _promotion_evidence(summary: dict[str, Any]) -> dict[str, Any]:
    total = int(summary.get("eligible_missions", 0) or 0)
    legacy = int(summary.get("legacy_or_incomplete_missions", 0) or 0)
    impl = summary.get("implementation", {}) if isinstance(summary.get("implementation"), dict) else {}
    readonly = summary.get("read_only", {}) if isinstance(summary.get("read_only"), dict) else {}

    def claim(name: str, supported: bool, basis: str) -> dict[str, Any]:
        return {
            "claim": name,
            "status": "SUPPORTED" if supported else "UNCONFIRMED",
            "basis": basis,
        }

    return {
        "runtime_backed_investigation": claim(
            "runtime_backed_investigation",
            bool(readonly.get("count")) and readonly.get("audit_ok_count", 0) == readonly.get("count", 0),
            f"eligible_read_only_count={readonly.get('count', 0)} audit_ok={readonly.get('audit_ok_count', 0)} legacy_or_incomplete={legacy}",
        ),
        "bounded_implementation": claim(
            "bounded_implementation",
            bool(impl.get("count")) and impl.get("audit_ok_count", 0) == impl.get("count", 0),
            f"eligible_implementation_count={impl.get('count', 0)} audit_ok={impl.get('audit_ok_count', 0)} verified={impl.get('verified_count', 0)} legacy_or_incomplete={legacy}",
        ),
        "workspace_apply_evidence": claim(
            "workspace_apply_evidence",
            impl.get("workspace_mutation_count", 0) > 0,
            f"eligible_workspace_mutation_count={impl.get('workspace_mutation_count', 0)} legacy_or_incomplete={legacy}",
        ),
        "critical_certification_evidence": claim(
            "critical_certification_evidence",
            int(impl.get("proof_graded_certified_count", 0) or 0) > 0,
            f"eligible_proof_graded_certified_count={impl.get('proof_graded_certified_count', 0)} eligible_missions={total} legacy_or_incomplete={legacy}",
        ),
    }


def _avg(values) -> float:
    values = [float(value) for value in values]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _read_json_if_exists(path: str) -> Any:
    if not os.path.exists(path):
        return None
    import json

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
