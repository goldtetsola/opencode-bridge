#!/usr/bin/env python3
r"""A4/A5 implementation burn-in pack — patch proposal and isolated apply.

Run:
  set -a; source .codex-oss/env/opencode-go.env; set +a; LIVE_BURNIN=1 python3 tests/test_a4a5_burnin_pack.py

Note: This bridge has A3 model aliases available. A4/A5 dedicated model profiles
(mission-a4-kimi, mission-a5-kimi) require a dedicated bridge instance.
This pack uses mission-a3-kimi as fallback for validation testing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

ROOT = os.path.dirname(os.path.dirname(__file__))
BASE_URL = os.getenv("OSS_LIVE_BURNIN_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
AUTH = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"
TIMEOUT = float(os.getenv("OSS_LIVE_BURNIN_TIMEOUT", "180"))
# Use A3 model aliases — the bridge doesn't have A4/A5 aliases registered
DEFAULT_A4_MODEL = os.getenv("LIVE_A4A5_A4_MODEL", "mission-a3-kimi")
DEFAULT_A5_MODEL = os.getenv("LIVE_A4A5_A5_MODEL", "mission-a3-kimi")


@dataclass
class BurninCase:
    name: str
    tier: str
    model: str
    mission: dict
    expected_status: str  # VALID, INVALID, VERIFIED, FAILED, ESCALATE


@dataclass
class BurninResult:
    case: str = ""
    tier: str = ""
    status: str = ""
    patch_valid: bool = False
    applies_cleanly: bool = False
    readiness_ok: bool = False
    coverage_ok: bool = False
    runtime_built_diff: bool = False
    main_workspace_mutated: bool = False
    rollback_available: bool = False
    passed: bool = False
    elapsed: float = 0
    output_preview: str = ""


def call_bridge(mission: dict, model: str) -> dict:
    body = {
        "model": model,
        "stream": False,
        "input": [
            {"role": "user", "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>"},
        ],
    }
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AUTH}",
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{BASE_URL}/responses",
                data=data, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": f"HTTP {e.code}: {err_body}", "bridge_unreachable": True}
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": str(e)[:200], "bridge_unreachable": True}
    return {"error": "all attempts failed", "bridge_unreachable": True}


def extract_text(payload: dict) -> str:
    parts = []
    for output in payload.get("output", []) or []:
        for item in output.get("content", []) or []:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
    return "\n".join(parts)


def extract_metrics(response: dict) -> BurninResult:
    result = BurninResult()
    content = extract_text(response) if isinstance(response, dict) else str(response)
    if not content:
        return result
    result.output_preview = content[:300]

    if "OSS_PATCH_VALIDATION_BEGIN" in content:
        result.status = "VALID" if "Status: VALID" in content else (
            "ESCALATE" if "Status: ESCALATE" in content else "INVALID"
        )
    elif "OSS_IMPLEMENTATION_REPORT_BEGIN" in content:
        if "Status: VERIFIED" in content:
            result.status = "VERIFIED"
        elif "Status: FAILED" in content:
            result.status = "FAILED"
        elif "Status: ESCALATE" in content:
            result.status = "ESCALATE"
        else:
            result.status = "UNKNOWN"
        result.main_workspace_mutated = "Main workspace mutated: true" in content
        result.rollback_available = "rollback" in content.lower() and "artifact" in content.lower()

    result.patch_valid = result.status == "VALID"
    result.applies_cleanly = '"applies_cleanly": true' in content.lower() or '"applies_cleanly": True' in content
    result.readiness_ok = '"implementation_readiness_ok": true' in content.lower() or '"implementation_readiness_ok": True' in content
    result.coverage_ok = '"implementation_coverage_ok": true' in content.lower() or '"implementation_coverage_ok": True' in content
    result.runtime_built_diff = '"runtime_built_diff": true' in content.lower() or '"runtime_built_diff": True' in content

    return result


def build_burnin_cases() -> list[BurninCase]:
    cases: list[BurninCase] = []

    # A4: Simple test-only patch (add docstring to test fixture)
    cases.append(BurninCase(
        name="a4_raw_patch_docstring",
        tier="A4",
        model=DEFAULT_A4_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "burnin_a4_docstring",
            "tier": "A4",
            "mode": "patch_proposal",
            "objective": "Add a docstring to describe() in tests/fixtures/a4_http_target.py. Only change the docstring — do not modify the function body or return value.",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": ["tests/fixtures/a4_http_target.py"],
            "owned_paths": ["tests/fixtures/a4_http_target.py"],
            "read_only_paths": ["tests/fixtures/a4_http_target.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read"],
            "tool_budget": 4,
            "time_budget_seconds": 120,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "patch_validation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/fixtures/a4_http_target.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        },
        expected_status="VALID",
    ))

    # A4: Test-only patch with patch_intent
    cases.append(BurninCase(
        name="a4_patch_intent_test_add",
        tier="A4",
        model=DEFAULT_A4_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "burnin_a4_patch_intent",
            "tier": "A4",
            "mode": "patch_proposal",
            "objective": "Add a new test function test_a4_live_empty_string to tests/test_a4_live_fixture_behavior.py. The test should call describe() and assert the result is a string. Use PatchIntentV1 — do NOT return a raw diff.",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": [
                "tests/fixtures/a4_http_target.py",
                "tests/test_a4_live_fixture_behavior.py",
            ],
            "owned_paths": ["tests/test_a4_live_fixture_behavior.py"],
            "read_only_paths": [
                "tests/fixtures/a4_http_target.py",
                "tests/test_a4_live_fixture_behavior.py",
            ],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read"],
            "tool_budget": 5,
            "time_budget_seconds": 120,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "patch_validation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_a4_live_fixture_behavior.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_test_only",
                "target": {
                    "required_changed_files": ["tests/test_a4_live_fixture_behavior.py"],
                    "required_test_files": ["tests/test_a4_live_fixture_behavior.py"],
                    "required_test_names": ["test_a4_live_empty_string"],
                    "source_files": ["tests/fixtures/a4_http_target.py"],
                },
                "required_outputs": ["changed_test_file"],
                "required_evidence_shapes": ["test_definition"],
                "completion_criteria": ["required_tests_present"],
            },
        },
        expected_status="VALID",
    ))

    # A4: Additional test case with implementation coverage check
    cases.append(BurninCase(
        name="a4_raw_patch_with_coverage",
        tier="A4",
        model=DEFAULT_A4_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "burnin_a4_coverage",
            "tier": "A4",
            "mode": "patch_proposal",
            "objective": "Add a comment '# Coverage test marker' to the top of tests/fixtures/a4_http_target.py. Do not modify any existing code. Just add the comment.",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": ["tests/fixtures/a4_http_target.py"],
            "owned_paths": ["tests/fixtures/a4_http_target.py"],
            "read_only_paths": ["tests/fixtures/a4_http_target.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read"],
            "tool_budget": 5,
            "time_budget_seconds": 120,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "patch_validation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/fixtures/a4_http_target.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/a4_http_target.py"],
                    "required_source_files": ["tests/fixtures/a4_http_target.py"],
                },
                "required_outputs": ["changed_source_file"],
                "required_evidence_shapes": ["source_change"],
                "completion_criteria": ["required_symbols_present"],
            },
        },
        expected_status="VALID",
    ))

    # A5: Isolated apply — add a docstring and verify compilation
    cases.append(BurninCase(
        name="a5_isolated_apply_docstring",
        tier="A5",
        model=DEFAULT_A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "burnin_a5_isolated",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Apply a bounded docstring change to describe() in tests/fixtures/a4_http_target.py. Verify the file still compiles after the change. Do not mutate any other files.",
            "risk_tier": "low",
            "write_allowed": True,
            "apply_mode": "isolated_worktree",
            "allowed_roots": [],
            "allowed_paths": ["tests/fixtures/a4_http_target.py"],
            "owned_paths": ["tests/fixtures/a4_http_target.py"],
            "read_only_paths": ["tests/fixtures/a4_http_target.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read"],
            "tool_budget": 5,
            "time_budget_seconds": 120,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/fixtures/a4_http_target.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        },
        expected_status="VERIFIED",
    ))

    # A5: Isolated apply with specific symbol constraint
    cases.append(BurninCase(
        name="a5_isolated_apply_symbol",
        tier="A5",
        model=DEFAULT_A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "burnin_a5_symbol",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Add a comment '# A5 burn-in marker' to the top of tests/fixtures/a4_live_string_utils.py. Apply in isolation and verify compilation. Do not modify existing code.",
            "risk_tier": "low",
            "write_allowed": True,
            "apply_mode": "isolated_worktree",
            "allowed_roots": [],
            "allowed_paths": ["tests/fixtures/a4_live_string_utils.py"],
            "owned_paths": ["tests/fixtures/a4_live_string_utils.py"],
            "read_only_paths": ["tests/fixtures/a4_live_string_utils.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read"],
            "tool_budget": 5,
            "time_budget_seconds": 180,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/fixtures/a4_live_string_utils.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/a4_live_string_utils.py"],
                    "required_source_files": ["tests/fixtures/a4_live_string_utils.py"],
                },
                "required_outputs": ["changed_source_file"],
                "required_evidence_shapes": ["source_change"],
                "completion_criteria": ["required_symbols_present"],
            },
        },
        expected_status="VERIFIED",
    ))

    # A5 with implementation coverage check
    cases.append(BurninCase(
        name="a5_isolated_apply_coverage",
        tier="A5",
        model=DEFAULT_A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "burnin_a5_coverage",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Add a comment '# A5 test marker' to the top of tests/test_a4_live_string_utils.py. Apply in isolation and verify compilation. Do not modify existing tests.",
            "risk_tier": "low",
            "write_allowed": True,
            "apply_mode": "isolated_worktree",
            "allowed_roots": [],
            "allowed_paths": [
                "tests/fixtures/a4_live_string_utils.py",
                "tests/test_a4_live_string_utils.py",
            ],
            "owned_paths": ["tests/test_a4_live_string_utils.py"],
            "read_only_paths": [
                "tests/fixtures/a4_live_string_utils.py",
                "tests/test_a4_live_string_utils.py",
            ],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read"],
            "tool_budget": 5,
            "time_budget_seconds": 180,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_a4_live_string_utils.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_test_only",
                "target": {
                    "required_changed_files": ["tests/test_a4_live_string_utils.py"],
                    "required_test_files": ["tests/test_a4_live_string_utils.py"],
                },
                "required_outputs": ["changed_test_file"],
                "required_evidence_shapes": ["source_change"],
                "completion_criteria": ["required_tests_present"],
            },
        },
        expected_status="VERIFIED",
    ))

    return cases


def run_burnin(cases: list[BurninCase], limit: int = 25, start: int = 1):
    results: list[BurninResult] = []
    total = min(limit, len(cases))

    print(f"\nA4/A5 Implementation Burn-In: {total} cases ({len(cases)} defined)")
    print(f"Bridge: {BASE_URL}")
    print(f"Timeout: {TIMEOUT}s")
    print("-" * 60)

    for idx, case in enumerate(cases[start - 1 : start - 1 + total], start=start):
        mission = dict(case.mission)
        mission["mission_id"] = f"{mission['mission_id']}_{idx}"

        print(f"\n[{idx}/{total}] {case.name} ({case.tier})")
        start_time = time.time()
        response = call_bridge(mission, case.model)
        elapsed = time.time() - start_time

        if response.get("bridge_unreachable"):
            print(f"  SKIP: Bridge unreachable")
            continue

        if response.get("error"):
            print(f"  ERROR: {str(response.get('error', ''))[:120]}")
            results.append(BurninResult(case=case.name, tier=case.tier, passed=False, elapsed=elapsed))
            continue

        result = extract_metrics(response)
        result.case = case.name
        result.tier = case.tier
        result.elapsed = elapsed
        result.passed = result.status in {case.expected_status}

        print(f"  Status: {result.status} | Expected: {case.expected_status} | "
              f"PatchValid: {result.patch_valid} | Readiness: {result.readiness_ok} | "
              f"Coverage: {result.coverage_ok} | RuntimeDiff: {result.runtime_built_diff} | "
              f"Elapsed: {elapsed:.1f}s")
        if result.main_workspace_mutated:
            print(f"  WARNING: main workspace mutated!")
        if not result.passed:
            preview = result.output_preview[:200].replace("\n", "\\n")
            print(f"  Preview: {preview}")

        results.append(result)

    print("\n" + "=" * 60)
    passed = [r for r in results if r.passed]
    by_tier: dict[str, list] = {}
    for r in results:
        by_tier.setdefault(r.tier, []).append(r)

    print(f"Results: {len(results)} total")
    print(f"  Passed: {len(passed)}/{len(results)} ({_pct(len(passed), len(results))})")
    for tier in ["A4", "A5", "A6"]:
        tier_results = by_tier.get(tier, [])
        tier_passed = len([r for r in tier_results if r.passed])
        print(f"  {tier}: {tier_passed}/{len(tier_results)}")

    covered = len([r for r in results if r.coverage_ok])
    print(f"  Coverage ok: {covered}/{len(results)} ({_pct(covered, len(results))})")

    return results


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part / total * 100:.0f}%"


def main():
    if not os.getenv("LIVE_BURNIN"):
        print("Skipping live burn-in (set LIVE_BURNIN=1 to run).")
        print("Run: set -a; source .codex-oss/env/opencode-go.env; set +a; LIVE_BURNIN=1 python3 tests/test_a4a5_burnin_pack.py")
        return 0

    cases = build_burnin_cases()
    limit = int(os.getenv("OSS_LIVE_BURNIN_LIMIT", "25"))
    start = int(os.getenv("OSS_LIVE_BURNIN_START_INDEX", "1"))
    results = run_burnin(cases, limit=limit, start=start)

    passed = len([r for r in results if r.passed])
    if len(results) == 0:
        print("\nBURN-IN SKIPPED: No results collected")
        return 2

    if passed / len(results) >= 0.66:
        print(f"\nBURN-IN PASSED: {passed}/{len(results)} ({_pct(passed, len(results))})")
        return 0
    else:
        print(f"\nBURN-IN FAILED: only {_pct(passed, len(results))} passed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
