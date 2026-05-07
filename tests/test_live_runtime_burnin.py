#!/usr/bin/env python3
"""Opt-in live burn-in for runtime-backed OSS agents.

This test intentionally talks to a running local bridge and a real upstream OSS
provider. It is skipped by default so the public repo stays CI-safe.

Run locally:
  OSS_LIVE_BURNIN=1 python3 tests/test_live_runtime_burnin.py
"""

from __future__ import annotations

import json
import http.client
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


BASE_URL = os.getenv("OSS_LIVE_BURNIN_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
AUTH = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"
TIMEOUT = float(os.getenv("OSS_LIVE_BURNIN_TIMEOUT", "180"))
CAPACITY_RETRIES = int(os.getenv("OSS_LIVE_BURNIN_CAPACITY_RETRIES", "30"))
TRANSPORT_RETRIES = int(os.getenv("OSS_LIVE_BURNIN_TRANSPORT_RETRIES", "3"))
SERVER_RECOVERY_TIMEOUT = float(os.getenv("OSS_LIVE_BURNIN_SERVER_RECOVERY_TIMEOUT", "30"))
START_INDEX = int(os.getenv("OSS_LIVE_BURNIN_START_INDEX", "1"))
LIMIT = int(os.getenv("OSS_LIVE_BURNIN_LIMIT", "25"))

REQUIRED_OUTPUTS = [
    "files_inspected",
    "commands_run",
    "findings",
    "uncertainties",
    "confidence",
    "caveats",
    "escalation_recommendation",
]


@dataclass
class LiveCase:
    name: str
    model: str
    mission: dict
    expected_tokens: list[str]


def mission_base(name: str, tier: str, objective: str, allowed_path: str, objective_spec: dict,
                 tools: list[str] | None = None, budget: int = 6) -> dict:
    return {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": f"mission_live_burnin_{name}",
        "tier": tier,
        "mode": "guided_exploration" if tier == "A2" else "managed_investigation",
        "objective": objective,
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": [],
        "allowed_paths": [allowed_path],
        "allowed_tool_classes": tools or ["search", "read"],
        "tool_budget": budget,
        "time_budget_seconds": 60 if tier == "A2" else 120,
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": REQUIRED_OUTPUTS,
        "allow_heuristic_objective": False,
        "objective_spec": objective_spec,
    }


def function_case(name: str, model: str, tier: str, symbol: str, path: str) -> LiveCase:
    return LiveCase(
        name=name,
        model=model,
        mission=mission_base(
            name=name,
            tier=tier,
            objective=f"Find where {symbol} is defined and return the file path.",
            allowed_path=path,
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "function_location",
                "target": {"symbol": symbol},
                "required_outputs": ["file_path", "symbol_name", "evidence_ref"],
                "required_evidence_shapes": ["function_definition"],
            },
        ),
        expected_tokens=["Status: COMPLETE", symbol, path],
    )


def mapping_case(name: str, model: str, tier: str, alias: str, expected: str) -> LiveCase:
    path = "codex_oss/managed_bridge.py"
    return LiveCase(
        name=name,
        model=model,
        mission=mission_base(
            name=name,
            tier=tier,
            objective=f"Find the runtime model alias mapping for {alias}.",
            allowed_path=path,
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "mapping_lookup",
                "target": {"key": alias},
                "required_outputs": ["mapped_value", "mapping_file", "evidence_ref"],
                "required_evidence_shapes": ["mapping_assignment"],
            },
        ),
        expected_tokens=["Status: COMPLETE", alias, expected],
    )


def config_case(name: str, model: str, tier: str, alias: str, budget: str, seconds: str) -> LiveCase:
    path = "codex_oss/managed_bridge.py"
    return LiveCase(
        name=name,
        model=model,
        mission=mission_base(
            name=name,
            tier=tier,
            objective=f"Find the runtime autonomy profile limits for {alias}.",
            allowed_path=path,
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "config_value_extraction",
                "target": {"key": alias},
                "required_values": ["max_tool_budget", "max_time_seconds"],
                "required_outputs": ["file_path", "max_tool_budget", "max_time_seconds", "evidence_ref"],
                "required_evidence_shapes": ["dictionary_entry", "literal_value"],
            },
            budget=8,
        ),
        expected_tokens=["Status: COMPLETE", alias, f"max_tool_budget={budget}", f"max_time_seconds={seconds}"],
    )


def zero_case(name: str, model: str, tier: str, pattern: str, path: str) -> LiveCase:
    return LiveCase(
        name=name,
        model=model,
        mission=mission_base(
            name=name,
            tier=tier,
            objective=f"Confirm zero-match evidence for {pattern}.",
            allowed_path=path,
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "zero_match_evidence",
                "target": {"pattern": pattern},
                "required_outputs": ["searched_paths", "zero_match_result", "evidence_ref"],
                "required_evidence_shapes": ["grep_zero_match"],
            },
            tools=["search"],
            budget=3,
        ),
        expected_tokens=["Status: COMPLETE", pattern],
    )


def live_cases() -> list[LiveCase]:
    return [
        function_case("flash_function_validate_report", "mission-a2-flash", "A2", "validate_report", "codex_oss/validation/__init__.py"),
        function_case("kimi_function_run_loop", "mission-a3-kimi", "A3", "run_loop", "codex_oss/runtime/loop.py"),
        function_case("flash_function_parse_action", "mission-a2-flash", "A2", "parse_action", "codex_oss/runtime/loop.py"),
        mapping_case("kimi_mapping_a3_kimi", "mission-a3-kimi", "A3", "mission-a3-kimi", "ocg-kimi-k2.6"),
        mapping_case("deepseek_mapping_a3_deepseek", "mission-a3-deepseek", "A3", "mission-a3-deepseek", "ocg-deepseek-v4-pro"),
        mapping_case("flash_mapping_a2_flash", "mission-a2-flash", "A2", "mission-a2-flash", "ocg-deepseek-v4-flash"),
        config_case("deepseek_config_a3_deepseek", "mission-a3-deepseek", "A3", "mission-a3-deepseek", "20", "180"),
        config_case("kimi_config_a3_kimi", "mission-a3-kimi", "A3", "mission-a3-kimi", "14", "150"),
        config_case("flash_config_a2_flash", "mission-a2-flash", "A2", "mission-a2-flash", "8", "90"),
        zero_case("flash_zero_validation", "mission-a2-flash", "A2", "definitely_not_a_real_runtime_symbol", "codex_oss/validation/__init__.py"),
        zero_case("kimi_zero_loop", "mission-a3-kimi", "A3", "definitely_not_a_loop_symbol", "codex_oss/runtime/loop.py"),
        zero_case("deepseek_zero_bridge", "mission-a3-deepseek", "A3", "definitely_not_a_bridge_symbol", "codex_oss/managed_bridge.py"),
        function_case("kimi_function_resolve_path", "mission-a3-kimi", "A3", "resolve_path", "codex_oss/runtime/__init__.py"),
        function_case("flash_function_check_allowed_paths", "mission-a2-flash", "A2", "check_allowed_paths", "codex_oss/runtime/policy.py"),
        function_case("deepseek_function_critical_read", "mission-a3-deepseek", "A3", "critical_read_allowed_for_path", "codex_oss/runtime/policy.py"),
        function_case("kimi_function_partial_report", "mission-a3-kimi", "A3", "build_deterministic_partial_report", "codex_oss/runtime/policy.py"),
        function_case("flash_function_synthesize_objective", "mission-a2-flash", "A2", "synthesize_objective_finding", "codex_oss/runtime/objectives.py"),
        function_case("deepseek_function_classify_objective", "mission-a3-deepseek", "A3", "classify_objective", "codex_oss/runtime/objectives.py"),
        function_case("kimi_function_extract_model_text", "mission-a3-kimi", "A3", "_extract_model_text", "codex_oss/runtime/loop.py"),
        mapping_case("flash_mapping_a3_flash", "mission-a2-flash", "A2", "mission-a3-flash", "ocg-deepseek-v4-flash"),
        config_case("flash_config_a3_flash", "mission-a2-flash", "A2", "mission-a3-flash", "8", "90"),
        function_case("deepseek_function_managed_mission", "mission-a3-deepseek", "A3", "run_managed_mission_from_body", "codex_oss/managed_bridge.py"),
        function_case("kimi_function_runtime_deadline", "mission-a3-kimi", "A3", "_managed_runtime_deadline", "codex_oss/managed_bridge.py"),
        function_case("deepseek_function_autonomy_profile", "mission-a3-deepseek", "A3", "_apply_runtime_autonomy_profile", "codex_oss/managed_bridge.py"),
        zero_case("flash_zero_objectives_eval", "mission-a2-flash", "A2", "eval(", "codex_oss/runtime/objectives.py"),
    ]


def call_bridge(case: LiveCase) -> str:
    body = {
        "model": case.model,
        "stream": False,
        "input": [
            {
                "role": "user",
                "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(case.mission) + "\n</OSS_HANDOFF_JSON>",
            }
        ],
    }
    request = urllib.request.Request(
        f"{BASE_URL}/responses",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {AUTH}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "completed":
        return json.dumps(payload, indent=2)
    return extract_text(payload)


def call_bridge_with_capacity_retry(case: LiveCase) -> str:
    last_text = ""
    for attempt in range(CAPACITY_RETRIES + 1):
        last_text = call_bridge_with_transport_retry(case)
        if "global mission concurrency limit reached" not in last_text:
            return last_text
        if attempt < CAPACITY_RETRIES:
            time.sleep(min(10, 2 + attempt))
    return last_text


def call_bridge_with_transport_retry(case: LiveCase) -> str:
    last_exc: Exception | None = None
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            wait_for_health(timeout=SERVER_RECOVERY_TIMEOUT)
            return call_bridge(case)
        except (TimeoutError, urllib.error.URLError, http.client.RemoteDisconnected, ConnectionResetError) as exc:
            last_exc = exc
            if attempt >= TRANSPORT_RETRIES:
                raise
            wait_for_health(timeout=SERVER_RECOVERY_TIMEOUT)
            time.sleep(1 + attempt)
    raise RuntimeError(f"unreachable transport retry state: {last_exc}")


def extract_text(payload: dict) -> str:
    parts = []
    for output in payload.get("output", []) or []:
        for item in output.get("content", []) or []:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
    return "\n".join(parts)


def health_ok() -> bool:
    try:
        request = urllib.request.Request(f"{BASE_URL.removesuffix('/v1')}/health")
        request.add_header("Authorization", f"Bearer {AUTH}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def wait_for_health(timeout: float = SERVER_RECOVERY_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_ok():
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    if os.getenv("OSS_LIVE_BURNIN") != "1":
        print("SKIP: set OSS_LIVE_BURNIN=1 to run live runtime-backed OSS burn-in")
        return 0
    if not wait_for_health():
        print(f"FAIL: bridge is not healthy at {BASE_URL}", file=sys.stderr)
        return 1

    all_cases = live_cases()
    selected = all_cases[START_INDEX - 1:START_INDEX - 1 + LIMIT]
    failures = []
    started = time.time()
    print("Live runtime-backed OSS burn-in")
    print("==============================")
    for local_idx, case in enumerate(selected, start=1):
        idx = START_INDEX + local_idx - 1
        try:
            wait_for_health()
            text = call_bridge_with_capacity_retry(case)
            missing = [token for token in case.expected_tokens if token not in text]
            if missing:
                failures.append((case.name, f"missing tokens: {missing}", text))
                print(f"FAIL {idx:02d}/{len(all_cases):02d} {case.name} model={case.model} missing={missing}", flush=True)
            else:
                print(f"PASS {idx:02d}/{len(all_cases):02d} {case.name} model={case.model}", flush=True)
        except (TimeoutError, urllib.error.URLError, http.client.RemoteDisconnected, ConnectionResetError) as exc:
            failures.append((case.name, f"request failed: {exc}", ""))
            print(f"FAIL {idx:02d}/{len(all_cases):02d} {case.name} model={case.model} error={exc}", flush=True)
        except Exception as exc:
            failures.append((case.name, f"unexpected error: {exc}", ""))
            print(f"FAIL {idx:02d}/{len(all_cases):02d} {case.name} model={case.model} error={exc}", flush=True)

    elapsed = time.time() - started
    print(f"\nScore: {len(selected) - len(failures)}/{len(selected)} in {elapsed:.1f}s")
    if failures:
        print("\nFailures:")
        for name, reason, text in failures:
            print(f"- {name}: {reason}")
            if text:
                print(text[:1200].replace("\n", "\\n"))
        return 1
    print("PASS: live runtime-backed OSS burn-in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
