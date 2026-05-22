"""Deterministic factual fast paths for narrow A2/A3 missions."""

from __future__ import annotations

from typing import Any

from codex_oss.runtime import exec_grep, exec_read
from codex_oss.runtime.objectives import classify_objective, objective_coverage_errors, synthesize_objective_finding
from codex_oss.validation import validate_report


SUPPORTED_FAST_PATH_TYPES = {
    "function_location",
    "mapping_lookup",
    "config_value_extraction",
    "zero_match_evidence",
}


def run_deterministic_fast_path(mission: Any, ledger: Any) -> dict[str, Any] | None:
    """Return a terminal mission result for narrow explicit factual missions.

    This intentionally handles only small, deterministic cases where the runtime
    can gather enough evidence without a full model tool loop.
    """
    spec = classify_objective(mission)
    if not spec.explicit or spec.objective_type not in SUPPORTED_FAST_PATH_TYPES:
        return None

    allowed_paths = list(getattr(mission, "allowed_paths", []) or [])
    allowed_roots = list(getattr(mission, "allowed_roots", []) or [])
    if len(allowed_paths) != 1 or allowed_roots:
        return None
    if getattr(mission, "write_allowed", False):
        return None

    path = allowed_paths[0]
    if spec.objective_type == "zero_match_evidence":
        pattern = str((spec.raw.get("target", {}) or {}).get("pattern") or spec.target or "")
        if not pattern:
            return None
        result = exec_grep(pattern, path)
        ledger.add_command("rtk_grep", {"pattern": pattern, "path": path}, result, len(getattr(ledger, "commands_run", [])))
    else:
        result = exec_read(path)
        ledger.add_file(path, result, len(getattr(ledger, "commands_run", [])))
        ledger.add_command("rtk_read", {"path": path}, result, len(getattr(ledger, "commands_run", [])))

    finding = synthesize_objective_finding(mission, ledger)
    if not finding:
        return None

    report = {
        "oss_report_version": "1.0",
        "mission_id": getattr(mission, "mission_id", "unknown"),
        "status": "COMPLETE",
        "confidence": "LOW",
        "report_source": "deterministic_fast_path",
        "runtime_model_alias": getattr(mission, "runtime_model_alias", ""),
        "explorer_model": "",
        "finalizer_model": None,
        "fallback_model_used": False,
        "files_inspected": [{"path": p, "complete": e.complete} for p, e in getattr(ledger, "files_inspected", {}).items()],
        "commands_run": [{"tool": c.tool, "args": c.args} for c in getattr(ledger, "commands_run", [])],
        "findings": [finding],
        "uncertainties": [],
        "caveats": ["Answered by deterministic fast path from runtime-owned evidence."],
        "escalation_recommendation": "GPT-5.5 review recommended",
        "missing_fields": [],
    }
    coverage = objective_coverage_errors(mission, report, ledger)
    if coverage:
        report["status"] = "PARTIAL"
        report["uncertainties"] = list(coverage)
        report["caveats"].append("Objective coverage was incomplete for a COMPLETE response; downgraded to PARTIAL.")
    validation = validate_report(report, ledger)
    if not validation.is_valid:
        report["status"] = "PARTIAL"
        report["uncertainties"] = list(report.get("uncertainties", [])) + list(validation.errors)
    return {"status": report["status"], "report": report, "fast_path": spec.objective_type}

