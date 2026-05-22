#!/usr/bin/env python3
r"""A5 staged burn-in ladder — DesiredState -> PatchRecipe -> PatchIntent.

Run:
  set -a; source .codex-oss/env/opencode-go.env; set +a; LIVE_BURNIN=1 python3 tests/test_a5_ladder_burnin.py

Stages:
  A5.0  Runtime-build exact content (predetermined patch)
  A5.1  DesiredStateV1 fixture change
  A5.2  PatchRecipeV1 anchor fill
  A5.3  PatchIntentV1 structured intent
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
A4_MODEL = os.getenv("LIVE_A4A5_A4_MODEL", "mission-a3-kimi")
A5_MODEL = os.getenv("LIVE_A4A5_A5_MODEL", "mission-a3-kimi")


@dataclass
class LadderCase:
    name: str
    stage: str
    tier: str
    model: str
    mission: dict
    expected_status: str


@dataclass
class LadderResult:
    case: str = ""
    stage: str = ""
    status: str = ""
    proposal_source: str = ""
    runtime_built: bool = False
    semantic_decision: str = ""
    semantic_blocking_count: int = 0
    main_mutated: bool = False
    rollback_available: bool = False
    passed: bool = False
    elapsed: float = 0


def call_bridge(mission: dict, model: str) -> dict:
    body = {
        "model": model,
        "stream": False,
        "input": [
            {"role": "user", "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>"},
        ],
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {AUTH}"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{BASE_URL}/responses", data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(5)
            else:
                return {"error": str(e)[:200], "bridge_unreachable": True}
    return {"error": "all attempts failed"}


def extract_text(payload: dict) -> str:
    parts = []
    for output in payload.get("output", []) or []:
        for item in output.get("content", []) or []:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
    return "\n".join(parts)


def extract_metrics(response: dict) -> LadderResult:
    result = LadderResult()
    content = extract_text(response) if isinstance(response, dict) else str(response)
    if not content:
        return result

    if "OSS_PATCH_VALIDATION_BEGIN" in content:
        result.status = "VALID" if "Status: VALID" in content else (
            "ESCALATE" if "Status: ESCALATE" in content else "INVALID"
        )
        result.proposal_source = _extract_field(content, "Proposal source:")
    elif "OSS_IMPLEMENTATION_REPORT_BEGIN" in content:
        if "Status: VERIFIED" in content:
            result.status = "VERIFIED"
        elif "Status: FAILED" in content:
            result.status = "FAILED"
        elif "Status: ESCALATE" in content:
            result.status = "ESCALATE"
        result.main_mutated = "Main workspace mutated: true" in content
        result.rollback_available = "rollback" in content.lower() and "artifact" in content.lower()
        result.proposal_source = _extract_field(content, "Proposal source:")

    result.runtime_built = '"runtime_built_diff": true' in content.lower()

    # Extract semantic review details from JSON section
    if '"semantic_review"' in content:
        try:
            base = _find_json_block(content)
            if base:
                sr_data = base.get("semantic_review", {}) if isinstance(base, dict) else {}
                if isinstance(sr_data, dict):
                    result.semantic_decision = str(sr_data.get("decision", ""))
                    result.semantic_blocking_count = int(sr_data.get("blocking_count", 0))
        except Exception:
            pass

    return result


def _extract_field(content: str, field: str) -> str:
    for line in content.split("\n"):
        if line.startswith(field):
            return line.split(":", 1)[1].strip()
    return ""


def _find_json_block(content: str) -> dict | None:
    markers = ["OSS_PATCH_VALIDATION_JSON:", "OSS_IMPLEMENTATION_REPORT_JSON:"]
    for marker in markers:
        if marker in content:
            try:
                start = content.index(marker) + len(marker)
                _, rest = content[start:].split("{", 1)
                depth = 0
                end = 0
                for i, c in enumerate(rest, 1):
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        if depth == 0:
                            end = i
                            break
                        depth -= 1
                return json.loads("{" + rest[:end])
            except (ValueError, json.JSONDecodeError):
                pass
    return None


def build_ladder_cases() -> list[LadderCase]:
    cases: list[LadderCase] = []

    # ── A5.0: Runtime-build exact content ──
    # The mission specifies EXACTLY what to write. Model role is minimal.
    cases.append(LadderCase(
        name="ladder_a50_exact_marker_add",
        stage="A5.0",
        tier="A5",
        model=A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "ladder_a50_marker",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Add a comment '# A5.0 Ladder marker' at the end of tests/fixtures/a4_http_target.py. Do not modify existing code.",
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
            "time_budget_seconds": 180,
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
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "documentation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/a4_http_target.py"],
                    "required_source_files": ["tests/fixtures/a4_http_target.py"],
                },
                "required_outputs": ["changed_source_file"],
                "required_evidence_shapes": ["source_change"],
            },
        },
        expected_status="VERIFIED",
    ))

    # ── A5.1: DesiredStateV1 fixture change ──
    cases.append(LadderCase(
        name="ladder_a51_desired_state_comment",
        stage="A5.1",
        tier="A5",
        model=A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "ladder_a51_desired",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Ensure tests/fixtures/a4_http_target.py contains a comment '# A5.1 Ladder marker' near the top. Use DesiredStateV1: the file should contain this comment. Return DesiredStateV1, not a raw diff.",
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
            "time_budget_seconds": 180,
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
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "documentation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/a4_http_target.py"],
                    "required_source_files": ["tests/fixtures/a4_http_target.py"],
                },
                "required_outputs": ["changed_source_file"],
                "required_evidence_shapes": ["source_change"],
            },
        },
        expected_status="VERIFIED",
    ))

    # ── A5.2: PatchRecipeV1 anchor fill ──
    cases.append(LadderCase(
        name="ladder_a52_recipe_anchor",
        stage="A5.2",
        tier="A5",
        model=A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "ladder_a52_recipe",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Insert a comment '# A5.2 Ladder marker' after the describe() function definition in tests/fixtures/a4_http_target.py. Use PatchRecipeV1 with anchor 'def describe'. Do not modify any existing code.",
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
            "time_budget_seconds": 180,
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
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "documentation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/a4_http_target.py"],
                    "required_source_files": ["tests/fixtures/a4_http_target.py"],
                },
                "required_outputs": ["changed_source_file"],
                "required_evidence_shapes": ["source_change"],
            },
        },
        expected_status="VERIFIED",
    ))

    # ── A5.3: PatchIntentV1 structured intent ──
    cases.append(LadderCase(
        name="ladder_a53_patch_intent",
        stage="A5.3",
        tier="A5",
        model=A5_MODEL,
        mission={
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "ladder_a53_intent",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Add a comment '# A5.3 Ladder marker' at the end of tests/fixtures/a4_http_target.py. Use PatchIntentV1 — specify the target file and the desired change, but do not return a raw diff.",
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
            "time_budget_seconds": 180,
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
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "documentation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/a4_http_target.py"],
                    "required_source_files": ["tests/fixtures/a4_http_target.py"],
                },
                "required_outputs": ["changed_source_file"],
                "required_evidence_shapes": ["source_change"],
            },
        },
        expected_status="VERIFIED",
    ))

    return cases


def run_ladder(cases: list[LadderCase], limit: int = 25, start: int = 1):
    results: list[LadderResult] = []
    total = min(limit, len(cases))

    print(f"\nA5 Staged Burn-In Ladder: {total} cases ({len(cases)} defined)")
    print(f"Bridge: {BASE_URL}")
    print(f"A4 Model: {A4_MODEL}")
    print(f"A5 Model: {A5_MODEL}")
    print(f"Timeout: {TIMEOUT}s")
    print("-" * 60)

    for idx, case in enumerate(cases[start - 1 : start - 1 + total], start=start):
        mission = dict(case.mission)
        mission["mission_id"] = f"{mission['mission_id']}_{idx}"

        print(f"\n[{idx}/{total}] {case.name} ({case.stage})")
        start_time = time.time()
        response = call_bridge(mission, case.model)
        elapsed = time.time() - start_time

        if response.get("bridge_unreachable"):
            print(f"  SKIP: Bridge unreachable")
            continue
        if response.get("error"):
            print(f"  ERROR: {str(response.get('error', ''))[:120]}")
            results.append(LadderResult(case=case.name, stage=case.stage, passed=False, elapsed=elapsed))
            continue

        result = extract_metrics(response)
        result.case = case.name
        result.stage = case.stage
        result.elapsed = elapsed
        result.passed = result.status in {case.expected_status}

        print(f"  Status: {result.status} | Expected: {case.expected_status} | "
              f"Source: {result.proposal_source} | RuntimeDiff: {result.runtime_built} | "
              f"SemanticDecision: {result.semantic_decision} | "
              f"BlockingCount: {result.semantic_blocking_count} | "
              f"MainMutated: {result.main_mutated} | "
              f"Elapsed: {elapsed:.1f}s")
        if result.main_mutated:
            print(f"  WARNING: main workspace mutated!")
        if not result.passed:
            print(f"  EXPECTED: {case.expected_status}, GOT: {result.status}")

        results.append(result)

    print("\n" + "=" * 60)
    by_stage: dict[str, list] = {}
    for r in results:
        by_stage.setdefault(r.stage, []).append(r)

    print(f"Results: {len(results)} total")
    for stage in sorted(by_stage):
        stage_results = by_stage[stage]
        passed = len([r for r in stage_results if r.passed])
        print(f"  {stage}: {passed}/{len(stage_results)}")
        for r in stage_results:
            print(f"    {r.case}: {r.status} (semantic={r.semantic_decision} blocks={r.semantic_blocking_count})")

    return results


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part / total * 100:.0f}%"


def main():
    if not os.getenv("LIVE_BURNIN"):
        print("Skipping live burn-in (set LIVE_BURNIN=1 to run).")
        return 0

    cases = build_ladder_cases()
    limit = int(os.getenv("OSS_LIVE_BURNIN_LIMIT", "25"))
    start = int(os.getenv("OSS_LIVE_BURNIN_START_INDEX", "1"))
    results = run_ladder(cases, limit=limit, start=start)

    passed = len([r for r in results if r.passed])
    if len(results) == 0:
        print("\nLADDER SKIPPED: No results")
        return 2

    if passed / len(results) >= 0.5:
        print(f"\nLADDER PASSED: {passed}/{len(results)} ({_pct(passed, len(results))})")
        return 0
    else:
        print(f"\nLADDER FAILED: only {_pct(passed, len(results))} passed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
