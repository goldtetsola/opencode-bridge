"""Runtime-owned implementation readiness graph for A4/A5/A6 missions."""

from __future__ import annotations

import os
import re
from typing import Any


JSON = dict[str, Any]


def build_implementation_readiness_graph(
    proposal: JSON,
    files: list[Any],
    mission: Any,
    checks: JSON,
) -> JSON:
    spec = getattr(mission, "objective_spec", None)
    target = spec.get("target", {}) if isinstance(spec, dict) and isinstance(spec.get("target"), dict) else {}
    objective_type = str(spec.get("objective_type", "") or "") if isinstance(spec, dict) else ""
    required_evidence_shapes = _string_list(spec.get("required_evidence_shapes", [])) if isinstance(spec, dict) else []
    changed_paths = {str(getattr(file, "path", "") or "") for file in files}
    file_by_path = {str(getattr(file, "path", "") or ""): file for file in files}
    added_text = "\n".join(line for file in files for line in getattr(file, "added_lines", []))
    removed_text = "\n".join(line for file in files for line in getattr(file, "removed_lines", []))
    obligations: list[JSON] = []

    for path in _target_list(target, "required_changed_files"):
        obligations.append(_path_obligation("required_changed_file", path, path in changed_paths))
    for path in _target_list(target, "required_source_files"):
        obligations.append(_path_obligation("required_source_file", path, path in changed_paths))
    for path in _target_list(target, "required_test_files"):
        obligations.append(_path_obligation("required_test_file", path, path in changed_paths))

    test_file = str(target.get("test_file", "") or "").strip()
    if test_file:
        obligations.append(_path_obligation("target_test_file", test_file, test_file in changed_paths))

    for symbol in target.get("required_symbols", []) or []:
        if not isinstance(symbol, dict):
            continue
        path = str(symbol.get("path", "") or "")
        kind = str(symbol.get("kind", "function") or "function")
        name = str(symbol.get("name", "") or "")
        added = "\n".join(getattr(file_by_path.get(path), "added_lines", []) or [])
        satisfied = _added_symbol_present(added, kind, name)
        obligations.append(
            {
                "id": f"required_symbol:{path}:{name or 'anonymous'}",
                "kind": "required_symbol",
                "required": True,
                "status": "satisfied" if satisfied else "missing",
                "path": path,
                "symbol_kind": kind,
                "name": name,
                "evidence_refs": [f"diff:{path}#symbol:{name}"] if satisfied and path else [],
                "missing_evidence": [] if satisfied else [f"added symbol {name} in {path}"],
                "reason": "" if satisfied else f"required symbol missing from added lines: {path}:{name}",
            }
        )

    for name in _target_list(target, "required_test_names"):
        satisfied = bool(re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", added_text, re.MULTILINE))
        obligations.append(
            {
                "id": f"required_test_name:{name}",
                "kind": "required_test_name",
                "required": True,
                "status": "satisfied" if satisfied else "missing",
                "name": name,
                "evidence_refs": [f"diff:tests#test:{name}"] if satisfied else [],
                "missing_evidence": [] if satisfied else [f"added test definition {name}"],
                "reason": "" if satisfied else f"required test name missing from added lines: {name}",
            }
        )

    for path in _target_list(target, "forbidden_changed_files"):
        contradicted = path in changed_paths
        obligations.append(
            {
                "id": f"forbidden_changed_file:{path}",
                "kind": "forbidden_changed_file",
                "required": True,
                "status": "contradicted" if contradicted else "satisfied",
                "path": path,
                "evidence_refs": [f"diff:{path}"] if contradicted else [],
                "missing_evidence": [],
                "reason": f"forbidden changed file touched: {path}" if contradicted else "",
            }
        )

    for pattern in _target_list(target, "forbidden_removed_patterns"):
        contradicted = bool(pattern and pattern in removed_text)
        obligations.append(
            {
                "id": f"forbidden_removed_pattern:{pattern}",
                "kind": "forbidden_removed_pattern",
                "required": True,
                "status": "contradicted" if contradicted else "satisfied",
                "pattern": pattern,
                "evidence_refs": [f"diff:removed#{pattern}"] if contradicted else [],
                "missing_evidence": [],
                "reason": f"forbidden removal pattern matched: {pattern}" if contradicted else "",
            }
        )

    source_files = {str(path) for path in target.get("source_files", []) or []}
    if source_files and objective_type == "implementation_test_only":
        touched = sorted(path for path in changed_paths if path in source_files)
        obligations.append(
            {
                "id": "test_only_source_unchanged",
                "kind": "source_unchanged",
                "required": True,
                "status": "contradicted" if touched else "satisfied",
                "paths": sorted(source_files),
                "evidence_refs": [f"diff:{path}" for path in touched],
                "missing_evidence": [],
                "reason": f"source file changed under test-only objective: {touched[0]}" if touched else "",
            }
        )

    for shape in required_evidence_shapes:
        satisfied = _evidence_shape_present(shape, files, proposal)
        obligations.append(
            {
                "id": f"required_evidence_shape:{shape}",
                "kind": "required_evidence_shape",
                "required": True,
                "status": "satisfied" if satisfied else "insufficient_evidence",
                "shape": shape,
                "evidence_refs": [f"shape:{shape}"] if satisfied else [],
                "missing_evidence": [] if satisfied else [shape],
                "reason": "" if satisfied else f"required evidence shape not found: {shape}",
            }
        )

    semantic_ok = bool(checks.get("semantic_review_ok", False))
    obligations.append(
        {
            "id": "semantic_review",
            "kind": "semantic_review",
            "required": True,
            "status": "satisfied" if semantic_ok else "contradicted",
            "evidence_refs": [],
            "missing_evidence": [],
            "reason": "" if semantic_ok else "semantic review did not pass",
        }
    )

    verification_required = str(getattr(mission, "tier", "") or "") in {"A5", "A6"}
    verification_ok = bool(checks.get("verification_plan_ok", False))
    obligations.append(
        {
            "id": "verification_plan",
            "kind": "verification_plan",
            "required": verification_required,
            "status": (
                "satisfied"
                if verification_ok
                else ("blocked" if verification_required else "satisfied")
            ),
            "evidence_refs": [f"verification:{len(proposal.get('verification_plan', []) or [])}"] if verification_ok else [],
            "missing_evidence": [] if verification_ok or not verification_required else ["mission-allowed verification command"],
            "reason": "" if verification_ok or not verification_required else "implementation apply requires a valid verification plan",
        }
    )

    apply_cleanly = bool(checks.get("applies_cleanly", False))
    obligations.append(
        {
            "id": "applies_cleanly",
            "kind": "apply_cleanliness",
            "required": True,
            "status": "satisfied" if apply_cleanly else "blocked",
            "evidence_refs": [],
            "missing_evidence": [] if apply_cleanly else ["clean git apply"],
            "reason": "" if apply_cleanly else "patch does not apply cleanly",
        }
    )

    statuses = [str(item.get("status", "") or "") for item in obligations if item.get("required", True)]
    missing = [item for item in obligations if item.get("status") == "missing"]
    insufficient = [item for item in obligations if item.get("status") == "insufficient_evidence"]
    contradicted = [item for item in obligations if item.get("status") == "contradicted"]
    blocked = [item for item in obligations if item.get("status") == "blocked"]
    required_obligations = [item for item in obligations if item.get("required", True)]
    satisfied = [item for item in required_obligations if item.get("status") == "satisfied"]

    if contradicted:
        recommended_status = "ESCALATE" if bool(checks.get("critical_paths_touched", False)) else "INVALID"
        reason = contradicted[0].get("reason") or "contradictory implementation evidence present"
    elif blocked:
        recommended_status = "INVALID"
        reason = blocked[0].get("reason") or "implementation is blocked on required readiness"
    elif missing or insufficient:
        recommended_status = "INVALID"
        culprit = (missing or insufficient)[0]
        reason = culprit.get("reason") or "required implementation evidence is incomplete"
    elif apply_cleanly and semantic_ok and (verification_ok or not verification_required):
        recommended_status = "VALID"
        reason = "Required implementation readiness obligations are satisfied."
    else:
        recommended_status = "INVALID"
        reason = "Implementation readiness is incomplete."

    next_required_actions = []
    for item in missing:
        if item.get("kind") in {"required_changed_file", "required_source_file", "required_test_file", "target_test_file"}:
            next_required_actions.append(
                {
                    "action_type": "patch_target",
                    "path": item.get("path", ""),
                    "reason": item.get("reason", "") or "required target file is not yet changed",
                }
            )
        elif item.get("kind") == "required_symbol":
            next_required_actions.append(
                {
                    "action_type": "add_symbol",
                    "path": item.get("path", ""),
                    "name": item.get("name", ""),
                    "symbol_kind": item.get("symbol_kind", ""),
                    "reason": item.get("reason", ""),
                }
            )
        elif item.get("kind") == "required_test_name":
            next_required_actions.append(
                {
                    "action_type": "add_test",
                    "name": item.get("name", ""),
                    "reason": item.get("reason", ""),
                }
            )
    for item in insufficient:
        next_required_actions.append(
            {
                "action_type": "strengthen_evidence",
                "shape": item.get("shape", ""),
                "reason": item.get("reason", ""),
            }
        )
    for item in blocked:
        next_required_actions.append(
            {
                "action_type": "unblock",
                "kind": item.get("kind", ""),
                "reason": item.get("reason", ""),
            }
        )

    coverage_status = {
        "required_total": len(required_obligations),
        "satisfied_total": len(satisfied),
        "missing_total": len(missing),
        "insufficient_evidence_total": len(insufficient),
        "blocked_total": len(blocked),
        "contradicted_total": len(contradicted),
        "can_propose": not missing and not insufficient and not contradicted,
        "can_apply": not missing and not insufficient and not contradicted and not blocked and apply_cleanly,
        "can_verify": not missing and not insufficient and not contradicted and not blocked and verification_ok,
        "can_report": bool(checks.get("unified_diff_parse", False)),
        "recommended_status": recommended_status,
        "reason": reason,
        "missing_obligation_ids": [str(item.get("id", "") or "") for item in missing],
        "blocked_obligation_ids": [str(item.get("id", "") or "") for item in blocked],
        "contradicted_obligation_ids": [str(item.get("id", "") or "") for item in contradicted],
        "next_required_actions": next_required_actions,
    }
    return {
        "implementation_readiness_graph_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "tier": str(getattr(mission, "tier", "") or ""),
        "objective_type": objective_type,
        "proposal_source": str(proposal.get("proposal_source", "") or "raw_patch_proposal_v1"),
        "obligations": obligations,
        "coverage_status": coverage_status,
    }


def _path_obligation(kind: str, path: str, satisfied: bool) -> JSON:
    return {
        "id": f"{kind}:{path}",
        "kind": kind,
        "required": True,
        "status": "satisfied" if satisfied else "missing",
        "path": path,
        "evidence_refs": [f"diff:{path}"] if satisfied and path else [],
        "missing_evidence": [] if satisfied else [path],
        "reason": "" if satisfied else f"required file missing from patch: {path}",
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


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


def _evidence_shape_present(shape: str, files: list[Any], proposal: JSON) -> bool:
    added_text = "\n".join(line for file in files for line in getattr(file, "added_lines", []))
    removed_text = "\n".join(line for file in files for line in getattr(file, "removed_lines", []))
    lowered = added_text.lower()
    if shape == "function_definition":
        return bool(re.search(r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", added_text, re.MULTILINE))
    if shape == "class_definition":
        return bool(re.search(r"^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\b", added_text, re.MULTILINE))
    if shape == "test_definition":
        return bool(re.search(r"^\s*def\s+test_[A-Za-z0-9_]*\s*\(", added_text, re.MULTILINE))
    if shape == "mapping_assignment":
        return ":" in added_text and "{" in added_text
    if shape == "config_value":
        return "=" in added_text or ":" in added_text
    if shape == "flag_parameter":
        return bool(re.search(r"\b(flag|allow|enable|require)_[A-Za-z0-9_]*\b", added_text))
    if shape == "flag_read":
        return "get(" in added_text or "if " in lowered
    if shape == "behavior_derivation":
        return "return " in added_text or "status" in lowered
    if shape == "zero_match":
        return not added_text.strip() and not removed_text.strip()
    if shape == "test_assertion":
        return "assert " in added_text
    if shape == "verification_command":
        return bool(proposal.get("verification_plan"))
    if shape == "source_change":
        return any(not _looks_like_test_path(str(getattr(file, "path", "") or "")) for file in files)
    if shape == "critical_source_change":
        return any(
            (not _looks_like_test_path(str(getattr(file, "path", "") or "")))
            and "/auth/" in f"/{str(getattr(file, 'path', '') or '').strip('/')}/"
            for file in files
        )
    return False


def _looks_like_test_path(path: str) -> bool:
    base = os.path.basename(path)
    if path.startswith("tests/fixtures/"):
        return base.startswith("test_") or base.endswith("_test.py")
    return path.startswith("tests/") or base.startswith("test_") or base.endswith("_test.py")
