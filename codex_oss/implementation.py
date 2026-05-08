"""A4/A5 patch-mediated implementation pipeline.

OSS models may propose patches. The runtime validates, applies in isolation,
and verifies. The model never receives raw write tools in this lane.
"""

from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from codex_oss.runtime import resolve_path
from codex_oss.runtime.loop import _extract_model_text
from codex_oss.runtime.policy import is_critical_path, scan_secrets


JSON = dict[str, Any]

DEFAULT_DENY_ROOTS = (
    ".env",
    "secrets/",
    ".git/",
    ".codex-oss/env/",
    ".codex-oss/state/",
    ".codex-oss/logs/",
    "node_modules/",
)


def run_implementation_mission(
    mission: Any,
    raw_model_alias: str,
    handoff: str,
    call_model: Callable[[Any, Any, float], JSON],
    timeout: float,
    project_root: str,
) -> JSON:
    """Run A4/A5 patch-mediated implementation without raw write tools."""
    mission.runtime_model_alias = raw_model_alias
    mission.model_repair_count = 0
    _write_mission_artifact(mission, project_root)
    proposal, parse_error = _proposal_from_handoff(handoff)
    if proposal is None:
        desired_state, _ = _desired_state_from_handoff(handoff)
        if desired_state is not None:
            proposal, parse_error = _proposal_from_desired_state(desired_state, mission, project_root)
    if proposal is None:
        recipe, _ = _patch_recipe_from_handoff(handoff)
        if recipe is not None:
            proposal, parse_error = _proposal_from_recipe(recipe, mission, project_root)
    if proposal is None:
        proposal, parse_error = _proposal_from_model(mission, call_model, timeout, project_root)
    if proposal is None:
        report = {
            "status": "FAILED",
            "patch_validation_version": "1.0",
            "changed_files": [],
            "checks": {},
            "reasons": [parse_error or "PatchProposalV1 was not returned"],
        }
        return {
            "status": "FAILED",
            "text": render_patch_validation_report(mission, report),
        }

    if mission.tier == "A4":
        validation = validate_patch_proposal(proposal, mission, project_root)
        if validation.get("status") == "INVALID":
            mission.model_repair_count = int(getattr(mission, "model_repair_count", 0) or 0) + 1
            repaired, _ = _repair_patch_proposal(mission, proposal, validation, call_model, timeout, project_root)
            if repaired is not None:
                repaired_validation = validate_patch_proposal(repaired, mission, project_root)
                if repaired_validation.get("status") != "INVALID":
                    proposal = repaired
                    validation = repaired_validation
        mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
        artifact_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
        os.makedirs(artifact_dir, exist_ok=True)
        patch_path, rollback_path = _write_patch_artifacts(artifact_dir, proposal)
        _write_json(os.path.join(artifact_dir, "validation.json"), validation)
        _write_json(os.path.join(artifact_dir, "verification.json"), [])
        report = _implementation_report(
            status="VALIDATED" if validation.get("status") == "VALID" else (
                "ESCALATE" if validation.get("status") == "ESCALATE" else "FAILED"
            ),
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=["Patch proposal was validated but not applied because A4 is non-mutating."],
            execution_mode="proposal_only",
        )
        _persist_implementation_runtime_artifacts(
            artifact_dir=artifact_dir,
            mission=mission,
            proposal=proposal,
            validation=validation,
            verification=[],
            report=report,
            certification=None,
        )
        return {
            "status": validation.get("status", "INVALID"),
            "text": render_patch_validation_report(mission, validation),
        }

    validation = validate_patch_proposal(proposal, mission, project_root)
    if validation.get("status") == "INVALID":
        mission.model_repair_count = int(getattr(mission, "model_repair_count", 0) or 0) + 1
        repaired, _ = _repair_patch_proposal(mission, proposal, validation, call_model, timeout, project_root)
        if repaired is not None:
            repaired_validation = validate_patch_proposal(repaired, mission, project_root)
            if repaired_validation.get("status") != "INVALID":
                proposal = repaired
    apply_mode = getattr(mission, "apply_mode", "isolated_worktree")
    certification = None
    workspace_policy = getattr(mission, "workspace_apply_policy", {}) or {}
    if apply_mode == "critical_workspace_certified" or bool(workspace_policy.get("certification_required")):
        certification = _run_workspace_certification(
            mission=mission,
            proposal=proposal,
            validation=validate_patch_proposal(proposal, mission, project_root),
            call_model=call_model,
            timeout=timeout,
            project_root=project_root,
        )
    if apply_mode in (
        "workspace",
        "workspace_low_risk",
        "workspace_explicit",
        "critical_workspace_certified",
    ):
        implementation = apply_patch_in_workspace(proposal, mission, project_root, certification=certification)
    elif apply_mode == "temp_project":
        implementation = apply_patch_in_temp_project(proposal, mission, project_root)
    else:
        implementation = apply_patch_in_isolated_worktree(proposal, mission, project_root)
    return {
        "status": implementation.get("status", "FAILED"),
        "text": render_implementation_report(mission, implementation),
    }


def render_patch_validation_report(mission: Any, report: JSON) -> str:
    changed = report.get("changed_files", []) or []
    reasons = report.get("reasons", []) or []
    checks = report.get("checks", {}) or {}
    lines = [
        "OSS_PATCH_VALIDATION_BEGIN",
        f"Mission: {mission.mission_id}",
        f"Tier: {mission.tier}",
        f"Status: {report.get('status', 'INVALID')}",
        f"Changed files: {', '.join(changed) if changed else 'none'}",
        f"Proposal source: {report.get('proposal_source', 'unknown')}",
        f"Runtime built diff: {str(bool(report.get('runtime_built_diff', False))).lower()}",
        f"Model repair count: {report.get('model_repair_count', 0)}",
        "GPT review required: true",
    ]
    if reasons:
        lines.append("Reasons:")
        lines.extend(f"- {reason}" for reason in reasons)
    if checks:
        lines.append("Checks:")
        for key in sorted(checks):
            lines.append(f"- {key}: {checks[key]}")
    lines.append("OSS_PATCH_VALIDATION_JSON:")
    lines.append(json.dumps(report, indent=2, sort_keys=True))
    lines.append("OSS_PATCH_VALIDATION_END")
    return "\n".join(lines)


def render_implementation_report(mission: Any, report: JSON) -> str:
    changed = report.get("changed_files", []) or []
    verification = report.get("verification", []) or []
    caveats = report.get("caveats", []) or []
    lines = [
        "OSS_IMPLEMENTATION_REPORT_BEGIN",
        f"Mission: {mission.mission_id}",
        f"Tier: {mission.tier}",
        f"Status: {report.get('status', 'FAILED')}",
        f"Changed files: {', '.join(changed) if changed else 'none'}",
        f"Patch artifact: {report.get('patch_artifact', '')}",
        f"Main workspace mutated: {str(bool(report.get('main_workspace_mutated', False))).lower()}",
        f"Proposal source: {report.get('proposal_source', 'unknown')}",
        f"Runtime built diff: {str(bool(report.get('runtime_built_diff', False))).lower()}",
        f"Model repair count: {report.get('model_repair_count', 0)}",
        "GPT review required: true",
    ]
    if verification:
        lines.append("Verification:")
        for item in verification:
            command = item.get("command", [])
            lines.append(f"- {' '.join(command)} -> exit {item.get('exit_code')}")
    if caveats:
        lines.append("Caveats:")
        lines.extend(f"- {caveat}" for caveat in caveats)
    lines.append("OSS_IMPLEMENTATION_REPORT_JSON:")
    lines.append(json.dumps(report, indent=2, sort_keys=True))
    lines.append("OSS_IMPLEMENTATION_REPORT_END")
    return "\n".join(lines)


def _persist_implementation_runtime_artifacts(
    artifact_dir: str,
    mission: Any,
    proposal: JSON,
    validation: JSON,
    verification: list[JSON],
    report: JSON,
    certification: JSON | None = None,
) -> None:
    _write_json(os.path.join(artifact_dir, "report.json"), report)
    _write_json(
        os.path.join(artifact_dir, "ledger.json"),
        _implementation_ledger(mission, proposal, validation, verification, report, certification),
    )
    _write_text(
        os.path.join(artifact_dir, "trace.jsonl"),
        _implementation_trace_jsonl(mission, proposal, validation, verification, report, certification),
    )
    _write_text(
        os.path.join(artifact_dir, "summary.md"),
        _implementation_summary_md(mission, proposal, validation, verification, report, certification),
    )


def _write_patch_artifacts(artifact_dir: str, proposal: JSON) -> tuple[str, str]:
    patch_path = os.path.join(artifact_dir, "patch.diff")
    rollback_path = os.path.join(artifact_dir, "rollback.diff")
    diff = str(proposal.get("unified_diff", "") or "")
    _write_text(patch_path, diff)
    _write_text(rollback_path, diff)
    return patch_path, rollback_path


def _implementation_ledger(
    mission: Any,
    proposal: JSON,
    validation: JSON,
    verification: list[JSON],
    report: JSON,
    certification: JSON | None = None,
) -> JSON:
    return {
        "implementation_ledger_version": "1.0",
        "mission_id": getattr(mission, "mission_id", ""),
        "tier": getattr(mission, "tier", ""),
        "objective": getattr(mission, "objective", ""),
        "apply_mode": getattr(mission, "apply_mode", ""),
        "proposal_source": str(report.get("proposal_source", "") or proposal.get("proposal_source", "") or ""),
        "runtime_built_diff": bool(report.get("runtime_built_diff", False)),
        "changed_files": list(report.get("changed_files", []) or []),
        "validation_status": str(validation.get("status", "") or ""),
        "report_status": str(report.get("status", "") or ""),
        "semantic_review_score": int(report.get("semantic_review_score", 0) or 0),
        "verification_plan_score": int(report.get("verification_plan_score", 0) or 0),
        "verification_commands": [item.get("command", []) for item in verification if isinstance(item, dict)],
        "verification_exit_codes": [item.get("exit_code") for item in verification if isinstance(item, dict)],
        "certification_status": str((certification or {}).get("status", "") or ""),
        "main_workspace_mutated": bool(report.get("main_workspace_mutated", False)),
        "rollback_artifact": str(((report.get("rollback") or {}) if isinstance(report.get("rollback"), dict) else {}).get("artifact", "") or ""),
        "gpt_review_required": bool(report.get("gpt_review_required", True)),
    }


def _implementation_trace_jsonl(
    mission: Any,
    proposal: JSON,
    validation: JSON,
    verification: list[JSON],
    report: JSON,
    certification: JSON | None = None,
) -> str:
    events: list[JSON] = [
        {
            "step": 1,
            "phase": "proposal",
            "status": "captured",
            "mission_id": getattr(mission, "mission_id", ""),
            "proposal_source": str(report.get("proposal_source", "") or proposal.get("proposal_source", "") or ""),
            "runtime_built_diff": bool(report.get("runtime_built_diff", False)),
            "changed_files": list(report.get("changed_files", []) or []),
        },
        {
            "step": 2,
            "phase": "validation",
            "status": str(validation.get("status", "") or ""),
            "semantic_review_ok": bool(validation.get("checks", {}).get("semantic_review_ok", False)),
            "semantic_review_score": int(validation.get("checks", {}).get("semantic_review_score", 0) or 0),
            "verification_plan_ok": bool(validation.get("checks", {}).get("verification_plan_ok", False)),
            "verification_plan_score": int(validation.get("checks", {}).get("verification_plan_score", 0) or 0),
            "reasons": list(validation.get("reasons", []) or []),
        },
    ]
    if certification is not None:
        events.append(
            {
                "step": len(events) + 1,
                "phase": "certification",
                "status": str(certification.get("status", "") or ""),
                "approved_for_workspace_apply": bool(certification.get("approved_for_workspace_apply", False)),
                "approved_count": int(certification.get("approved_count", 0) or 0),
                "min_reviewer_approvals": int(certification.get("min_reviewer_approvals", 0) or 0),
            }
        )
    events.append(
        {
            "step": len(events) + 1,
            "phase": "verification",
            "status": "completed" if verification else "skipped",
            "command_count": len(verification),
            "all_green": bool(verification) and all(
                isinstance(item, dict) and item.get("exit_code") == 0 for item in verification
            ),
        }
    )
    events.append(
        {
            "step": len(events) + 1,
            "phase": "report",
            "status": str(report.get("status", "") or ""),
            "main_workspace_mutated": bool(report.get("main_workspace_mutated", False)),
            "execution_mode": str(report.get("execution_mode", "") or ""),
        }
    )
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


def _implementation_summary_md(
    mission: Any,
    proposal: JSON,
    validation: JSON,
    verification: list[JSON],
    report: JSON,
    certification: JSON | None = None,
) -> str:
    lines = [
        "# Implementation Mission Summary",
        "",
        f"- Mission ID: {getattr(mission, 'mission_id', '')}",
        f"- Tier: {getattr(mission, 'tier', '')}",
        f"- Objective: {getattr(mission, 'objective', '')}",
        f"- Apply Mode: {getattr(mission, 'apply_mode', '')}",
        f"- Proposal Source: {str(report.get('proposal_source', '') or proposal.get('proposal_source', '') or '')}",
        f"- Runtime Built Diff: {str(bool(report.get('runtime_built_diff', False))).lower()}",
        f"- Validation Status: {str(validation.get('status', '') or '')}",
        f"- Report Status: {str(report.get('status', '') or '')}",
        f"- Changed Files: {', '.join(report.get('changed_files', []) or []) or 'none'}",
        f"- Semantic Review Score: {int(report.get('semantic_review_score', 0) or 0)}",
        f"- Verification Plan Score: {int(report.get('verification_plan_score', 0) or 0)}",
        f"- Execution Mode: {str(report.get('execution_mode', '') or '')}",
        f"- Main Workspace Mutated: {str(bool(report.get('main_workspace_mutated', False))).lower()}",
        f"- Rollback Artifact: {str(((report.get('rollback') or {}) if isinstance(report.get('rollback'), dict) else {}).get('artifact', '') or '')}",
        f"- GPT Review Required: {str(bool(report.get('gpt_review_required', True))).lower()}",
    ]
    if certification is not None:
        lines.extend(
            [
                f"- Certification Status: {str(certification.get('status', '') or '')}",
                f"- Approved For Workspace Apply: {str(bool(certification.get('approved_for_workspace_apply', False))).lower()}",
            ]
        )
    if verification:
        lines.extend(["", "## Verification"])
        for item in verification:
            if not isinstance(item, dict):
                continue
            command = " ".join(item.get("command", []) or [])
            lines.append(f"- `{command}` -> exit {item.get('exit_code')}")
    reasons = list(validation.get("reasons", []) or [])
    if reasons:
        lines.extend(["", "## Validation Notes"])
        lines.extend(f"- {reason}" for reason in reasons)
    caveats = list(report.get("caveats", []) or [])
    if caveats:
        lines.extend(["", "## Caveats"])
        lines.extend(f"- {caveat}" for caveat in caveats)
    return "\n".join(lines) + "\n"


def _proposal_from_handoff(handoff: str) -> tuple[JSON | None, str | None]:
    block = _extract_optional_block(handoff, "OSS_PATCH_PROPOSAL_JSON")
    if block is None:
        return None, None
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        return None, f"invalid OSS_PATCH_PROPOSAL_JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "OSS_PATCH_PROPOSAL_JSON must contain a JSON object"
    return parsed, None


def _desired_state_from_handoff(handoff: str) -> tuple[JSON | None, str | None]:
    block = _extract_optional_block(handoff, "OSS_DESIRED_STATE_JSON")
    if block is None:
        return None, None
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        return None, f"invalid OSS_DESIRED_STATE_JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "OSS_DESIRED_STATE_JSON must contain a JSON object"
    return parsed, None


def _patch_recipe_from_handoff(handoff: str) -> tuple[JSON | None, str | None]:
    block = _extract_optional_block(handoff, "OSS_PATCH_RECIPE_JSON")
    if block is None:
        return None, None
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        return None, f"invalid OSS_PATCH_RECIPE_JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "OSS_PATCH_RECIPE_JSON must contain a JSON object"
    return parsed, None


def _proposal_from_model(
    mission: Any,
    call_model: Callable[[Any, Any, float], JSON],
    timeout: float,
    project_root: str,
) -> tuple[JSON | None, str | None]:
    context = _build_implementation_context(mission)
    prompt = (
        _desired_state_contract_text()
        + "\n\n"
        + _patch_recipe_contract_text()
        + "\n\n"
        + _patch_intent_contract_text()
        + "\n\n"
        f"Mission id: {mission.mission_id}\n"
        f"Tier: {mission.tier}\n"
        f"Objective: {mission.objective}\n"
        f"Owned paths: {getattr(mission, 'owned_paths', [])}\n"
        f"Read-only paths: {getattr(mission, 'read_only_paths', [])}\n"
        f"Max files changed: {getattr(mission, 'max_files_changed', 1)}\n"
        f"Max patch bytes: {getattr(mission, 'max_patch_bytes', 12000)}\n"
        f"Verification policy: {json.dumps(getattr(mission, 'verification_policy', {}) or {}, sort_keys=True)}\n"
        "\nAllowed file context and base hashes:\n"
        f"{context}\n"
    )
    try:
        response = call_model(
            [
                {"role": "system", "content": "You are an OSS patch proposal engine. You cannot write files."},
                {"role": "user", "content": prompt},
            ],
            [],
            timeout,
        )
    except Exception as exc:
        return None, f"patch intent model call failed: {exc}"
    text = _extract_model_text(response)
    desired_state, desired_error = _parse_desired_state_text(text)
    if desired_state is not None:
        proposal, error = _proposal_from_desired_state(desired_state, mission, project_root)
        if proposal is not None:
            return proposal, None
        return None, error or desired_error
    recipe, recipe_error = _parse_patch_recipe_text(text)
    if recipe is not None:
        proposal, error = _proposal_from_recipe(recipe, mission, project_root)
        if proposal is not None:
            return proposal, None
        return None, error or recipe_error
    intent, intent_error = _parse_patch_intent_text(text)
    if intent is not None:
        try:
            return build_patch_proposal_from_intent(intent, mission, project_root), None
        except ValueError as exc:
            build_error = f"PatchIntentV1 build failed: {exc}"
            repaired, repair_error = _repair_patch_intent_build_failure(
                mission,
                intent,
                str(exc),
                call_model,
                timeout,
                project_root,
            )
            if repaired is not None:
                return repaired, None
            return None, f"{build_error}; repair_error={repair_error or 'none'}"
    proposal, proposal_error = _parse_patch_proposal_text(text)
    if proposal is not None:
        return proposal, None
    if desired_error and "DesiredStateV1 JSON parse failed" in desired_error:
        repaired, repair_error = _repair_desired_state_parse_failure(
            mission,
            text,
            desired_error,
            call_model,
            timeout,
            project_root,
        )
        if repaired is not None:
            return repaired, None
        return None, f"{desired_error}; repair_error={repair_error or 'none'}"
    return None, desired_error or recipe_error or intent_error or proposal_error


def _proposal_from_desired_state(desired_state: JSON, mission: Any, project_root: str) -> tuple[JSON | None, str | None]:
    try:
        return build_patch_proposal_from_desired_state(desired_state, mission, project_root), None
    except ValueError as exc:
        return None, f"DesiredStateV1 build failed: {exc}"


def _proposal_from_recipe(recipe: JSON, mission: Any, project_root: str) -> tuple[JSON | None, str | None]:
    try:
        return build_patch_proposal_from_recipe(recipe, mission, project_root), None
    except ValueError as exc:
        return None, f"PatchRecipeV1 build failed: {exc}"


def _repair_patch_proposal(
    mission: Any,
    proposal: JSON,
    validation: JSON,
    call_model: Callable[[Any, Any, float], JSON],
    timeout: float,
    project_root: str,
) -> tuple[JSON | None, str | None]:
    context = _build_implementation_context(mission)
    prompt = (
        _patch_intent_contract_text()
        + "\n\nThe previous patch attempt failed runtime validation. "
        "Return a corrected PatchIntentV1 JSON object only. Do not explain.\n\n"
        f"Validation report: {json.dumps(validation, sort_keys=True)}\n\n"
        f"Previous proposal: {json.dumps(proposal, sort_keys=True)}\n\n"
        f"Allowed file context and base hashes:\n{context}\n"
    )
    try:
        response = call_model(
            [
                {"role": "system", "content": "You repair invalid patch proposal JSON. You cannot write files."},
                {"role": "user", "content": prompt},
            ],
            [],
            min(timeout, 60.0),
        )
    except Exception as exc:
        return None, f"patch proposal repair call failed: {exc}"
    text = _extract_model_text(response)
    intent, intent_error = _parse_patch_intent_text(text)
    if intent is None:
        return _parse_patch_proposal_text(text)
    try:
        return build_patch_proposal_from_intent(intent, mission, project_root), None
    except ValueError as exc:
        return None, f"PatchIntentV1 repair build failed: {exc}; parse_error={intent_error or ''}"


def _repair_desired_state_parse_failure(
    mission: Any,
    raw_text: str,
    error: str,
    call_model: Callable[[Any, Any, float], JSON],
    timeout: float,
    project_root: str,
) -> tuple[JSON | None, str | None]:
    prompt = (
        _desired_state_contract_text()
        + "\n\nThe previous DesiredStateV1 response was not valid JSON. "
        "Return one corrected DesiredStateV1 JSON object only. Do not use markdown. "
        "Do not change the requested file, function name, or return value.\n\n"
        f"Parse error: {error}\n\n"
        f"Previous response: {raw_text[:4000]}\n"
    )
    try:
        response = call_model(
            [
                {"role": "system", "content": "You repair malformed DesiredStateV1 JSON. You cannot write files."},
                {"role": "user", "content": prompt},
            ],
            [],
            min(timeout, 60.0),
        )
    except Exception as exc:
        return None, f"DesiredStateV1 repair call failed: {exc}"
    repaired_state, parse_error = _parse_desired_state_text(_extract_model_text(response))
    if repaired_state is None:
        return None, parse_error
    return _proposal_from_desired_state(repaired_state, mission, project_root)


def _repair_patch_intent_build_failure(
    mission: Any,
    intent: JSON,
    error: str,
    call_model: Callable[[Any, Any, float], JSON],
    timeout: float,
    project_root: str,
) -> tuple[JSON | None, str | None]:
    context = _build_implementation_context(mission)
    prompt = (
        _patch_intent_contract_text()
        + "\n\nThe previous PatchIntentV1 could not be turned into a patch. "
        "Return a corrected PatchIntentV1 JSON object only. Do not explain. "
        "The corrected intent must change file content; do not return old_text equal to new_text, "
        "and do not repeat the same no-op edit.\n\n"
        f"Build error: {error}\n\n"
        f"Previous intent: {json.dumps(intent, sort_keys=True)}\n\n"
        f"Allowed file context and base hashes:\n{context}\n"
    )
    try:
        response = call_model(
            [
                {"role": "system", "content": "You repair invalid patch edit intents. You cannot write files."},
                {"role": "user", "content": prompt},
            ],
            [],
            min(timeout, 60.0),
        )
    except Exception as exc:
        return None, f"PatchIntentV1 repair call failed: {exc}"
    repaired_intent, parse_error = _parse_patch_intent_text(_extract_model_text(response))
    if repaired_intent is None:
        proposal, proposal_error = _parse_patch_proposal_text(_extract_model_text(response))
        return proposal, parse_error or proposal_error
    try:
        return build_patch_proposal_from_intent(repaired_intent, mission, project_root), None
    except ValueError as exc:
        return None, f"PatchIntentV1 repair build failed: {exc}"


def evaluate_desired_state(desired_state: JSON, mission: Any, project_root: str) -> JSON:
    """Evaluate whether a DesiredStateV1 assertion already holds."""
    if not isinstance(desired_state, dict) or desired_state.get("desired_state_version") != "1.0":
        raise ValueError("DesiredStateV1 object with desired_state_version=1.0 is required")
    assertions = desired_state.get("assertions", [])
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("DesiredStateV1 requires at least one assertion")
    results = [_evaluate_desired_assertion(assertion, mission, project_root) for assertion in assertions]
    if all(result["satisfied"] for result in results):
        status = "ALREADY_SATISFIED"
    elif any(result["satisfied"] for result in results):
        status = "PARTIAL"
    else:
        status = "MISSING"
    return {
        "desired_state_version": "1.0",
        "status": status,
        "assertions": results,
    }


def build_patch_proposal_from_desired_state(desired_state: JSON, mission: Any, project_root: str) -> JSON:
    """Build a PatchProposalV1 from simple DesiredStateV1 assertions."""
    evaluation = evaluate_desired_state(desired_state, mission, project_root)
    if evaluation["status"] == "ALREADY_SATISFIED":
        raise ValueError("desired state already satisfied")
    edits = []
    for assertion, result in zip(desired_state.get("assertions", []), evaluation["assertions"]):
        if result["satisfied"]:
            continue
        edits.append(_desired_assertion_to_edit(assertion, mission, project_root))
    intent = {
        "patch_intent_version": "1.0",
        "summary": str(desired_state.get("summary", "") or "Runtime-built patch from DesiredStateV1"),
        "edits": edits,
        "risk_assessment": _as_dict(desired_state.get("risk_assessment"), {
            "risk_tier": getattr(mission, "risk_tier", "low"),
            "critical_paths_touched": False,
            "blast_radius": "bounded desired state",
        }),
        "verification_plan": _as_list(desired_state.get("verification_plan")),
        "evidence_refs": _as_list(desired_state.get("evidence_refs")),
        "caveats": _as_list(desired_state.get("caveats")),
    }
    proposal = build_patch_proposal_from_intent(intent, mission, project_root)
    proposal["proposal_source"] = "desired_state_v1"
    return proposal


def build_patch_proposal_from_recipe(recipe: JSON, mission: Any, project_root: str) -> JSON:
    """Build a PatchProposalV1 from constrained PatchRecipeV1 slot fills."""
    if not isinstance(recipe, dict) or recipe.get("patch_recipe_version") != "1.0":
        raise ValueError("PatchRecipeV1 object with patch_recipe_version=1.0 is required")
    entries = recipe.get("recipes")
    if entries is None:
        entries = recipe.get("recipe_entries")
    if entries is None:
        entries = [recipe]
    if not isinstance(entries, list) or not entries:
        raise ValueError("PatchRecipeV1 requires at least one recipe entry")

    edits = [_recipe_entry_to_edit(entry, mission, project_root) for entry in entries]
    intent = {
        "patch_intent_version": "1.0",
        "summary": str(recipe.get("summary", "") or "Runtime-built patch from PatchRecipeV1"),
        "edits": edits,
        "risk_assessment": _as_dict(recipe.get("risk_assessment"), {
            "risk_tier": getattr(mission, "risk_tier", "low"),
            "critical_paths_touched": False,
            "blast_radius": "bounded recipe edit",
        }),
        "verification_plan": _as_list(recipe.get("verification_plan")),
        "evidence_refs": _as_list(recipe.get("evidence_refs")),
        "caveats": _as_list(recipe.get("caveats")),
    }
    proposal = build_patch_proposal_from_intent(intent, mission, project_root)
    proposal["proposal_source"] = "patch_recipe_v1"
    return proposal


def _recipe_entry_to_edit(entry: Any, mission: Any, project_root: str) -> JSON:
    if not isinstance(entry, dict):
        raise ValueError("PatchRecipeV1 entries must be objects")
    recipe_type = str(entry.get("recipe_type") or entry.get("type") or "").strip()
    if recipe_type != "insert_block_at_anchor":
        raise ValueError(f"unsupported PatchRecipeV1 recipe_type: {recipe_type}")
    path = str(
        entry.get("target_file")
        or entry.get("path")
        or entry.get("file_path")
        or entry.get("target_path")
        or ""
    ).strip()
    if not path:
        raise ValueError("PatchRecipeV1 insert_block_at_anchor requires target_file")
    if not _path_allowed(path, mission) or _path_has_escape(path, project_root):
        raise ValueError(f"recipe path is not owned or safe: {path}")
    content = str(
        entry.get("content")
        or entry.get("block")
        or entry.get("content_block")
        or entry.get("insert_text")
        or entry.get("body")
        or ""
    )
    if not content:
        raise ValueError("PatchRecipeV1 insert_block_at_anchor requires content")
    anchor_id = str(
        entry.get("selected_anchor_id")
        or entry.get("anchor_id")
        or entry.get("anchor")
        or "anchor:eof"
    ).strip()
    base_content = _read_text_file(project_root, path)
    if anchor_id in ("anchor:eof", "eof", "end_of_file", "anchor:end_of_file"):
        return {
            "operation": "append_to_file",
            "path": path,
            "content": content,
            "reason": f"PatchRecipeV1 insert at {anchor_id}",
        }
    anchor_text = _anchor_text_for_id(base_content, anchor_id)
    if not anchor_text:
        candidates = ", ".join(candidate["id"] for candidate in _anchor_candidates(base_content))
        raise ValueError(f"unknown anchor id {anchor_id}; available anchors: {candidates}")
    return {
        "operation": "insert_after",
        "path": path,
        "anchor": anchor_text,
        "content": content,
        "reason": f"PatchRecipeV1 insert after {anchor_id}",
    }


def _anchor_candidates(content: str) -> list[JSON]:
    candidates = [{"id": "anchor:eof", "description": "End of file"}]
    for match in re.finditer(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content, re.MULTILINE):
        name = match.group(1)
        block = _python_function_block(content[match.start():], name)
        if block:
            candidates.append({
                "id": f"anchor:def_{name}",
                "description": f"After function {name}",
                "text": block,
            })
    return candidates


def _anchor_text_for_id(content: str, anchor_id: str) -> str | None:
    for candidate in _anchor_candidates(content):
        if candidate["id"] == anchor_id:
            return candidate.get("text")
    return None


def _evaluate_desired_assertion(assertion: Any, mission: Any, project_root: str) -> JSON:
    if not isinstance(assertion, dict):
        raise ValueError("DesiredStateV1 assertions must be objects")
    kind = _desired_assertion_kind(assertion)
    if kind not in ("python_function_exists", "python_unittest_method_exists"):
        raise ValueError(f"unsupported desired state assertion type: {kind}")
    path = str(assertion.get("path", "") or "").strip()
    function_name = str(assertion.get("function_name", "") or "").strip()
    method_name = str(assertion.get("method_name", "") or "").strip()
    class_name = str(assertion.get("class_name", "") or "").strip()
    if kind == "python_function_exists" and (not path or not function_name):
        raise ValueError("python_function_exists requires path and function_name")
    if kind == "python_unittest_method_exists" and (not path or not class_name or not method_name):
        raise ValueError("python_unittest_method_exists requires path, class_name, and method_name")
    if not _path_allowed(path, mission) or _path_has_escape(path, project_root):
        raise ValueError(f"desired state path is not owned or safe: {path}")
    content = _read_text_file(project_root, path)
    if kind == "python_unittest_method_exists":
        function_block = _python_method_block_in_class(content, class_name, method_name)
        symbol_name = method_name
    else:
        function_block = _python_function_block(content, function_name)
        symbol_name = function_name
    body_contains = str(assertion.get("body_contains", "") or "")
    body = str(assertion.get("body", "") or "")
    satisfied = function_block is not None
    if body_contains:
        satisfied = bool(satisfied and body_contains in function_block)
    if body:
        satisfied = bool(satisfied and body.strip() in function_block.strip())
    return {
        "type": kind,
        "path": path,
        "function_name": symbol_name,
        "class_name": class_name,
        "satisfied": bool(satisfied),
    }


def _desired_assertion_to_edit(assertion: JSON, mission: Any, project_root: str) -> JSON:
    kind = _desired_assertion_kind(assertion)
    if kind == "python_unittest_method_exists":
        return _unittest_method_assertion_to_edit(assertion, mission, project_root)
    if kind != "python_function_exists":
        raise ValueError(f"unsupported desired state assertion type: {kind}")
    path = str(assertion.get("path", "") or "").strip()
    function_name = str(assertion.get("function_name", "") or "").strip()
    body = str(assertion.get("body", "") or "")
    return_value = assertion.get("return_value")
    if not body:
        if return_value is not None:
            body = f"def {function_name}():\n    return {json.dumps(return_value)}\n"
        else:
            body = f"def {function_name}():\n    pass\n"
    if not body.endswith("\n"):
        body += "\n"
    return {
        "operation": "append_to_file",
        "path": path,
        "content": "\n\n" + body,
        "reason": f"Ensure python function exists: {function_name}",
    }


def _desired_assertion_kind(assertion: JSON) -> str:
    kind = str(assertion.get("type") or assertion.get("assertion_type") or assertion.get("kind") or "").strip()
    if kind:
        return kind
    if assertion.get("class_name") and (assertion.get("method_name") or assertion.get("test_name")):
        if assertion.get("method_name") in (None, "") and assertion.get("test_name"):
            assertion["method_name"] = assertion.get("test_name")
        return "python_unittest_method_exists"
    if assertion.get("function_name"):
        return "python_function_exists"
    return ""


def _unittest_method_assertion_to_edit(assertion: JSON, mission: Any, project_root: str) -> JSON:
    path = str(assertion.get("path", "") or "").strip()
    class_name = str(assertion.get("class_name", "") or "").strip()
    method_name = str(assertion.get("method_name", "") or "").strip()
    content = _read_text_file(project_root, path)
    class_span = _python_class_span(content, class_name)
    if class_span is None:
        raise ValueError(f"class not found for python_unittest_method_exists: {class_name}")
    class_block = content[class_span[0]:class_span[1]]
    anchor = _last_python_method_block(class_block)
    if not anchor:
        class_header = class_block.splitlines(keepends=True)[0]
        anchor = class_header
    method_text = _unittest_method_text(assertion, method_name)
    return {
        "operation": "insert_after",
        "path": path,
        "anchor": anchor,
        "content": "\n" + method_text,
        "reason": f"Ensure unittest method exists: {class_name}.{method_name}",
    }


def _unittest_method_text(assertion: JSON, method_name: str) -> str:
    body = str(assertion.get("body", "") or "")
    if body:
        text = body
        if text.lstrip().startswith("def "):
            text = _indent_block(text, "    ")
        elif not text.startswith("    def "):
            text = f"    def {method_name}(self):\n" + _indent_block(text, "        ")
    else:
        body_lines = assertion.get("body_lines")
        if isinstance(body_lines, list) and body_lines:
            body_text = "\n".join(str(line) for line in body_lines)
        else:
            body_text = "pass"
        text = f"    def {method_name}(self):\n" + _indent_block(body_text, "        ")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _indent_block(text: str, prefix: str) -> str:
    lines = text.splitlines()
    return "\n".join(prefix + line if line else prefix.rstrip() for line in lines) + "\n"


def _read_text_file(project_root: str, path: str) -> str:
    with open(os.path.join(project_root, path), "r", encoding="utf-8") as handle:
        return handle.read()


def _as_dict(value: Any, default: JSON) -> JSON:
    return dict(value) if isinstance(value, dict) else dict(default)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _python_function_block(content: str, function_name: str) -> str | None:
    pattern = re.compile(rf"^def\s+{re.escape(function_name)}\s*\(", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    lines = content[match.start():].splitlines(keepends=True)
    block = []
    for idx, line in enumerate(lines):
        if idx > 0 and line.startswith("def "):
            break
        block.append(line)
    return "".join(block)


def _python_class_span(content: str, class_name: str) -> tuple[int, int] | None:
    pattern = re.compile(rf"^class\s+{re.escape(class_name)}\b[^\n]*:\n?", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    end = len(content)
    for next_match in re.finditer(r"^(class|def)\s+[A-Za-z_][A-Za-z0-9_]*\b", content[match.end():], re.MULTILINE):
        end = match.end() + next_match.start()
        break
    return match.start(), end


def _python_method_block_in_class(content: str, class_name: str, method_name: str) -> str | None:
    span = _python_class_span(content, class_name)
    if span is None:
        return None
    class_block = content[span[0]:span[1]]
    pattern = re.compile(rf"^\s+def\s+{re.escape(method_name)}\s*\(", re.MULTILINE)
    match = pattern.search(class_block)
    if not match:
        return None
    return _python_indented_block(class_block[match.start():])


def _last_python_method_block(class_block: str) -> str | None:
    matches = list(re.finditer(r"^\s+def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", class_block, re.MULTILINE))
    if not matches:
        return None
    return _python_indented_block(class_block[matches[-1].start():])


def _python_indented_block(content: str) -> str:
    lines = content.splitlines(keepends=True)
    block = []
    base_indent = None
    for idx, line in enumerate(lines):
        if idx == 0:
            base_indent = len(line) - len(line.lstrip(" "))
            block.append(line)
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped and base_indent is not None and indent <= base_indent:
            break
        block.append(line)
    return "".join(block)


def build_patch_proposal_from_intent(intent: JSON, mission: Any, project_root: str) -> JSON:
    """Build a canonical PatchProposalV1 from typed PatchIntentV1 edit operations."""
    if not isinstance(intent, dict) or intent.get("patch_intent_version") != "1.0":
        raise ValueError("PatchIntentV1 object with patch_intent_version=1.0 is required")
    edits = intent.get("edits", [])
    if not isinstance(edits, list) or not edits:
        raise ValueError("PatchIntentV1 requires at least one edit")
    if len(edits) > int(getattr(mission, "max_files_changed", 1)) * 10:
        raise ValueError("too many edit operations")
    default_path = str(
        intent.get("path")
        or intent.get("file_path")
        or intent.get("target_path")
        or intent.get("target_file")
        or intent.get("filename")
        or ""
    ).strip()

    original_by_path: dict[str, str] = {}
    updated_by_path: dict[str, str] = {}
    change_type_by_path: dict[str, str] = {}
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("edit entries must be objects")
        edit = _normalize_intent_edit(edit)
        if default_path and not edit.get("path"):
            edit["path"] = default_path
        path = str(edit.get("path", "") or "").strip()
        operation = str(edit.get("operation", "") or "").strip()
        if not path:
            raise ValueError(f"edit.path is required; intent_shape={_intent_shape(intent)}")
        if not _path_allowed(path, mission) or _path_has_escape(path, project_root):
            raise ValueError(f"edit path is not owned or safe: {path}")
        if is_critical_path(path) and not bool(getattr(mission, "critical_path_write_allowed", False)):
            raise ValueError(f"critical path write requires GPT review: {path}")

        if path not in original_by_path:
            full_path = os.path.join(project_root, path)
            if operation == "create_file":
                if os.path.exists(full_path):
                    raise ValueError(f"create_file target already exists: {path}")
                original_by_path[path] = ""
                updated_by_path[path] = ""
                change_type_by_path[path] = "add"
            else:
                try:
                    with open(full_path, "r", encoding="utf-8") as handle:
                        content = handle.read()
                except (OSError, UnicodeDecodeError) as exc:
                    raise ValueError(f"cannot read edit target {path}: {exc}") from exc
                original_by_path[path] = content
                updated_by_path[path] = content
                change_type_by_path[path] = "modify"
        elif operation == "create_file":
            raise ValueError(f"create_file cannot follow another edit for {path}")

        updated_by_path[path] = _apply_intent_edit(updated_by_path[path], edit)

    for path, content in updated_by_path.items():
        _, found = scan_secrets(_added_text(original_by_path.get(path, ""), content))
        if found:
            raise ValueError(f"edit appears to introduce secret material: {path}")

    diff_parts = []
    changed_files = []
    for path in sorted(updated_by_path):
        before = original_by_path[path]
        after = updated_by_path[path]
        if before == after:
            raise ValueError(f"edit produced no change: {path}")
        diff_parts.append(_build_file_diff(path, before, after, change_type_by_path.get(path, "modify")))
        changed_files.append({
            "path": path,
            "change_type": change_type_by_path.get(path, "modify"),
            "reason": _reason_for_path(path, edits),
            "base_sha256": hashlib.sha256(before.encode()).hexdigest(),
        })

    return {
        "patch_proposal_version": "1.0",
        "status": "PROPOSED",
        "summary": str(intent.get("summary", "") or "Runtime-built patch proposal from PatchIntentV1"),
        "base": _as_dict(intent.get("base"), {"git_head": "runtime", "dirty_worktree_allowed": False}),
        "changed_files": changed_files,
        "unified_diff": "\n".join(diff_parts),
        "risk_assessment": _as_dict(intent.get("risk_assessment"), {
            "risk_tier": getattr(mission, "risk_tier", "low"),
            "critical_paths_touched": False,
            "blast_radius": "bounded owned paths",
        }),
        "verification_plan": _as_list(intent.get("verification_plan")),
        "evidence_refs": _as_list(intent.get("evidence_refs")),
        "caveats": _as_list(intent.get("caveats")),
        "proposal_source": "patch_intent_v1",
    }


def _apply_intent_edit(content: str, edit: JSON) -> str:
    operation = str(edit.get("operation", "") or "")
    if operation == "insert_after":
        anchor = str(edit.get("anchor", "") or "")
        insert = str(edit.get("content", "") or "")
        idx = _unique_index(content, anchor)
        return content[:idx + len(anchor)] + insert + content[idx + len(anchor):]
    if operation == "insert_before":
        anchor = str(edit.get("anchor", "") or "")
        insert = str(edit.get("content", "") or "")
        idx = _unique_index(content, anchor)
        return content[:idx] + insert + content[idx:]
    if operation == "replace_exact":
        old = str(edit.get("old_text", "") or "")
        new = str(edit.get("new_text", "") or "")
        idx = _unique_index(content, old)
        return content[:idx] + new + content[idx + len(old):]
    if operation == "replace_range":
        start_line = int(edit.get("start_line", 0) or 0)
        end_line = int(edit.get("end_line", 0) or 0)
        if start_line < 1 or end_line < start_line:
            raise ValueError("replace_range requires valid 1-based start_line/end_line")
        lines = content.splitlines(keepends=True)
        if end_line > len(lines):
            raise ValueError("replace_range line range exceeds file length")
        old_text = "".join(lines[start_line - 1:end_line])
        expected_hash = str(edit.get("expected_old_text_sha256", "") or "")
        if expected_hash and hashlib.sha256(old_text.encode()).hexdigest() != expected_hash:
            raise ValueError("replace_range expected_old_text_sha256 mismatch")
        new_text = str(edit.get("new_text", "") or "")
        return "".join(lines[:start_line - 1]) + new_text + "".join(lines[end_line:])
    if operation == "append_to_file":
        return content + str(edit.get("content", "") or "")
    if operation == "create_file":
        return str(edit.get("content", "") or "")
    raise ValueError(f"unsupported edit operation: {operation}")


def _normalize_intent_edit(edit: JSON) -> JSON:
    normalized = dict(edit)
    _copy_first(normalized, "path", ("file_path", "target_path", "target_file", "filename", "file"))
    _copy_first(normalized, "operation", ("op", "action", "edit_type", "type"))
    _copy_first(
        normalized,
        "content",
        (
            "insert_text",
            "text",
            "new_content",
            "append_text",
            "append_content",
            "append",
            "add_text",
            "addition",
            "content_to_add",
            "content_to_append",
            "text_to_append",
            "block",
            "append_block",
            "content_block",
            "markdown_block",
            "markdown",
            "new_block",
            "new_section",
            "section",
            "body",
            "new_text",
        ),
    )
    _copy_first(normalized, "anchor", ("anchor_text", "after", "before", "target_text"))
    _copy_first(normalized, "old_text", ("target", "find", "from_text"))
    _copy_first(normalized, "new_text", ("replacement", "replace_with", "to_text"))
    if not normalized.get("operation"):
        if normalized.get("old_text") and normalized.get("new_text"):
            normalized["operation"] = "replace_exact"
        elif normalized.get("anchor") and normalized.get("content"):
            normalized["operation"] = "insert_after"
        elif normalized.get("content"):
            normalized["operation"] = "append_to_file"
    return normalized


def _intent_shape(intent: JSON) -> JSON:
    edits = intent.get("edits", [])
    edit_shapes = []
    if isinstance(edits, list):
        for edit in edits[:5]:
            if isinstance(edit, dict):
                edit_shapes.append(sorted(str(key) for key in edit.keys()))
            else:
                edit_shapes.append(type(edit).__name__)
    return {
        "top_level_keys": sorted(str(key) for key in intent.keys()),
        "edit_shapes": edit_shapes,
    }


def _copy_first(target: JSON, canonical: str, aliases: tuple[str, ...]) -> None:
    if target.get(canonical) not in (None, ""):
        return
    for alias in aliases:
        if target.get(alias) not in (None, ""):
            target[canonical] = target[alias]
            return


def _unique_index(content: str, needle: str) -> int:
    if not needle:
        raise ValueError("anchor/old_text is required")
    count = content.count(needle)
    if count != 1:
        raise ValueError(f"anchor must match exactly once, matched {count}")
    return content.index(needle)


def _build_file_diff(path: str, before: str, after: str, change_type: str) -> str:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff_lines = list(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path}" if change_type != "add" else "/dev/null",
        tofile=f"b/{path}",
        lineterm="",
    ))
    header = [f"diff --git a/{path} b/{path}"]
    if change_type == "add":
        header.append("new file mode 100644")
    return "\n".join(header + diff_lines) + "\n"


def _added_text(before: str, after: str) -> str:
    additions = []
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            additions.append(line[2:])
    return "\n".join(additions)


def _reason_for_path(path: str, edits: list[JSON]) -> str:
    reasons = [str(edit.get("reason", "") or "") for edit in edits if edit.get("path") == path and edit.get("reason")]
    return "; ".join(reasons) or "PatchIntentV1 edit"


def _parse_patch_intent_text(text: str) -> tuple[JSON | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty patch intent response"
    block = _extract_optional_block(raw, "OSS_PATCH_INTENT_JSON")
    if block is not None:
        raw = block.strip()
    if raw.startswith("```"):
        raw = _strip_fenced_json(raw)
    if not raw.startswith("{"):
        raw = _extract_first_json_object(raw) or raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"PatchIntentV1 JSON parse failed: {exc}"
    if not isinstance(parsed, dict) or parsed.get("patch_intent_version") != "1.0":
        return None, "PatchIntentV1 response must be a JSON object with patch_intent_version=1.0"
    return parsed, None


def _parse_desired_state_text(text: str) -> tuple[JSON | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty desired state response"
    block = _extract_optional_block(raw, "OSS_DESIRED_STATE_JSON")
    if block is not None:
        raw = block.strip()
    if raw.startswith("```"):
        raw = _strip_fenced_json(raw)
    if not raw.startswith("{"):
        raw = _extract_first_json_object(raw) or raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"DesiredStateV1 JSON parse failed: {exc}"
    if not isinstance(parsed, dict) or parsed.get("desired_state_version") != "1.0":
        return None, "DesiredStateV1 response must be a JSON object with desired_state_version=1.0"
    return parsed, None


def _parse_patch_recipe_text(text: str) -> tuple[JSON | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty patch recipe response"
    block = _extract_optional_block(raw, "OSS_PATCH_RECIPE_JSON")
    if block is not None:
        raw = block.strip()
    if raw.startswith("```"):
        raw = _strip_fenced_json(raw)
    if not raw.startswith("{"):
        raw = _extract_first_json_object(raw) or raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"PatchRecipeV1 JSON parse failed: {exc}"
    if not isinstance(parsed, dict) or parsed.get("patch_recipe_version") != "1.0":
        return None, "PatchRecipeV1 response must be a JSON object with patch_recipe_version=1.0"
    return parsed, None


def _desired_state_contract_text() -> str:
    return (
        "Prefer returning exactly one DesiredStateV1 JSON object for simple changes. "
        "Do not use markdown. DesiredStateV1 must include desired_state_version='1.0', "
        "summary, assertions, risk_assessment, verification_plan, evidence_refs, and caveats. "
        "Supported assertion types: python_function_exists with path, function_name, and either "
        "body, body_contains, or return_value; python_unittest_method_exists with path, class_name, "
        "method_name, and either body, body_lines, or body_contains. Use DesiredStateV1 when the "
        "task asks to ensure a function, unittest method, block, or config value exists; the "
        "runtime will build the patch."
    )


def _patch_recipe_contract_text() -> str:
    return (
        "Use PatchRecipeV1 when the task says to select an anchor or fill an anchor slot. "
        "Return exactly one PatchRecipeV1 JSON object. Do not use markdown. "
        "PatchRecipeV1 must include patch_recipe_version='1.0', recipe_type='insert_block_at_anchor', "
        "target_file, selected_anchor_id, content, risk_assessment, verification_plan, evidence_refs, "
        "and caveats. Supported selected_anchor_id values include anchor:eof and any anchor:def_<name> "
        "listed in the allowed file context. The runtime builds the actual diff."
    )


def _patch_intent_contract_text() -> str:
    return (
        "Return exactly one PatchIntentV1 JSON object. Do not use markdown. "
        "Do not return a unified diff unless explicitly asked; the runtime builds diffs. "
        "The object must include patch_intent_version='1.0', summary, edits, risk_assessment, "
        "verification_plan, evidence_refs, and caveats. Supported edit operations are "
        "insert_before, insert_after, replace_exact, replace_range, append_to_file, and create_file. "
        "For insert_before/insert_after, anchor must match exactly once in the target file. "
        "For replace_exact, old_text must match exactly once. For replace_range, include start_line, "
        "end_line, expected_old_text_sha256 when available, and new_text. "
        "Every edit must produce a non-empty file change; never set replace_exact old_text and new_text "
        "to the same value. For simple additions at the end of a file, prefer append_to_file."
    )


def _patch_proposal_contract_text() -> str:
    return (
        "Return exactly one PatchProposalV1 JSON object. Do not use markdown. "
        "Do not claim that you applied files. The runtime validates and applies patches. "
        "The JSON must include patch_proposal_version='1.0', changed_files, unified_diff, "
        "risk_assessment, verification_plan, evidence_refs, and caveats. "
        "The unified_diff field must be a real git-style unified diff beginning with "
        "'diff --git a/<path> b/<path>' and must include ---/+++ file headers plus @@ hunks. "
        "Each changed_files item must include path, change_type, reason, and base_sha256 copied exactly "
        "from the allowed file context."
    )


def _build_implementation_context(mission: Any) -> str:
    allowed_paths = []
    for path in (
        list(getattr(mission, "read_only_paths", []) or [])
        + list(getattr(mission, "allowed_paths", []) or [])
        + list(getattr(mission, "owned_paths", []) or [])
    ):
        if path and path not in allowed_paths:
            allowed_paths.append(path)
    if not allowed_paths:
        return "No explicit file context was available."

    chunks: list[str] = []
    total_bytes = 0
    max_total = 50000
    for raw_path in allowed_paths:
        resolved, error = resolve_path(
            raw_path,
            list(getattr(mission, "allowed_roots", []) or []),
            allowed_paths,
        )
        if error or not resolved:
            chunks.append(f"[{raw_path}] skipped: {error}")
            continue
        if os.path.isdir(resolved):
            chunks.append(f"[{resolved}] skipped: directories are not embedded in A4/A5 context")
            continue
        try:
            with open(resolved, "r", encoding="utf-8") as handle:
                content = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            chunks.append(f"[{resolved}] skipped: {exc}")
            continue
        redacted, found_secret = scan_secrets(content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        remaining = max_total - total_bytes
        if remaining <= 0:
            chunks.append("[context truncated: max_total_observation_bytes reached]")
            break
        snippet = redacted[:remaining]
        total_bytes += len(snippet.encode("utf-8"))
        redaction_note = " redactions_applied=true" if found_secret else ""
        anchors = _anchor_candidates(redacted)
        anchor_lines = "\n".join(
            f"- {anchor['id']}: {anchor.get('description', '')}"
            for anchor in anchors
        )
        chunks.append(
            f"--- FILE {resolved} sha256={digest}{redaction_note} ---\n"
            f"PatchRecipeV1 anchor candidates:\n{anchor_lines}\n"
            f"{snippet}\n"
            f"--- END FILE {resolved} ---"
        )
    return "\n\n".join(chunks)


def _parse_patch_proposal_text(text: str) -> tuple[JSON | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty patch proposal response"
    block = _extract_optional_block(raw, "OSS_PATCH_PROPOSAL_JSON")
    if block is not None:
        raw = block.strip()
    if raw.startswith("```"):
        raw = _strip_fenced_json(raw)
    if not raw.startswith("{"):
        raw = _extract_first_json_object(raw) or raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"PatchProposalV1 JSON parse failed: {exc}"
    if not isinstance(parsed, dict):
        return None, "PatchProposalV1 response must be a JSON object"
    return parsed, None


def _extract_optional_block(text: str, name: str) -> str | None:
    start = f"<{name}>"
    end = f"</{name}>"
    start_idx = text.find(start)
    if start_idx < 0:
        return None
    end_idx = text.find(end, start_idx + len(start))
    if end_idx < 0:
        return None
    return text[start_idx + len(start):end_idx].strip()


def _strip_fenced_json(text: str) -> str:
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("```"):
        return text
    if lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return "\n".join(lines[1:]).strip()


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


@dataclass
class DiffFile:
    old_path: str
    new_path: str
    change_type: str = "modify"
    binary: bool = False
    mode_change: bool = False
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path


def validate_patch_proposal(proposal: JSON, mission: Any, project_root: str) -> JSON:
    """Validate PatchProposalV1 without mutating the workspace."""
    checks = {
        "unified_diff_parse": False,
        "paths_allowed": False,
        "path_escape": False,
        "critical_paths_touched": False,
        "max_files_ok": False,
        "max_patch_size_ok": False,
        "base_sha_match": False,
        "applies_cleanly": False,
        "secret_scan_ok": False,
        "binary_changes": False,
        "mode_changes": False,
        "deletions_allowed": False,
        "objective_satisfied": False,
        "semantic_review_ok": False,
        "verification_plan_ok": False,
        "semantic_review_score": 0,
        "verification_plan_score": 0,
    }
    reasons: list[str] = []

    if not isinstance(proposal, dict) or proposal.get("patch_proposal_version") != "1.0":
        reasons.append("PatchProposalV1 object with patch_proposal_version=1.0 is required")
        return _validation_report("INVALID", checks, reasons, [], proposal, mission)

    diff = str(proposal.get("unified_diff", "") or "")
    if not diff.strip():
        reasons.append("unified_diff is required")
        return _validation_report("INVALID", checks, reasons, [], proposal, mission)

    files = _parse_unified_diff(diff)
    checks["unified_diff_parse"] = bool(files)
    if not files:
        reasons.append("unified_diff could not be parsed")
        return _validation_report("INVALID", checks, reasons, [], proposal, mission)

    changed_paths = [f.path for f in files]
    checks["max_files_ok"] = len(set(changed_paths)) <= int(getattr(mission, "max_files_changed", 1))
    if not checks["max_files_ok"]:
        reasons.append("patch changes too many files")

    checks["max_patch_size_ok"] = len(diff.encode("utf-8")) <= int(getattr(mission, "max_patch_bytes", 12000))
    if not checks["max_patch_size_ok"]:
        reasons.append("patch exceeds max_patch_bytes")

    escaped = [_path_has_escape(path, project_root) for path in changed_paths]
    checks["path_escape"] = any(escaped)
    if checks["path_escape"]:
        reasons.append("patch path escapes project root")

    paths_allowed = all(_path_allowed(path, mission) for path in changed_paths)
    checks["paths_allowed"] = paths_allowed and not checks["path_escape"]
    if not checks["paths_allowed"]:
        reasons.append("patch touches paths outside owned_paths or default policy")

    critical = [path for path in changed_paths if is_critical_path(path)]
    checks["critical_paths_touched"] = bool(critical)
    if critical and not bool(getattr(mission, "critical_path_write_allowed", False)):
        reasons.append(f"critical path write requires GPT review: {', '.join(critical)}")

    checks["binary_changes"] = any(f.binary for f in files)
    if checks["binary_changes"]:
        reasons.append("binary patches are forbidden")

    checks["mode_changes"] = any(f.mode_change for f in files)
    if checks["mode_changes"]:
        reasons.append("file mode changes are forbidden")

    deletion = any(f.new_path == "/dev/null" or f.change_type == "delete" for f in files)
    checks["deletions_allowed"] = not deletion
    if deletion:
        reasons.append("file deletions are forbidden in A4/A5 v1")

    secret_found = False
    for file in files:
        for line in file.added_lines:
            _, found = scan_secrets(line)
            if found:
                secret_found = True
                break
    checks["secret_scan_ok"] = not secret_found
    if secret_found:
        reasons.append("patch appears to introduce secret material")

    checks["base_sha_match"] = _base_hashes_match(proposal, files, project_root)
    if not checks["base_sha_match"]:
        reasons.append("base_sha256 does not match current file content")

    objective_ok, objective_reasons = _validate_implementation_objective(proposal, files, mission)
    checks["objective_satisfied"] = objective_ok
    reasons.extend(objective_reasons)

    semantic_ok, semantic_reasons = _semantic_review_patch(proposal, files, mission)
    checks["semantic_review_ok"] = semantic_ok
    checks["semantic_review_score"] = _score_from_reasons(semantic_reasons)
    reasons.extend(semantic_reasons)

    verification_ok, verification_reasons = _validate_verification_plan(proposal, mission)
    checks["verification_plan_ok"] = verification_ok
    checks["verification_plan_score"] = _verification_plan_score(proposal, mission, changed_paths, verification_ok, verification_reasons)
    reasons.extend(verification_reasons)

    can_apply_check = (
        checks["unified_diff_parse"]
        and checks["paths_allowed"]
        and not checks["path_escape"]
        and not checks["binary_changes"]
        and not checks["mode_changes"]
        and checks["deletions_allowed"]
    )
    if can_apply_check:
        checks["applies_cleanly"] = _git_apply_check(diff, project_root)
        if not checks["applies_cleanly"]:
            reasons.append("patch does not apply cleanly")

    if checks["critical_paths_touched"] and not bool(getattr(mission, "critical_path_write_allowed", False)):
        status = "ESCALATE"
    elif all(
        checks[name]
        for name in (
            "unified_diff_parse",
            "paths_allowed",
            "max_files_ok",
            "max_patch_size_ok",
            "base_sha_match",
            "applies_cleanly",
            "secret_scan_ok",
            "deletions_allowed",
            "objective_satisfied",
            "semantic_review_ok",
            "verification_plan_ok",
        )
    ) and not checks["path_escape"] and not checks["binary_changes"] and not checks["mode_changes"]:
        status = "VALID"
    else:
        status = "INVALID"
    return _validation_report(status, checks, reasons, changed_paths, proposal, mission)


def apply_patch_in_isolated_worktree(proposal: JSON, mission: Any, project_root: str) -> JSON:
    """Validate and apply a patch in an isolated copy, never the main workspace."""
    _write_mission_artifact(mission, project_root)
    validation = validate_patch_proposal(proposal, mission, project_root)
    mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
    artifact_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    patch_path, rollback_path = _write_patch_artifacts(artifact_dir, proposal)
    validation_path = os.path.join(artifact_dir, "validation.json")
    verification_path = os.path.join(artifact_dir, "verification.json")
    _write_json(validation_path, validation)

    if validation["status"] != "VALID":
        report = _implementation_report(
            status="ESCALATE" if validation["status"] == "ESCALATE" else "FAILED",
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=["Patch was not applied because validation did not pass."],
        )
        _persist_implementation_runtime_artifacts(artifact_dir, mission, proposal, validation, [], report)
        return _relativize_report_paths(report, project_root)

    changed_paths = validation.get("changed_files", []) or []
    path_locks = _path_lock_paths(project_root, changed_paths)
    try:
        acquired_locks = _acquire_path_locks(path_locks)
    except PathLockError as exc:
        report = _implementation_report(
            status="FAILED",
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=[str(exc)],
        )
        _persist_implementation_runtime_artifacts(artifact_dir, mission, proposal, validation, [], report)
        return _relativize_report_paths(report, project_root)
    worktree_root = os.path.join(project_root, ".codex-oss", "worktrees", mission_id)
    try:
        if os.path.exists(worktree_root):
            shutil.rmtree(worktree_root)
        os.makedirs(os.path.dirname(worktree_root), exist_ok=True)
        shutil.copytree(project_root, worktree_root, ignore=shutil.ignore_patterns(".codex-oss", ".git", "__pycache__"))

        apply_result = subprocess.run(
            ["git", "apply", patch_path],
            cwd=worktree_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        verification: list[JSON] = []
        if apply_result.returncode != 0:
            verification.append({
                "command": ["git", "apply", "patch.diff"],
                "exit_code": apply_result.returncode,
                "stdout_summary": (apply_result.stdout or "")[:1000],
                "stderr_summary": (apply_result.stderr or "")[:1000],
            })
            status = "FAILED"
        else:
            verification = _run_verification(mission, worktree_root)
            if verification:
                status = "VERIFIED" if all(item["exit_code"] == 0 for item in verification) else "VERIFICATION_FAILED"
            else:
                status = "APPLIED_IN_ISOLATION"
    finally:
        _release_path_locks(acquired_locks)

    _write_json(verification_path, verification)
    report = _implementation_report(
        status=status,
        mission=mission,
        proposal=proposal,
        patch_path=patch_path,
        rollback_path=rollback_path,
        validation=validation,
        verification=verification,
        caveats=["Patch was applied only in an isolated worktree; main workspace was not mutated."],
    )
    _persist_implementation_runtime_artifacts(artifact_dir, mission, proposal, validation, verification, report)
    return _relativize_report_paths(report, project_root)


def apply_patch_in_temp_project(proposal: JSON, mission: Any, project_root: str) -> JSON:
    """Validate and apply a patch in a disposable temporary project copy."""
    _write_mission_artifact(mission, project_root)
    validation = validate_patch_proposal(proposal, mission, project_root)
    mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
    artifact_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    patch_path, rollback_path = _write_patch_artifacts(artifact_dir, proposal)
    validation_path = os.path.join(artifact_dir, "validation.json")
    verification_path = os.path.join(artifact_dir, "verification.json")
    _write_json(validation_path, validation)

    if validation["status"] != "VALID":
        report = _implementation_report(
            status="ESCALATE" if validation["status"] == "ESCALATE" else "FAILED",
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=["Patch was not applied because validation did not pass."],
        )
        _persist_implementation_runtime_artifacts(artifact_dir, mission, proposal, validation, [], report)
        return _relativize_report_paths(report, project_root)

    changed_paths = validation.get("changed_files", []) or []
    path_locks = _path_lock_paths(project_root, changed_paths)
    try:
        acquired_locks = _acquire_path_locks(path_locks)
    except PathLockError as exc:
        report = _implementation_report(
            status="FAILED",
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=[str(exc)],
        )
        _persist_implementation_runtime_artifacts(artifact_dir, mission, proposal, validation, [], report)
        return _relativize_report_paths(report, project_root)

    temp_root = tempfile.mkdtemp(prefix=f"oss_temp_project_{mission_id}_")
    execution_root = os.path.join(temp_root, "project")
    try:
        shutil.copytree(project_root, execution_root, ignore=shutil.ignore_patterns(".codex-oss", ".git", "__pycache__"))
        apply_result = subprocess.run(
            ["git", "apply", patch_path],
            cwd=execution_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        verification: list[JSON] = []
        if apply_result.returncode != 0:
            verification.append({
                "command": ["git", "apply", "patch.diff"],
                "exit_code": apply_result.returncode,
                "stdout_summary": (apply_result.stdout or "")[:1000],
                "stderr_summary": (apply_result.stderr or "")[:1000],
            })
            status = "FAILED"
        else:
            verification = _run_verification(mission, execution_root)
            if verification:
                status = "VERIFIED" if all(item["exit_code"] == 0 for item in verification) else "VERIFICATION_FAILED"
            else:
                status = "APPLIED_IN_TEMP_PROJECT"
    finally:
        _release_path_locks(acquired_locks)
        shutil.rmtree(temp_root, ignore_errors=True)

    _write_json(verification_path, verification)
    report = _implementation_report(
        status=status,
        mission=mission,
        proposal=proposal,
        patch_path=patch_path,
        rollback_path=rollback_path,
        validation=validation,
        verification=verification,
        caveats=["Patch was applied only in a temporary project copy; main workspace was not mutated."],
        execution_mode="temp_project",
    )
    _persist_implementation_runtime_artifacts(artifact_dir, mission, proposal, validation, verification, report)
    return _relativize_report_paths(report, project_root)


def apply_patch_in_workspace(proposal: JSON, mission: Any, project_root: str, certification: JSON | None = None) -> JSON:
    """Validate and apply a patch to the main workspace only behind explicit policy gates."""
    _write_mission_artifact(mission, project_root)
    validation = validate_patch_proposal(proposal, mission, project_root)
    mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
    artifact_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    patch_path, rollback_path = _write_patch_artifacts(artifact_dir, proposal)
    validation_path = os.path.join(artifact_dir, "validation.json")
    verification_path = os.path.join(artifact_dir, "verification.json")
    certification_path = os.path.join(artifact_dir, "certification.json")
    _write_json(validation_path, validation)
    if certification is not None:
        _write_json(certification_path, certification)

    if validation["status"] != "VALID":
        report = _implementation_report(
            status="ESCALATE" if validation["status"] == "ESCALATE" else "FAILED",
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=["Patch was not applied because validation did not pass."],
        )
        _persist_implementation_runtime_artifacts(
            artifact_dir, mission, proposal, validation, [], report, certification=certification
        )
        return _relativize_report_paths(report, project_root)

    caveats: list[str] = []
    apply_mode = getattr(mission, "apply_mode", "")
    if apply_mode not in ("workspace", "workspace_low_risk", "workspace_explicit", "critical_workspace_certified"):
        caveats.append("Workspace apply requires a workspace apply mode.")
    workspace_policy = getattr(mission, "workspace_apply_policy", {}) or {}
    workspace_allowed = os.getenv("ALLOW_A5_WORKSPACE_APPLY") == "1" or bool(workspace_policy.get("allow_direct_workspace_apply"))
    if apply_mode in ("workspace", "workspace_explicit", "workspace_low_risk") and not workspace_allowed:
        caveats.append("Workspace apply requires ALLOW_A5_WORKSPACE_APPLY=1 or workspace_apply_policy.allow_direct_workspace_apply=true.")
    if apply_mode in ("workspace", "workspace_low_risk", "workspace_explicit") and getattr(mission, "risk_tier", "low") != "low":
        caveats.append("Workspace apply is limited to low-risk missions in A5 v1.")
    if validation.get("checks", {}).get("critical_paths_touched") and not bool(workspace_policy.get("allow_critical_workspace_apply")):
        caveats.append("Workspace apply is forbidden for critical paths unless workspace_apply_policy.allow_critical_workspace_apply=true.")
    if apply_mode == "critical_workspace_certified":
        if getattr(mission, "tier", "") != "A6":
            caveats.append("critical_workspace_certified requires tier A6.")
        if getattr(mission, "risk_tier", "") != "critical":
            caveats.append("critical_workspace_certified requires risk_tier=critical.")
        if not bool(workspace_policy.get("certification_required")):
            caveats.append("critical_workspace_certified requires workspace_apply_policy.certification_required=true.")
        if not bool(workspace_policy.get("require_gpt_review", True)):
            caveats.append("critical_workspace_certified requires require_gpt_review=true.")
        if not workspace_policy.get("reviewer_models"):
            caveats.append("critical_workspace_certified requires reviewer_models.")
        if certification is None:
            caveats.append("critical_workspace_certified requires a runtime certification artifact.")
        elif not bool(certification.get("approved_for_workspace_apply")):
            caveats.append("critical_workspace_certified requires approved_for_workspace_apply=true.")
        elif workspace_policy.get("require_isolated_preflight") and not bool(certification.get("preflight", {}).get("ok")):
            caveats.append("critical_workspace_certified requires successful isolated preflight proof.")
        elif workspace_policy.get("require_rollback_proof") and not bool(certification.get("rollback_proof", {}).get("ok")):
            caveats.append("critical_workspace_certified requires rollback proof.")
    if workspace_policy.get("require_clean_worktree") and not _git_worktree_clean(project_root):
        caveats.append("Workspace apply requires a clean worktree.")
    changed_paths = validation.get("changed_files", []) or []
    if not workspace_policy.get("allow_dirty_target_files") and _git_paths_dirty(project_root, changed_paths):
        caveats.append("Workspace apply requires clean target files unless workspace_apply_policy.allow_dirty_target_files=true.")
    lock_path = _workspace_lock_path(project_root)
    lock_acquired = False
    path_locks: list[str] = []
    acquired_path_locks: list[str] = []
    if not caveats:
        try:
            _acquire_workspace_lock(lock_path)
            lock_acquired = True
            path_locks = _path_lock_paths(project_root, changed_paths)
            acquired_path_locks = _acquire_path_locks(path_locks)
        except FileExistsError:
            caveats.append("Workspace apply lock is already held.")
        except PathLockError as exc:
            caveats.append(str(exc))
    if caveats:
        report = _implementation_report(
            status="FAILED",
            mission=mission,
            proposal=proposal,
            patch_path=patch_path,
            rollback_path=rollback_path,
            validation=validation,
            verification=[],
            caveats=caveats,
            certification=certification,
        )
        _persist_implementation_runtime_artifacts(
            artifact_dir, mission, proposal, validation, [], report, certification=certification
        )
        return _relativize_report_paths(report, project_root)

    try:
        snapshot = _snapshot_changed_files(project_root, validation.get("changed_files", []) or [])
        apply_result = subprocess.run(
            ["git", "apply", patch_path],
            cwd=project_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        verification: list[JSON] = []
        main_workspace_mutated = False
        if apply_result.returncode != 0:
            verification.append({
                "command": ["git", "apply", "patch.diff"],
                "exit_code": apply_result.returncode,
                "stdout_summary": (apply_result.stdout or "")[:1000],
                "stderr_summary": (apply_result.stderr or "")[:1000],
            })
            status = "FAILED"
        else:
            main_workspace_mutated = True
            verification = _run_verification(mission, project_root)
            if verification:
                status = "VERIFIED" if all(item["exit_code"] == 0 for item in verification) else "VERIFICATION_FAILED"
            else:
                status = "APPLIED_TO_WORKSPACE"
            if status == "VERIFICATION_FAILED":
                _restore_snapshot(project_root, snapshot)
                main_workspace_mutated = False
                caveats.append("Verification failed; workspace changes were rolled back from the pre-apply snapshot.")
    finally:
        _release_path_locks(acquired_path_locks)
        if lock_acquired:
            _release_workspace_lock(lock_path)

    _write_json(verification_path, verification)
    report = _implementation_report(
        status=status,
        mission=mission,
        proposal=proposal,
        patch_path=patch_path,
        rollback_path=rollback_path,
        validation=validation,
        verification=verification,
        caveats=caveats or ["Patch was applied to the main workspace under explicit A5 workspace policy gates."],
        main_workspace_mutated=main_workspace_mutated,
        certification=certification,
        execution_mode="workspace",
    )
    _persist_implementation_runtime_artifacts(
        artifact_dir, mission, proposal, validation, verification, report, certification=certification
    )
    return _relativize_report_paths(report, project_root)


def _parse_unified_diff(diff: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: Optional[DiffFile] = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                files.append(current)
            current = DiffFile(old_path="", new_path="")
            parts = line.split()
            if len(parts) >= 4:
                current.old_path = _strip_diff_prefix(parts[2])
                current.new_path = _strip_diff_prefix(parts[3])
            continue
        if current is None:
            continue
        if line.startswith("--- "):
            current.old_path = _strip_diff_prefix(line[4:].strip())
        elif line.startswith("+++ "):
            current.new_path = _strip_diff_prefix(line[4:].strip())
        elif line.startswith("Binary files") or line == "GIT binary patch":
            current.binary = True
        elif line.startswith(("old mode ", "new mode ")):
            current.mode_change = True
        elif line.startswith("deleted file mode"):
            current.change_type = "delete"
        elif line.startswith("+") and not line.startswith("+++ "):
            current.added_lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("--- "):
            current.removed_lines.append(line[1:])
    if current:
        files.append(current)
    return [file for file in files if file.path]


def _strip_diff_prefix(path: str) -> str:
    if path in ("/dev/null", "dev/null"):
        return "/dev/null"
    path = path.strip().strip("'\"")
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _path_has_escape(path: str, project_root: str) -> bool:
    if not path or path == "/dev/null":
        return True
    if os.path.isabs(path):
        real = os.path.realpath(path)
    else:
        real = os.path.realpath(os.path.join(project_root, path))
    root = os.path.realpath(project_root)
    return not (real == root or real.startswith(root + os.sep)) or ".." in path.split("/")


def _path_allowed(path: str, mission: Any) -> bool:
    if _deny_path(path):
        return False
    owned = list(getattr(mission, "owned_paths", []) or [])
    for allowed in owned:
        if _path_matches_scope(path, allowed):
            return True
    return False


def _path_matches_scope(path: str, scope: str) -> bool:
    path = path.strip("/")
    scope = scope.strip("/")
    if not scope:
        return False
    if scope.endswith("/"):
        return path.startswith(scope)
    return path == scope or path.startswith(scope + "/")


def _deny_path(path: str) -> bool:
    normalized = path.strip().strip("/")
    if normalized.startswith(".") and normalized not in (".",):
        if normalized == ".env" or normalized.startswith(".git/") or normalized.startswith(".codex-oss/"):
            return True
    for deny in DEFAULT_DENY_ROOTS:
        deny = deny.strip("/")
        if normalized == deny or normalized.startswith(deny + "/"):
            return True
    return normalized.endswith(".env")


def _base_hashes_match(proposal: JSON, files: list[DiffFile], project_root: str) -> bool:
    expected_by_path = {}
    for item in proposal.get("changed_files", []) or []:
        if not isinstance(item, dict):
            return False
        path = str(item.get("path", "") or "")
        expected = str(item.get("base_sha256", "") or "")
        expected_by_path[path] = expected
    for file in files:
        if file.old_path == "/dev/null":
            continue
        expected = expected_by_path.get(file.path) or expected_by_path.get(file.old_path)
        if not expected:
            return False
        current_path = os.path.join(project_root, file.old_path)
        if not os.path.exists(current_path):
            return False
        with open(current_path, "r", encoding="utf-8") as handle:
            actual = hashlib.sha256(handle.read().encode()).hexdigest()
        if actual != expected:
            return False
    return True


def _validate_implementation_objective(proposal: JSON, files: list[DiffFile], mission: Any) -> tuple[bool, list[str]]:
    spec = getattr(mission, "objective_spec", None)
    if not spec:
        return True, []
    objective_type = str(spec.get("objective_type", "") or "")
    if objective_type == "implementation_test_only":
        return _validate_test_only_objective(spec, files)
    if objective_type == "documentation_patch":
        return _validate_documentation_objective(spec, files)
    if objective_type == "implementation_patch":
        return _validate_general_implementation_objective(spec, files)
    if objective_type == "critical_path_patch":
        return _validate_general_implementation_objective(spec, files)
    return True, []


def _validate_test_only_objective(spec: JSON, files: list[DiffFile]) -> tuple[bool, list[str]]:
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    test_file = str(target.get("test_file", "") or "").strip()
    source_files = {str(path) for path in target.get("source_files", []) or []}
    required_test_names = [str(name) for name in target.get("required_test_names", []) or []]
    reasons: list[str] = []
    changed_paths = {file.path for file in files}

    if test_file and test_file not in changed_paths:
        reasons.append(f"required test file was not changed: {test_file}")
    for path in sorted(changed_paths):
        if path in source_files:
            reasons.append(f"source file changed under test-only objective: {path}")
        elif test_file and path != test_file:
            reasons.append(f"test-only objective changed unexpected file: {path}")

    added_text = "\n".join(line for file in files for line in file.added_lines)
    removed_text = "\n".join(line for file in files for line in file.removed_lines)
    for name in required_test_names:
        if not re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", added_text, re.MULTILINE):
            reasons.append(f"required test name missing from added lines: {name}")
    if re.search(r"^\s*def\s+test_[A-Za-z0-9_]*\s*\(", removed_text, re.MULTILINE):
        reasons.append("test-only objective removed an existing test")

    return not reasons, reasons


def _validate_general_implementation_objective(spec: JSON, files: list[DiffFile]) -> tuple[bool, list[str]]:
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    reasons: list[str] = []
    changed_paths = {file.path for file in files}
    file_by_path = {file.path: file for file in files}

    for path in _target_list(target, "required_changed_files"):
        if path not in changed_paths:
            reasons.append(f"required changed file missing: {path}")
    for path in _target_list(target, "forbidden_changed_files"):
        if path in changed_paths:
            reasons.append(f"forbidden changed file touched: {path}")
    for path in _target_list(target, "required_source_files"):
        if path not in changed_paths:
            reasons.append(f"required source file missing: {path}")
    for path in _target_list(target, "required_test_files"):
        if path not in changed_paths:
            reasons.append(f"required test file missing: {path}")

    for symbol in target.get("required_symbols", []) or []:
        if not isinstance(symbol, dict):
            continue
        path = str(symbol.get("path", "") or "")
        name = str(symbol.get("name", "") or "")
        kind = str(symbol.get("kind", "function") or "function")
        added = "\n".join(file_by_path.get(path, DiffFile("", "")).added_lines)
        if not _added_symbol_present(added, kind, name):
            reasons.append(f"required symbol missing from added lines: {path}:{name}")

    added_text = "\n".join(line for file in files for line in file.added_lines)
    removed_text = "\n".join(line for file in files for line in file.removed_lines)
    for name in _target_list(target, "required_test_names"):
        if not re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", added_text, re.MULTILINE):
            reasons.append(f"required test name missing from added lines: {name}")
    for pattern in _target_list(target, "forbidden_removed_patterns"):
        if pattern and pattern in removed_text:
            reasons.append(f"forbidden removal pattern matched: {pattern}")

    return not reasons, reasons


def _validate_documentation_objective(spec: JSON, files: list[DiffFile]) -> tuple[bool, list[str]]:
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    reasons: list[str] = []
    changed_paths = {file.path for file in files}
    added_text = "\n".join(line for file in files for line in file.added_lines)
    removed_text = "\n".join(line for file in files for line in file.removed_lines)

    for path in _target_list(target, "required_changed_files"):
        if path not in changed_paths:
            reasons.append(f"required changed file missing: {path}")
    for path in _target_list(target, "forbidden_changed_files"):
        if path in changed_paths:
            reasons.append(f"forbidden changed file touched: {path}")
    for heading in _target_list(target, "required_markdown_headings"):
        if not re.search(rf"^\s*#+\s+{re.escape(heading)}\s*$", added_text, re.MULTILINE):
            reasons.append(f"required markdown heading missing from added lines: {heading}")
    for text in _target_list(target, "required_content_substrings"):
        if text not in added_text:
            reasons.append(f"required content substring missing from added lines: {text}")
    for pattern in _target_list(target, "forbidden_removed_patterns"):
        if pattern and pattern in removed_text:
            reasons.append(f"forbidden removal pattern matched: {pattern}")

    return not reasons, reasons


def _target_list(target: JSON, key: str) -> list[str]:
    value = target.get(key, [])
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _added_symbol_present(added_text: str, kind: str, name: str) -> bool:
    if not name:
        return True
    if kind in ("function", "method"):
        return bool(re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", added_text, re.MULTILINE))
    if kind == "class":
        return bool(re.search(rf"^\s*class\s+{re.escape(name)}\b", added_text, re.MULTILINE))
    return name in added_text


def _semantic_review_patch(proposal: JSON, files: list[DiffFile], mission: Any) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    objective_type = ""
    spec = getattr(mission, "objective_spec", None)
    if isinstance(spec, dict):
        objective_type = str(spec.get("objective_type", "") or "")
    changed_paths = {file.path for file in files}
    source_changed = [path for path in changed_paths if not _looks_like_test_path(path)]
    test_changed = [path for path in changed_paths if _looks_like_test_path(path)]
    removed_text = "\n".join(line for file in files for line in file.removed_lines)

    if (
        source_changed
        and not test_changed
        and getattr(mission, "tier", "") in ("A5", "A6")
        and objective_type not in {"critical_path_patch", "documentation_patch"}
    ):
        reasons.append("source implementation change requires an accompanying test change")
    if re.search(r"^\s*def\s+test_[A-Za-z0-9_]*\s*\(", removed_text, re.MULTILINE):
        reasons.append("semantic review blocks removal of existing tests")
    risk = proposal.get("risk_assessment", {}) if isinstance(proposal.get("risk_assessment"), dict) else {}
    blast_radius = str(risk.get("blast_radius", "") or "").lower()
    if "test-only" in blast_radius and source_changed:
        reasons.append("risk assessment claims test-only but source files changed")
    for path in changed_paths:
        if _dependency_manifest_path(path):
            reasons.append(f"dependency manifest changes require GPT review: {path}")
    return not reasons, reasons


def _looks_like_test_path(path: str) -> bool:
    base = os.path.basename(path)
    return path.startswith("tests/") or base.startswith("test_") or base.endswith("_test.py")


def _dependency_manifest_path(path: str) -> bool:
    base = os.path.basename(path)
    return base in {"package.json", "pnpm-lock.yaml", "package-lock.json", "requirements.txt", "pyproject.toml", "poetry.lock"}


def _validate_verification_plan(proposal: JSON, mission: Any) -> tuple[bool, list[str]]:
    if getattr(mission, "tier", "") == "A4":
        return True, []
    policy = getattr(mission, "verification_policy", {}) or {}
    allowed = policy.get("allowed_commands", []) or []
    if not allowed:
        return False, ["implementation apply requires at least one allowed verification command"]
    proposed = proposal.get("verification_plan", []) if isinstance(proposal.get("verification_plan"), list) else []
    if proposed:
        allowed_set = {tuple(command) for command in allowed if isinstance(command, list)}
        for item in proposed:
            if not isinstance(item, dict):
                continue
            command = item.get("command")
            if isinstance(command, list) and tuple(command) not in allowed_set:
                return False, [f"verification command is not mission-allowed: {' '.join(str(part) for part in command)}"]
    return True, []


def _verification_plan_score(
    proposal: JSON,
    mission: Any,
    changed_paths: list[str],
    verification_ok: bool,
    verification_reasons: list[str],
) -> int:
    score = 100 if verification_ok else 40
    for reason in verification_reasons:
        if "not mission-allowed" in reason:
            score -= 30
        elif "requires at least one allowed verification command" in reason:
            score -= 40
    proposed = proposal.get("verification_plan", []) if isinstance(proposal.get("verification_plan"), list) else []
    commands = [" ".join(item.get("command", []) or []) for item in proposed if isinstance(item, dict)]
    if changed_paths and commands:
        covered = 0
        for path in changed_paths:
            if any(path in command or os.path.basename(path) in command for command in commands):
                covered += 1
        if covered == 0:
            score -= 20
        elif covered < len(changed_paths):
            score -= 10
    return max(0, min(100, score))


def _score_from_reasons(reasons: list[str]) -> int:
    return max(0, 100 - (len(reasons) * 25))


def _git_apply_check(diff: str, project_root: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "apply", "--check", "-"],
            input=diff,
            cwd=project_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _snapshot_changed_files(project_root: str, changed_paths: list[str]) -> dict[str, JSON]:
    snapshot: dict[str, JSON] = {}
    for path in changed_paths:
        full_path = os.path.join(project_root, path)
        try:
            with open(full_path, "r", encoding="utf-8") as handle:
                snapshot[path] = {"exists": True, "content": handle.read()}
        except FileNotFoundError:
            snapshot[path] = {"exists": False, "content": ""}
    return snapshot


def _restore_snapshot(project_root: str, snapshot: dict[str, JSON]) -> None:
    for path, item in snapshot.items():
        full_path = os.path.join(project_root, path)
        if item.get("exists"):
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as handle:
                handle.write(str(item.get("content", "")))
        else:
            try:
                os.remove(full_path)
            except FileNotFoundError:
                pass


def _workspace_lock_path(project_root: str) -> str:
    return os.path.join(project_root, ".codex-oss", "locks", "workspace-apply.lock")


def _acquire_workspace_lock(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def _release_workspace_lock(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _git_worktree_clean(project_root: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and not (proc.stdout or "").strip()


def _run_verification(mission: Any, worktree_root: str) -> list[JSON]:
    policy = getattr(mission, "verification_policy", {}) or {}
    allowed = list(policy.get("allowed_commands", []) or [])
    max_commands = int(policy.get("max_commands", len(allowed)))
    timeout = int(policy.get("timeout_seconds", 60))
    results = []
    for command in allowed[:max_commands]:
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            continue
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=worktree_root,
                env=_minimal_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            exit_code = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = "verification timeout"
        except FileNotFoundError:
            exit_code = -1
            stdout = ""
            stderr = "verification command not found"
        results.append({
            "command": command,
            "exit_code": exit_code,
            "duration_seconds": round(time.time() - started, 3),
            "stdout_summary": stdout[:1000],
            "stderr_summary": stderr[:1000],
        })
    return results


def _validation_report(
    status: str,
    checks: JSON,
    reasons: list[str],
    changed_paths: list[str],
    proposal: JSON | None = None,
    mission: Any | None = None,
) -> JSON:
    proposal = proposal or {}
    proposal_source = str(proposal.get("proposal_source", "") or "raw_patch_proposal_v1")
    runtime_built_diff = proposal_source in {"desired_state_v1", "patch_recipe_v1", "patch_intent_v1"}
    return {
        "patch_validation_version": "1.0",
        "status": status,
        "report_source": "runtime",
        "proposal_source": proposal_source,
        "runtime_built_diff": runtime_built_diff,
        "model_repair_count": int(getattr(mission, "model_repair_count", 0) or 0) if mission is not None else 0,
        "semantic_review_ok": bool(checks.get("semantic_review_ok", False)),
        "semantic_review_score": int(checks.get("semantic_review_score", 0) or 0),
        "verification_plan_ok": bool(checks.get("verification_plan_ok", False)),
        "verification_plan_score": int(checks.get("verification_plan_score", 0) or 0),
        "checks": checks,
        "changed_files": sorted(set(changed_paths)),
        "reasons": reasons,
    }


def _implementation_report(
    status: str,
    mission: Any,
    proposal: JSON,
    patch_path: str,
    rollback_path: str,
    validation: JSON,
    verification: list[JSON],
    caveats: list[str],
    main_workspace_mutated: bool = False,
    certification: JSON | None = None,
    execution_mode: str = "",
) -> JSON:
    changed = validation.get("changed_files", [])
    proposal_source = str(
        proposal.get("proposal_source", "")
        or validation.get("proposal_source", "")
        or "raw_patch_proposal_v1"
    )
    runtime_built_diff = proposal_source in {"desired_state_v1", "patch_recipe_v1", "patch_intent_v1"}
    workspace_policy = getattr(mission, "workspace_apply_policy", {}) or {}
    return {
        "implementation_report_version": "1.0",
        "status": status,
        "report_source": "runtime",
        "explorer_model": getattr(mission, "last_reasoning_model", ""),
        "patch_model": getattr(mission, "runtime_model_alias", ""),
        "proposal_source": proposal_source,
        "runtime_built_diff": runtime_built_diff,
        "model_repair_count": int(getattr(mission, "model_repair_count", 0) or 0),
        "workspace_policy": workspace_policy,
        "execution_mode": execution_mode or getattr(mission, "apply_mode", ""),
        "semantic_review_ok": bool(validation.get("checks", {}).get("semantic_review_ok", False)),
        "semantic_review_score": int(validation.get("checks", {}).get("semantic_review_score", 0) or 0),
        "verification_plan_ok": bool(validation.get("checks", {}).get("verification_plan_ok", False)),
        "verification_plan_score": int(validation.get("checks", {}).get("verification_plan_score", 0) or 0),
        "verification_scope": _verification_scope(validation.get("changed_files", []) or [], verification),
        "changed_files": changed,
        "patch_artifact": patch_path,
        "certification_artifact": (
            os.path.join(os.path.dirname(patch_path), "certification.json")
            if certification is not None
            else ""
        ),
        "certification_status": str(certification.get("status", "") or "") if isinstance(certification, dict) else "",
        "validation": validation,
        "verification": verification,
        "main_workspace_mutated": bool(main_workspace_mutated),
        "rollback": {
            "available": bool(patch_path),
            "artifact": rollback_path,
            "method": "git apply -R rollback.diff",
        },
        "confidence": "MEDIUM" if status == "VERIFIED" else "LOW",
        "caveats": caveats,
        "gpt_review_required": True,
    }


def _relativize_report_paths(report: JSON, project_root: str) -> JSON:
    result = dict(report)
    patch = str(result.get("patch_artifact", "") or "")
    if patch and os.path.isabs(patch):
        result["patch_artifact"] = os.path.relpath(patch, project_root)
    certification = str(result.get("certification_artifact", "") or "")
    if certification and os.path.isabs(certification):
        result["certification_artifact"] = os.path.relpath(certification, project_root)
    rollback = result.get("rollback")
    if isinstance(rollback, dict):
        rollback_artifact = str(rollback.get("artifact", "") or "")
        if rollback_artifact and os.path.isabs(rollback_artifact):
            rollback_copy = dict(rollback)
            rollback_copy["artifact"] = os.path.relpath(rollback_artifact, project_root)
            result["rollback"] = rollback_copy
    return result


def _write_json(path: str, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True))


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _write_mission_artifact(mission: Any, project_root: str) -> None:
    mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
    artifact_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    payload = {
        "mission_id": mission_id,
        "tier": getattr(mission, "tier", ""),
        "mode": getattr(mission, "mode", ""),
        "objective": getattr(mission, "objective", ""),
        "risk_tier": getattr(mission, "risk_tier", ""),
        "owned_paths": list(getattr(mission, "owned_paths", []) or []),
        "read_only_paths": list(getattr(mission, "read_only_paths", []) or []),
        "apply_mode": getattr(mission, "apply_mode", ""),
        "workspace_apply_policy": dict(getattr(mission, "workspace_apply_policy", {}) or {}),
        "objective_spec": dict(getattr(mission, "objective_spec", {}) or {}) if isinstance(getattr(mission, "objective_spec", None), dict) else None,
    }
    _write_json(os.path.join(artifact_dir, "mission.json"), payload)


def _minimal_env() -> dict:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM")
    return {key: value for key, value in os.environ.items() if key in allowed}


class PathLockError(RuntimeError):
    pass


def _path_lock_paths(project_root: str, changed_paths: list[str]) -> list[str]:
    base = os.path.join(project_root, ".codex-oss", "locks", "paths")
    locks = []
    for path in sorted(set(changed_paths)):
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        locks.append(os.path.join(base, f"{digest}.lock"))
    return locks


def _acquire_path_locks(lock_paths: list[str]) -> list[str]:
    acquired: list[str] = []
    try:
        for path in lock_paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            acquired.append(path)
        return acquired
    except FileExistsError as exc:
        _release_path_locks(acquired)
        raise PathLockError("Path lock is already held for one or more changed files.") from exc


def _release_path_locks(lock_paths: list[str]) -> None:
    for path in lock_paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _git_paths_dirty(project_root: str, changed_paths: list[str]) -> bool:
    if not changed_paths:
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + list(changed_paths),
            cwd=project_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


def _verification_scope(changed_files: list[str], verification: list[JSON]) -> JSON:
    if not verification:
        return {"level": "none", "reason": "No verification commands ran."}
    commands = [" ".join(item.get("command", []) or []) for item in verification if isinstance(item, dict)]
    if not commands:
        return {"level": "unknown", "reason": "Verification records were malformed."}
    changed = [path for path in changed_files if isinstance(path, str)]
    matched = 0
    for path in changed:
        if any(path in command or os.path.basename(path) in command for command in commands):
            matched += 1
    if changed and matched == len(changed):
        return {"level": "targeted", "reason": "Verification commands referenced every changed file directly."}
    if matched:
        return {"level": "partial", "reason": "Verification commands referenced some changed files directly."}
    return {"level": "broad", "reason": "Verification ran, but commands did not directly reference changed files."}


def _workspace_certification_contract_text() -> str:
    return (
        "Return exactly one CertificationReviewV1 JSON object. Do not use markdown. "
        "The object must include certification_review_version='1.0', approved (true or false), "
        "confidence ('LOW', 'MEDIUM', or 'HIGH'), findings (array of short strings), and rationale. "
        "Approve only if the patch matches the mission objective, changed files are appropriately scoped, "
        "verification looks relevant, and no obvious policy issue remains."
    )


def _parse_certification_review_text(text: str) -> tuple[JSON | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, "empty certification review response"
    block = _extract_optional_block(raw, "OSS_CERTIFICATION_REVIEW_JSON")
    if block is not None:
        raw = block.strip()
    if raw.startswith("```"):
        raw = _strip_fenced_json(raw)
    if not raw.startswith("{"):
        raw = _extract_first_json_object(raw) or raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"CertificationReviewV1 JSON parse failed: {exc}"
    if not isinstance(parsed, dict) or parsed.get("certification_review_version") != "1.0":
        return None, "CertificationReviewV1 response must be a JSON object with certification_review_version=1.0"
    return parsed, None


def _call_model_with_override(
    call_model: Callable[[Any, Any, float], JSON],
    messages: list[JSON],
    tools: list[JSON],
    timeout: float,
    model_alias_override: str | None = None,
) -> JSON:
    if model_alias_override:
        try:
            return call_model(messages, tools, timeout, model_alias_override=model_alias_override)
        except TypeError:
            pass
    return call_model(messages, tools, timeout)


def _run_workspace_certification(
    mission: Any,
    proposal: JSON,
    validation: JSON,
    call_model: Callable[[Any, Any, float], JSON],
    timeout: float,
    project_root: str,
) -> JSON:
    workspace_policy = getattr(mission, "workspace_apply_policy", {}) or {}
    reviewer_models = [str(item) for item in workspace_policy.get("reviewer_models", []) or [] if str(item).strip()]
    artifact: JSON = {
        "certification_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "")),
        "apply_mode": str(getattr(mission, "apply_mode", "")),
        "reviewer_models": reviewer_models,
        "reviews": [],
        "runtime_checks": {
            "validation_status": validation.get("status", ""),
            "semantic_review_ok": bool(validation.get("checks", {}).get("semantic_review_ok", False)),
            "semantic_review_score": int(validation.get("checks", {}).get("semantic_review_score", 0) or 0),
            "verification_plan_ok": bool(validation.get("checks", {}).get("verification_plan_ok", False)),
            "verification_plan_score": int(validation.get("checks", {}).get("verification_plan_score", 0) or 0),
            "critical_paths_touched": bool(validation.get("checks", {}).get("critical_paths_touched", False)),
        },
        "preflight": {"ok": False, "status": "SKIPPED", "report_artifact": "", "reason": "not required"},
        "rollback_proof": {"ok": False, "status": "SKIPPED", "reason": "not required"},
        "invariant_results": [],
        "approved_for_workspace_apply": False,
        "status": "REJECTED",
        "proof_grade": "UNCONFIRMED",
        "gpt_review_required": bool(workspace_policy.get("require_gpt_review", True)),
        "decision_reasons": [],
    }
    if validation.get("status") != "VALID":
        artifact["decision_reasons"].append("Patch validation was not VALID.")
        return artifact
    if not reviewer_models:
        artifact["decision_reasons"].append("No reviewer_models were configured.")
        return artifact
    min_approvals = int(workspace_policy.get("min_reviewer_approvals", 0) or 0)
    if min_approvals <= 0:
        min_approvals = len(reviewer_models)
    artifact["min_reviewer_approvals"] = min_approvals

    if bool(workspace_policy.get("require_isolated_preflight")):
        artifact["preflight"] = _run_certification_preflight(mission, proposal, project_root)
        if not artifact["preflight"].get("ok"):
            artifact["decision_reasons"].append("Isolated preflight proof failed.")
            return artifact

    if bool(workspace_policy.get("require_rollback_proof")):
        artifact["rollback_proof"] = _prove_patch_reversible(proposal, validation, project_root)
        if not artifact["rollback_proof"].get("ok"):
            artifact["decision_reasons"].append("Rollback proof failed.")
            return artifact

    invariant_commands = list(workspace_policy.get("invariant_commands", []) or [])
    if invariant_commands:
        artifact["invariant_results"] = _run_invariant_commands(invariant_commands, project_root)
        if not all(item.get("exit_code") == 0 for item in artifact["invariant_results"]):
            artifact["decision_reasons"].append("One or more invariant commands failed.")
            return artifact

    prompt = (
        _workspace_certification_contract_text()
        + "\n\n"
        f"Mission id: {getattr(mission, 'mission_id', '')}\n"
        f"Tier: {getattr(mission, 'tier', '')}\n"
        f"Objective: {getattr(mission, 'objective', '')}\n"
        f"Owned paths: {list(getattr(mission, 'owned_paths', []) or [])}\n"
        f"Apply mode: {getattr(mission, 'apply_mode', '')}\n"
        f"Workspace policy: {json.dumps(workspace_policy, sort_keys=True)}\n"
        f"Validation summary: {json.dumps(validation, sort_keys=True)}\n"
        f"Patch proposal summary: {json.dumps({k: proposal.get(k) for k in ('summary', 'changed_files', 'risk_assessment', 'verification_plan')}, sort_keys=True)}\n"
        "Patch diff:\n"
        f"{str(proposal.get('unified_diff', '') or '')[:8000]}\n"
        "Allowed file context:\n"
        f"{_build_implementation_context(mission)[:12000]}\n"
    )
    remaining = max(20.0, min(float(timeout or 60.0), 90.0))
    for reviewer in reviewer_models:
        try:
            response = _call_model_with_override(
                call_model,
                [
                    {"role": "system", "content": "You are a cautious certification reviewer for runtime-controlled patch application."},
                    {"role": "user", "content": prompt},
                ],
                [],
                remaining,
                model_alias_override=reviewer,
            )
            review_text = _extract_model_text(response)
            review, error = _parse_certification_review_text(review_text)
            if review is None:
                artifact["decision_reasons"].append(f"{reviewer}: {error or 'invalid certification review'}")
                artifact["reviews"].append({
                    "reviewer_model": reviewer,
                    "approved": False,
                    "confidence": "LOW",
                    "findings": [error or "invalid certification review"],
                    "rationale": error or "Certification review parse failed.",
                })
                continue
            artifact["reviews"].append({
                "reviewer_model": reviewer,
                "approved": bool(review.get("approved", False)),
                "confidence": str(review.get("confidence", "LOW") or "LOW"),
                "findings": list(review.get("findings", []) or []),
                "rationale": str(review.get("rationale", "") or ""),
            })
        except Exception as exc:
            artifact["decision_reasons"].append(f"{reviewer}: certification review call failed: {exc}")
            artifact["reviews"].append({
                "reviewer_model": reviewer,
                "approved": False,
                "confidence": "LOW",
                "findings": [f"certification review call failed: {exc}"],
                "rationale": "Reviewer model call failed.",
            })
    approved_count = sum(1 for review in artifact["reviews"] if bool(review.get("approved")))
    approved = bool(artifact["reviews"]) and approved_count >= min_approvals and all(bool(review.get("approved")) for review in artifact["reviews"])
    artifact["approved_count"] = approved_count
    artifact["approved_for_workspace_apply"] = approved
    artifact["status"] = "APPROVED" if approved else "REJECTED"
    preflight_ok = bool(artifact.get("preflight", {}).get("ok")) or artifact.get("preflight", {}).get("status") == "SKIPPED"
    rollback_ok = bool(artifact.get("rollback_proof", {}).get("ok")) or artifact.get("rollback_proof", {}).get("status") == "SKIPPED"
    invariants_ok = all(item.get("exit_code") == 0 for item in artifact.get("invariant_results", []) or [])
    runtime_checks = artifact.get("runtime_checks", {}) if isinstance(artifact.get("runtime_checks"), dict) else {}
    semantic_ok = bool(runtime_checks.get("semantic_review_ok"))
    verification_ok = bool(runtime_checks.get("verification_plan_ok"))
    artifact["proof_grade"] = (
        "PROOF_GRADED"
        if approved and preflight_ok and rollback_ok and invariants_ok and semantic_ok and verification_ok
        else "BASIC_APPROVED" if approved else "UNCONFIRMED"
    )
    if not artifact["decision_reasons"]:
        artifact["decision_reasons"].append(
            "All configured certification reviewers approved the patch for workspace apply."
            if approved
            else "One or more certification reviewers rejected the patch."
        )
    return artifact


def _run_certification_preflight(mission: Any, proposal: JSON, project_root: str) -> JSON:
    preflight_mission = copy.deepcopy(mission)
    preflight_mission.mission_id = f"{getattr(mission, 'mission_id', 'mission_unknown')}__cert_preflight"
    preflight_mission.apply_mode = "isolated_worktree"
    report = apply_patch_in_isolated_worktree(proposal, preflight_mission, project_root)
    status = str(report.get("status", "") or "")
    return {
        "ok": status == "VERIFIED",
        "status": status,
        "report_artifact": str(report.get("patch_artifact", "") or ""),
        "reason": "isolated preflight verification passed" if status == "VERIFIED" else "isolated preflight verification did not pass",
    }


def _prove_patch_reversible(proposal: JSON, validation: JSON, project_root: str) -> JSON:
    changed_paths = validation.get("changed_files", []) or []
    snapshot = _snapshot_changed_files(project_root, changed_paths)
    temp_root = tempfile.mkdtemp(prefix="oss_rollback_proof_")
    work_root = os.path.join(temp_root, "project")
    patch_path = os.path.join(temp_root, "patch.diff")
    try:
        shutil.copytree(project_root, work_root, ignore=shutil.ignore_patterns(".codex-oss", ".git", "__pycache__"))
        _write_text(patch_path, str(proposal.get("unified_diff", "") or ""))
        apply_proc = subprocess.run(
            ["git", "apply", patch_path],
            cwd=work_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if apply_proc.returncode != 0:
            return {"ok": False, "status": "APPLY_FAILED", "reason": (apply_proc.stderr or apply_proc.stdout or "")[:500]}
        reverse_proc = subprocess.run(
            ["git", "apply", "-R", patch_path],
            cwd=work_root,
            env=_minimal_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if reverse_proc.returncode != 0:
            return {"ok": False, "status": "REVERSE_FAILED", "reason": (reverse_proc.stderr or reverse_proc.stdout or "")[:500]}
        for path, item in snapshot.items():
            full_path = os.path.join(work_root, path)
            if item.get("exists"):
                with open(full_path, "r", encoding="utf-8") as handle:
                    if handle.read() != str(item.get("content", "")):
                        return {"ok": False, "status": "MISMATCH", "reason": f"rollback mismatch for {path}"}
            elif os.path.exists(full_path):
                return {"ok": False, "status": "MISMATCH", "reason": f"rollback left created file {path}"}
        return {"ok": True, "status": "PROVED", "reason": "Patch applied and reversed cleanly in disposable proof workspace."}
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _run_invariant_commands(commands: list[list[str]], project_root: str) -> list[JSON]:
    results: list[JSON] = []
    for command in commands:
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=project_root,
                env=_minimal_env(),
                capture_output=True,
                text=True,
                timeout=60,
            )
            exit_code = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = "invariant timeout"
        except FileNotFoundError:
            exit_code = -1
            stdout = ""
            stderr = "invariant command not found"
        results.append({
            "command": command,
            "exit_code": exit_code,
            "duration_seconds": round(time.time() - started, 3),
            "stdout_summary": stdout[:500],
            "stderr_summary": stderr[:500],
        })
    return results
