"""Executable certification gates for OSS runtime claims."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from .legacy import archive_legacy_missions
from .metrics import broad_low_risk_runtime_status, summarize_missions
from .proofs import refresh_claim_proofs
from .raw_lane import summarize_raw_probes


def certify_project(project_root: str, target: str = "all", refresh: bool = True) -> dict[str, Any]:
    refresh_results: dict[str, Any] = {}
    if refresh:
        refresh_results["proof"] = refresh_claim_proofs(project_root, suite="proof")
        refresh_results["operational"] = refresh_claim_proofs(project_root, suite="operational")

    proof_summary = summarize_missions(project_root, proof_only=True)
    operational_summary = summarize_missions(project_root, operational_only=True)
    current_summary = summarize_missions(project_root, current_only=True)
    raw_summary = summarize_raw_probes(project_root)
    legacy_plan = archive_legacy_missions(project_root, apply=False)
    broad_low_risk = broad_low_risk_runtime_status(operational_summary)

    gates = {
        "proof_path_supported": _gate(
            proof_summary.get("claim_status", {}).get("all_supported", False),
            f"proof_eligible_missions={proof_summary.get('eligible_missions', 0)}",
        ),
        "operational_path_supported": _gate(
            operational_summary.get("claim_status", {}).get("all_supported", False),
            f"operational_eligible_missions={operational_summary.get('eligible_missions', 0)}",
        ),
        "current_path_supported": _gate(
            current_summary.get("claim_status", {}).get("all_supported", False),
            f"current_eligible_missions={current_summary.get('eligible_missions', 0)}",
        ),
        "broader_low_risk_supported": _gate(
            broad_low_risk.get("status") == "SUPPORTED",
            broad_low_risk.get("basis", ""),
        ),
        "open_investigation_supported": _gate(
            current_summary.get("promotion_evidence", {}).get("open_investigation_runtime", {}).get("status") == "SUPPORTED",
            current_summary.get("promotion_evidence", {}).get("open_investigation_runtime", {}).get("basis", ""),
        ),
        "legacy_clean": _gate(
            int(legacy_plan.get("planned_count", 0) or 0) == 0,
            f"legacy_planned_count={legacy_plan.get('planned_count', 0)}",
        ),
        "raw_lane_smoke_supported": _gate(
            raw_summary.get("smoke_claim_status", {}).get("status") == "SUPPORTED",
            raw_summary.get("smoke_claim_status", {}).get("basis", ""),
        ),
        "raw_lane_supported": _gate(
            raw_summary.get("claim_status", {}).get("status") == "SUPPORTED",
            raw_summary.get("claim_status", {}).get("basis", ""),
        ),
    }

    target_verdicts = {
        "runtime_backed": _target_verdict(
            [
                gates["proof_path_supported"],
                gates["operational_path_supported"],
                gates["current_path_supported"],
                gates["broader_low_risk_supported"],
                gates["open_investigation_supported"],
            ]
        ),
        "open_investigation": _target_verdict([gates["open_investigation_supported"]]),
        "repo_hygiene": _target_verdict([gates["legacy_clean"]]),
        "raw_free_editing_smoke": _target_verdict([gates["raw_lane_smoke_supported"]]),
        "raw_free_editing": _target_verdict([gates["raw_lane_supported"]]),
    }
    target_verdicts["all"] = _target_verdict(
        [
            {"ok": target_verdicts["runtime_backed"]["status"] == "CERTIFIED"},
            {"ok": target_verdicts["open_investigation"]["status"] == "CERTIFIED"},
            {"ok": target_verdicts["repo_hygiene"]["status"] == "CERTIFIED"},
            {"ok": target_verdicts["raw_free_editing"]["status"] == "CERTIFIED"},
        ]
    )

    effective_target = target if target in {"runtime_backed", "open_investigation", "repo_hygiene", "raw_free_editing_smoke", "raw_free_editing", "all"} else "all"
    verdict = target_verdicts[effective_target]
    report = {
        "certification_report_version": "1.0",
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": effective_target,
        "refresh": bool(refresh),
        "refresh_results": refresh_results,
        "gates": gates,
        "targets": target_verdicts,
        "summaries": {
            "proof": _summary_snapshot(proof_summary),
            "operational": _summary_snapshot(operational_summary),
            "current": _summary_snapshot(current_summary),
            "raw": raw_summary.get("claim_status", {}),
            "legacy_plan_count": int(legacy_plan.get("planned_count", 0) or 0),
        },
        "verdict": verdict,
    }
    _write_certification_artifacts(project_root, report)
    return report


def _gate(ok: bool, basis: str) -> dict[str, Any]:
    return {"ok": bool(ok), "basis": basis}


def _target_verdict(gates: list[dict[str, Any]]) -> dict[str, Any]:
    certified = bool(gates) and all(bool(gate.get("ok")) for gate in gates)
    return {
        "status": "CERTIFIED" if certified else "UNCONFIRMED",
    }


def _summary_snapshot(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "eligible_missions": int(summary.get("eligible_missions", 0) or 0),
        "audit_fail_count": int(summary.get("audit_fail_count", 0) or 0),
        "claim_status": summary.get("claim_status", {}),
        "promotion_evidence": summary.get("promotion_evidence", {}),
    }


def _write_certification_artifacts(project_root: str, report: dict[str, Any]) -> None:
    artifact_root = os.path.join(project_root, ".codex-oss", "certifications")
    os.makedirs(artifact_root, exist_ok=True)
    target = str(report.get("target", "all") or "all")
    json_path = os.path.join(artifact_root, f"{target}.json")
    md_path = os.path.join(artifact_root, f"{target}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(_render_certification_markdown(report))


def _render_certification_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Certification Report",
        "",
        f"- Target: {report.get('target', '')}",
        f"- Verdict: {report.get('verdict', {}).get('status', '')}",
        "",
        "## Gates",
        "",
    ]
    for name, gate in (report.get("gates", {}) or {}).items():
        status = "PASS" if gate.get("ok") else "FAIL"
        lines.append(f"- {status} `{name}`: {gate.get('basis', '')}")
    lines.append("")
    lines.append("## Targets")
    lines.append("")
    for name, verdict in (report.get("targets", {}) or {}).items():
        lines.append(f"- `{name}`: {verdict.get('status', '')}")
    lines.append("")
    return "\n".join(lines) + "\n"
