"""SemanticReviewReportV1 — structured patch review with reason codes.

Reason taxonomy:

Blocking:
  forbidden_path        — patch touches a path outside owned_paths
  critical_path_write   — patch touches a critical path without permission
  unexpected_file_changed — file changed that is not in expected list
  test_removal          — patch removes an existing test function
  function_deletion     — patch deletes a function definition
  full_file_rewrite     — patch replaces the entire file
  dependency_change     — patch modifies a lockfile or dependency manifest
  secret_introduced     — patch introduces secret material
  binary_or_mode_change — patch changes binary content or file modes
  objective_mismatch    — patch does not match the objective spec
  verification_missing  — required verification plan is absent
  patch_noop            — patch produces no meaningful change
  patch_does_not_apply  — patch fails git apply --check
  base_hash_mismatch    — base_sha256 does not match current file

Repairable:
  anchor_not_found       — patch anchor cannot be located in the file
  ambiguous_anchor       — anchor matches multiple locations
  empty_change           — patch adds no content
  missing_required_symbol — required symbol not found in patch
  missing_required_test   — required test name not found in patch
  verification_plan_missing — verification plan absent but mission tier allows
  wrong_insertion_location — content inserted at wrong location
  content_already_exists   — patch adds content that already exists

Non-blocking:
  narrow_verification_only — only narrow verification ran
  formatting_change_detected — unrelated formatting changes present
  partial_objective_coverage — some objective requirements unmet
  no_broader_test_run      — no broader test suite was run
  source_change_without_test — source changed without test (A5/A6 advisory)
"""

from __future__ import annotations

import os
import re
from typing import Any

JSON = dict[str, Any]


BLOCKING_CODES = frozenset({
    "forbidden_path",
    "critical_path_write",
    "unexpected_file_changed",
    "test_removal",
    "function_deletion",
    "full_file_rewrite",
    "dependency_change",
    "secret_introduced",
    "binary_or_mode_change",
    "objective_mismatch",
    "verification_missing",
    "patch_noop",
    "patch_does_not_apply",
    "base_hash_mismatch",
})

REPAIRABLE_CODES = frozenset({
    "anchor_not_found",
    "ambiguous_anchor",
    "empty_change",
    "missing_required_symbol",
    "missing_required_test",
    "verification_plan_missing",
    "wrong_insertion_location",
    "content_already_exists",
})

NON_BLOCKING_CODES = frozenset({
    "narrow_verification_only",
    "formatting_change_detected",
    "partial_objective_coverage",
    "no_broader_test_run",
    "source_change_without_test",
})

ALL_CODES = BLOCKING_CODES | REPAIRABLE_CODES | NON_BLOCKING_CODES


def classify_file_role(path: str) -> str:
    """Classify a file path into a role for semantic review.

    Roles:
      production_source  — main codebase file (src/, codex_oss/, lib/, etc.)
      test_code          — test framework files under tests/ (not fixtures)
      test_fixture       — fixture/test data files (tests/fixtures/* or test_data/*)
      docs               — documentation (docs/, *.md, *.rst)
      config             — configuration (pyproject.toml, *.cfg, *.ini, .env, etc.)
      generated          — auto-generated files
      unknown            — unclassifiable
    """
    p = str(path)
    base = os.path.basename(p)

    # Docs
    if p.startswith("docs/") or base.endswith((".md", ".rst", ".txt")) and not p.startswith("src/"):
        return "docs"

    # Config
    if base in {"pyproject.toml", "setup.cfg", "setup.py", "Makefile", "Dockerfile"}:
        return "config"
    if base.endswith((".cfg", ".ini", ".toml", ".yaml", ".yml")) and not p.startswith("tests/"):
        return "config"

    # Test fixture
    if "fixture" in p.lower() or p.startswith("test_data/"):
        return "test_fixture"
    if p.startswith("tests/fixtures/"):
        # Under tests/fixtures but NOT a test file → fixture
        if not (base.startswith("test_") or base.endswith("_test.py")):
            return "test_fixture"

    # Test code
    if p.startswith("tests/") or "/test_" in p or base.startswith("test_") or base.endswith("_test.py"):
        return "test_code"

    # Generated
    if "generated" in p.lower() or "__pycache__" in p:
        return "generated"

    # Production source
    if any(p.startswith(prefix) for prefix in ("src/", "lib/", "codex_oss/", "app/", "pkg/")):
        return "production_source"

    # Heuristic: if it's a .py file not in tests/ and not a fixture, treat as source
    if base.endswith(".py"):
        return "production_source"

    return "unknown"


def classify_files_by_role(files: list[Any]) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}
    for file in files:
        path = str(getattr(file, "path", "") or "")
        role = classify_file_role(path)
        roles.setdefault(role, []).append(path)
    return roles


def review_removed_tests(removed_text: str) -> list[JSON]:
    findings = []
    for match in re.finditer(r"^\s*def\s+(test_[A-Za-z0-9_]*)\s*\(", removed_text, re.MULTILINE):
        name = match.group(1)
        findings.append({
            "code": "test_removal",
            "severity": "blocking",
            "detail": f"Patch removes existing test function {name}().",
            "location": {"function_name": name},
            "repairable": True,
            "repair_hint": f"Do not delete {name}(). Add new tests instead.",
        })
    return findings


def review_removed_functions(removed_text: str) -> list[JSON]:
    findings = []
    seen = set()
    for match in re.finditer(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", removed_text, re.MULTILINE):
        name = match.group(1)
        if name.startswith("test_"):
            continue
        if name in seen:
            continue
        seen.add(name)
        findings.append({
            "code": "function_deletion",
            "severity": "blocking",
            "detail": f"Patch deletes function {name}().",
            "location": {"function_name": name},
            "repairable": True,
            "repair_hint": f"Add new content after {name}() instead of deleting it.",
        })
    return findings


def review_dependency_changes(files: list[Any]) -> list[JSON]:
    findings = []
    for file in files:
        path = str(getattr(file, "path", "") or "")
        if _is_dependency_manifest(path):
            findings.append({
                "code": "dependency_change",
                "severity": "blocking",
                "detail": f"Dependency manifest change requires GPT review: {path}",
                "location": {"path": path},
                "repairable": False,
                "repair_hint": "",
            })
    return findings


def review_objective_alignment(
    files: list[Any],
    objective_spec: dict | None,
    mission: Any,
) -> JSON:
    spec = objective_spec or {}
    target = spec.get("target", {}) if isinstance(spec, dict) else {}
    changed_paths = {str(getattr(file, "path", "") or "") for file in files}
    objective_type = str(spec.get("objective_type", "") or "")

    required_changed = set(target.get("required_changed_files", []) or [])
    missing_required = [p for p in required_changed if p not in changed_paths]

    added_text = "\n".join(line for file in files for line in getattr(file, "added_lines", []))

    missing_symbols = []
    for symbol in target.get("required_symbols", []) or []:
        if not isinstance(symbol, dict):
            continue
        name = str(symbol.get("name", "") or "")
        path = str(symbol.get("path", "") or "")
        if name and not _added_symbol_present(added_text, symbol):
            missing_symbols.append({"path": path, "name": name, "kind": str(symbol.get("kind", ""))})

    missing_tests = []
    for name in (target.get("required_test_names", []) or []):
        if not re.search(rf"^\s*def\s+{re.escape(str(name))}\s*\(", added_text, re.MULTILINE):
            missing_tests.append(str(name))

    matches = not missing_required and not missing_symbols and not missing_tests

    return {
        "matches_objective": matches,
        "objective_type": objective_type,
        "missing_required_files": missing_required,
        "missing_required_symbols": missing_symbols,
        "missing_required_tests": missing_tests,
    }


def review_source_change_without_test(
    files: list[Any],
    mission: Any,
) -> list[JSON]:
    findings = []
    tier = str(getattr(mission, "tier", "") or "")
    if tier not in ("A5", "A6"):
        return findings
    spec = getattr(mission, "objective_spec", None)
    objective_type = str(spec.get("objective_type", "") or "") if isinstance(spec, dict) else ""
    if objective_type in {"critical_path_patch", "documentation_patch"}:
        return findings
    file_roles = classify_files_by_role(files)
    production_changed = file_roles.get("production_source", [])
    test_code_changed = file_roles.get("test_code", [])

    if production_changed and not test_code_changed:
        findings.append({
            "code": "source_change_without_test",
            "severity": "repairable" if tier == "A5" else "blocking",
            "detail": f"Source implementation change requires an accompanying test or verification caveat. Production files changed: {production_changed[:3]}.",
            "location": {"source_files": production_changed},
            "repairable": True,
            "repair_hint": "Add a test file change alongside the source change, or add a verification caveat.",
        })
    return findings


def review_patch_noop(files: list[Any]) -> list[JSON]:
    findings = []
    total_added = sum(len(getattr(f, "added_lines", []) or []) for f in files)
    total_removed = sum(len(getattr(f, "removed_lines", []) or []) for f in files)
    if total_added == 0 and total_removed == 0:
        findings.append({
            "code": "patch_noop",
            "severity": "blocking",
            "detail": "Patch produces no meaningful change (0 added lines, 0 removed lines).",
            "location": {},
            "repairable": True,
            "repair_hint": "Ensure the patch adds or modifies at least one line.",
        })
    added_text = "\n".join(line for file in files for line in getattr(file, "added_lines", []))
    if added_text.strip() and added_text.strip() == "\n".join(line for file in files for line in getattr(file, "removed_lines", [])):
        findings.append({
            "code": "patch_noop",
            "severity": "blocking",
            "detail": "Patch adds and removes identical content.",
            "location": {},
            "repairable": True,
            "repair_hint": "Check that the intended change is actually reflected in the diff.",
        })
    return findings


def build_semantic_review_report(
    proposal: JSON,
    files: list[Any],
    mission: Any,
) -> JSON:
    """Build a SemanticReviewReportV1 with structured reason codes."""
    spec = getattr(mission, "objective_spec", None)
    objective_spec = dict(spec) if isinstance(spec, dict) else {}
    removed_text = "\n".join(line for file in files for line in getattr(file, "removed_lines", []))
    changed_paths = {str(getattr(file, "path", "") or "") for file in files}

    findings: list[JSON] = []

    # Blocking
    findings.extend(review_removed_tests(removed_text))
    findings.extend(review_removed_functions(removed_text))
    findings.extend(review_dependency_changes(files))
    findings.extend(review_patch_noop(files))

    # Non-blocking
    findings.extend(review_source_change_without_test(files, mission))

    # Objective alignment
    alignment = review_objective_alignment(files, objective_spec, mission)
    if not alignment.get("matches_objective", True):
        findings.append({
            "code": "objective_mismatch",
            "severity": "blocking",
            "detail": f"Patch does not fully satisfy the objective spec. Missing files: {alignment.get('missing_required_files', [])}. Missing symbols: {alignment.get('missing_required_symbols', [])}. Missing tests: {alignment.get('missing_required_tests', [])}.",
            "location": {},
            "repairable": True,
            "repair_hint": "Ensure the patch satisfies all required_changed_files, required_symbols, and required_test_names from the objective spec.",
        })

    # Classify overall
    blocking_findings = [f for f in findings if f.get("severity") == "blocking"]
    repairable_findings = [f for f in findings if f.get("severity") == "repairable"]
    non_blocking_findings = [f for f in findings if f.get("severity") == "non_blocking"]

    if blocking_findings:
        decision = "INVALID"
    elif repairable_findings:
        decision = "REPAIRABLE"
    else:
        decision = "VALID"

    repairable = bool(repairable_findings) and not blocking_findings

    return {
        "semantic_review_version": "1.0",
        "decision": decision,
        "repairable": repairable,
        "blocking_count": len(blocking_findings),
        "repairable_count": len(repairable_findings),
        "non_blocking_count": len(non_blocking_findings),
        "findings": findings,
        "blocking_findings": blocking_findings,
        "repairable_findings": repairable_findings,
        "non_blocking_findings": non_blocking_findings,
        "objective_alignment": alignment,
        "file_roles": classify_files_by_role(files),
        "recommended_repair": _suggested_repair(findings) if repairable else None,
    }


def _suggested_repair(findings: list[JSON]) -> JSON | None:
    repairable = [f for f in findings if f.get("repairable")]
    if not repairable:
        return None
    return {
        "priority_finding": repairable[0].get("code", ""),
        "instruction": repairable[0].get("repair_hint", ""),
        "total_repairable_findings": len(repairable),
    }


def _is_dependency_manifest(path: str) -> bool:
    base = os.path.basename(path)
    return base in {
        "package.json", "pnpm-lock.yaml", "package-lock.json",
        "requirements.txt", "pyproject.toml", "poetry.lock",
    }


def _added_symbol_present(added_text: str, symbol: dict) -> bool:
    name = str(symbol.get("name", "") or "")
    kind = str(symbol.get("kind", "function") or "function")
    if not name:
        return True
    if kind in ("function", "method"):
        return bool(re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", added_text, re.MULTILINE))
    if kind == "class":
        return bool(re.search(rf"^\s*class\s+{re.escape(name)}\b", added_text, re.MULTILINE))
    return name in added_text
