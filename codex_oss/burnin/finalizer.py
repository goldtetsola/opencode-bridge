"""BurninFinalizerV1 — reconstruct summary from manifest + mission directories.

Callable standalone after the harness process, or as an emergency finalizer
when the main harness fails to write summary.json.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_oss.burnin.models import BurninSummary, CaseState, QualityGrade

JSON = dict[str, Any]


def finalize_burnin_run(project_root: str, run_id: str) -> JSON:
    """Reconstruct summary from manifest.json and mission directories.

    Callable independently:
      bin/codex-oss burnin finalize --run-id <run_id> --project .
    """
    run_dir = Path(project_root) / ".codex-oss" / "burnins" / run_id
    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "summary.json"
    missions_dir = Path(project_root) / ".codex-oss" / "missions"

    if not manifest_path.exists():
        return {"error": "manifest.json not found", "run_id": run_id, "run_dir": str(run_dir)}

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    expected_cases = manifest.get("expected_cases", []) or []
    summary = BurninSummary(
        run_id=run_id,
        suite=manifest.get("suite", ""),
        summary_complete=True,
        started_at=manifest.get("started_at", ""),
        finished_at=datetime.now(timezone.utc).isoformat(),
        expected_cases=len(expected_cases),
    )

    completed = 0
    failed_ids: list[str] = []
    missing_ids: list[str] = []
    earned = 0
    runtime_completes = 0
    suspicious = 0
    false_completes = 0
    truthful_partials = 0
    raw_dumps = 0
    model_closures = 0
    runtime_closures = 0
    evidence_failures = 0
    cleanup_ratings: dict[str, int] = {}

    for case in expected_cases:
        case_id = case.get("case_id", "")
        mission_id = case.get("mission_id", "")
        submitted = False
        found_mission_dir = None

        # Find matching mission directory
        for entry in os.listdir(missions_dir) if missions_dir.exists() else []:
            if entry.startswith(mission_id):
                found_mission_dir = missions_dir / entry
                submitted = True
                break

        if not found_mission_dir:
            missing_ids.append(case_id)
            continue

        report_path = found_mission_dir / "report.json"
        if not report_path.exists():
            failed_ids.append(case_id)
            continue

        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError):
            failed_ids.append(case_id)
            continue

        status = report.get("status", "")
        closure = report.get("closure_source", "")
        caveats = " ".join(report.get("caveats", []) or [])

        # Read answer graph for sufficiency
        sufficiency = {}
        ag_path = found_mission_dir / "answer_graph.json"
        if ag_path.exists():
            try:
                with open(ag_path, "r", encoding="utf-8") as f:
                    ag = json.load(f)
                sufficiency = ag.get("sufficiency", {}) or {}
            except (json.JSONDecodeError, OSError):
                pass

        # Read ledger for observations
        ledger_path = found_mission_dir / "ledger.json"
        has_observations = False
        if ledger_path.exists():
            try:
                with open(ledger_path, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
                commands = ledger.get("commands_run", ledger.get("commands", [])) or []
                has_observations = len(commands) > 0
            except (json.JSONDecodeError, OSError):
                pass

        completed += 1

        # Quality classification
        is_runtime_closed = "runtime_answer_graph" in closure or "runtime" in closure
        is_model_closed = "model_report" in closure

        if is_runtime_closed:
            runtime_closures += 1
        if is_model_closed:
            model_closures += 1

        can_close = sufficiency.get("can_close", True)
        entitlement = sufficiency.get("closure_entitlement", {}) or {}
        can_return = entitlement.get("can_return_complete", True)
        operator_asserted = "operator asserted" in caveats.lower() or "zero file reads" in caveats.lower()

        is_false = False
        if status == "COMPLETE":
            if not can_close or not can_return:
                is_false = True
            elif not sufficiency.get("required_answered", 1) and sufficiency.get("required_total", 1) > sufficiency.get("required_answered", 0):
                is_false = True
            missing_src = sufficiency.get("missing_required_sources", []) or []
            if missing_src:
                is_false = True

        is_suspicious = False
        if status == "COMPLETE" and not is_false:
            if not has_observations and "deterministic" not in closure.lower():
                is_suspicious = True
            if operator_asserted:
                is_suspicious = True

        if is_false:
            false_completes += 1
        if is_suspicious:
            suspicious += 1

        if status == "COMPLETE" and not is_false and not is_suspicious:
            earned += 1
        elif status == "COMPLETE" and is_runtime_closed and not is_false:
            runtime_completes += 1
        if status in ("PARTIAL", "ESCALATE") and has_observations:
            truthful_partials += 1

        if "raw dump" in caveats.lower():
            raw_dumps += 1
        if "evidence ref" in caveats.lower() and "not found" in caveats.lower():
            evidence_failures += 1

        rating = _cleanup_rating(report)
        cleanup_ratings[rating] = cleanup_ratings.get(rating, 0) + 1

    summary.completed_cases = completed
    summary.missing_cases = missing_ids
    summary.failed_cases = failed_ids
    summary.submitted_cases = completed + len(failed_ids)
    summary.mission_artifacts_created = completed
    summary.false_complete_count = false_completes
    summary.suspicious_complete_count = suspicious
    summary.earned_complete_count = earned
    summary.runtime_complete_count = runtime_completes
    summary.truthful_partial_count = truthful_partials
    summary.raw_dump_incidents = raw_dumps
    summary.evidence_ref_failures = evidence_failures
    summary.model_self_close_rate = _rate(model_closures, max(completed, 1))
    summary.runtime_rescue_rate = _rate(runtime_closures, max(completed, 1))

    useful = completed - false_completes - suspicious
    summary.useful_complete_or_partial_rate = _rate(useful, max(completed, 1))
    summary.earned_complete_rate = _rate(earned, max(completed, 1))
    summary.gpt_cleanup_distribution = cleanup_ratings

    # Truth-safe vs native-feeling
    truth_safe_ok = (
        false_completes == 0
        and raw_dumps == 0
        and evidence_failures == 0
        and (suspicious == 0)
    )
    native_feeling_ok = (
        summary.model_self_close_rate >= 0.2
        and summary.earned_complete_rate >= 0.3
        and not missing_ids
    )

    summary.result = "PASS" if truth_safe_ok else "FAIL"
    summary.result_reasons = []
    if not truth_safe_ok:
        summary.result_reasons.append("truth_safe_fail")
    if truth_safe_ok and not native_feeling_ok:
        summary.result_reasons.append("native_feeling_fail")

    result_dict = summary.to_dict()

    # Add truth-safe / native-feeling split
    result_dict["truth_safe"] = {
        "pass": truth_safe_ok,
        "false_complete_count": false_completes,
        "raw_dump_count": raw_dumps,
        "suspicious_complete_count": suspicious,
        "evidence_ref_failures": evidence_failures,
    }
    result_dict["native_feeling"] = {
        "pass": native_feeling_ok,
        "model_self_close_rate": round(summary.model_self_close_rate, 2),
        "runtime_rescue_rate": round(summary.runtime_rescue_rate, 2),
        "earned_complete_rate": round(summary.earned_complete_rate, 2),
    }

    # Atomic write
    tmp_path = summary_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, indent=2)
    os.replace(tmp_path, summary_path)

    return result_dict


def _rate(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total


def _cleanup_rating(report: JSON) -> str:
    caveats = len(report.get("caveats", []) or [])
    if caveats <= 2:
        return "minor"
    if caveats <= 4:
        return "moderate"
    return "major"
