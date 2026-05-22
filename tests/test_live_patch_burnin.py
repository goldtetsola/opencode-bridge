#!/usr/bin/env python3
"""Live A4/A5 implementation burn-in ladder.

Skipped by default. Run with LIVE_A4A5_BURNIN=1 and OPENCODE_GO_API_KEY set.
The ladder keeps running after individual case failures so the final output
shows which rung failed: model output, runtime validation, isolated apply, or
workspace-cleanliness.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


BRIDGE_PORT = int(os.getenv("LIVE_A4A5_BURNIN_PORT", "4017"))
AUTH = "sk-local-live-a4a5-burnin"
ROOT = os.path.dirname(os.path.dirname(__file__))
HTTP_TARGET = "tests/fixtures/a4_http_target.py"
NOTES_TARGET = "tests/fixtures/a4_live_notes.md"
CREATED_TARGET = "tests/fixtures/a4_live_created.py"
LIVE_TEST_TARGET = "tests/test_a4_live_fixture_behavior.py"
STRING_UTIL_TARGET = "tests/fixtures/a4_live_string_utils.py"
STRING_TEST_TARGET = "tests/test_a4_live_string_utils.py"
A6_AUTH_TARGET = "src/auth/a6_live_gate.py"
HTTP_TARGET_ORIGINAL = 'VALUE = "original"\n\n\ndef describe():\n    return VALUE\n'
NOTES_TARGET_ORIGINAL = (
    "# A4 Live Notes\n\n"
    "This fixture is intentionally tiny so live OSS patch tests can propose a\n"
    "bounded docs-style edit without touching product code.\n"
)
LIVE_TEST_TARGET_ORIGINAL = "def test_a4_live_existing():\n    assert True\n"
STRING_UTIL_TARGET_ORIGINAL = (
    "def normalize_label(value: str) -> str:\n"
    "    return \" \".join(value.strip().lower().split())\n"
)
STRING_TEST_TARGET_ORIGINAL = (
    "import os\n"
    "import sys\n"
    "import unittest\n"
    "\n\n"
    "ROOT = os.path.dirname(os.path.dirname(__file__))\n"
    "sys.path.insert(0, os.path.join(ROOT, \"tests\", \"fixtures\"))\n"
    "\n"
    "from a4_live_string_utils import normalize_label\n"
    "\n\n"
    "class NormalizeLabelTests(unittest.TestCase):\n"
    "    def test_strips_and_lowercases(self):\n"
    "        self.assertEqual(normalize_label(\"  Hello  \"), \"hello\")\n"
    "\n\n"
    "if __name__ == \"__main__\":\n"
    "    unittest.main()\n"
)
A6_AUTH_TARGET_ORIGINAL = (
    "def can_open_runtime_gate(user_role: str) -> bool:\n"
    "    return user_role == \"admin\"\n"
)
LOG_DIR = os.path.join(ROOT, "tmp", "live-a4a5-burnin")


def wait_for_bridge() -> None:
    for _ in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/health")
            req.add_header("Authorization", f"Bearer {AUTH}")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if json.loads(resp.read()).get("ok"):
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Bridge failed to start")


def call_bridge(body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {AUTH}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=int(os.getenv("LIVE_A4A5_CLIENT_TIMEOUT", "240"))) as resp:
        return json.loads(resp.read())


def read_fixture(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def assert_main_workspace_clean() -> None:
    assert read_fixture(HTTP_TARGET) == HTTP_TARGET_ORIGINAL
    assert read_fixture(NOTES_TARGET) == NOTES_TARGET_ORIGINAL
    assert read_fixture(LIVE_TEST_TARGET) == LIVE_TEST_TARGET_ORIGINAL
    assert read_fixture(STRING_UTIL_TARGET) == STRING_UTIL_TARGET_ORIGINAL
    assert read_fixture(STRING_TEST_TARGET) == STRING_TEST_TARGET_ORIGINAL
    assert read_fixture(A6_AUTH_TARGET) == A6_AUTH_TARGET_ORIGINAL
    assert not os.path.exists(os.path.join(ROOT, CREATED_TARGET))


def response_text(response: dict) -> str:
    try:
        return response["output"][0]["content"][0]["text"]
    except Exception:
        return json.dumps(response, indent=2)[:4000]


def implementation_mission(
    *,
    mission_id: str,
    tier: str,
    objective: str,
    owned_paths: list[str],
    read_only_paths: list[str] | None = None,
    max_files_changed: int = 1,
    verification_command: list[str] | None = None,
    objective_spec: dict | None = None,
    risk_tier: str = "low",
    critical_path_write_allowed: bool = False,
    critical_path_reason: str | None = None,
) -> dict:
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": tier,
        "mode": "patch_proposal" if tier == "A4" else ("critical_implementation" if tier == "A6" else "bounded_implementation"),
        "objective": objective,
        "risk_tier": risk_tier,
        "write_allowed": tier in ("A5", "A6"),
        "allowed_roots": [],
        "allowed_paths": sorted(set(owned_paths + (read_only_paths or []))),
        "owned_paths": owned_paths,
        "read_only_paths": read_only_paths or owned_paths,
        "forbidden_roots": [".env", ".git", ".codex-oss/"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": int(os.getenv("LIVE_A4A5_MISSION_SECONDS", "120")),
        "stop_conditions": ["valid_patch", "deadline_reached"],
        "report_schema": "patch_validation_report.v1" if tier == "A4" else "implementation_report.v1",
        "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
        "max_files_changed": max_files_changed,
        "max_patch_bytes": 12000,
        "verification_policy": {
            "allowed_commands": [verification_command or ["python3", HTTP_TARGET]],
            "max_commands": 1,
            "timeout_seconds": 20,
        },
        "critical_path_write_allowed": critical_path_write_allowed,
    }
    if critical_path_reason:
        mission["critical_path_reason"] = critical_path_reason
    if objective_spec:
        mission["objective_spec"] = objective_spec
    if tier in ("A5", "A6"):
        mission["apply_mode"] = "isolated_worktree"
    return mission


def multi_file_objective_spec() -> dict:
    return {
        "schema_version": "objective_spec.v1",
        "objective_type": "implementation_patch",
        "target": {
            "required_changed_files": [HTTP_TARGET, LIVE_TEST_TARGET],
            "required_source_files": [HTTP_TARGET],
            "required_test_files": [LIVE_TEST_TARGET],
            "required_symbols": [{"path": HTTP_TARGET, "kind": "function", "name": "live_multi_file_patch_marker"}],
            "required_test_names": ["test_live_multi_file_patch_marker_shape"],
            "forbidden_removed_patterns": ["test_a4_live_existing"],
        },
        "required_outputs": ["changed_source_file", "changed_test_file"],
        "required_evidence_shapes": ["source_change", "test_definition"],
        "completion_criteria": ["required_symbols_present", "required_tests_present", "verification_required"],
    }


def critical_path_objective_spec() -> dict:
    return {
        "schema_version": "objective_spec.v1",
        "objective_type": "critical_path_patch",
        "target": {
            "required_changed_files": [A6_AUTH_TARGET],
            "required_source_files": [A6_AUTH_TARGET],
            "required_symbols": [{"path": A6_AUTH_TARGET, "kind": "function", "name": "critical_runtime_patch_marker"}],
        },
        "required_outputs": ["changed_critical_file"],
        "required_evidence_shapes": ["critical_source_change"],
        "completion_criteria": ["required_symbols_present", "isolated_apply_only", "verification_required"],
    }


def run_case(model: str, mission: dict) -> str:
    response = call_bridge({
        "model": model,
        "stream": False,
        "input": [{
            "role": "user",
            "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
        }],
    })
    assert response.get("status") == "completed", response
    return response_text(response)


def assert_a4_valid(text: str) -> None:
    assert "OSS_PATCH_VALIDATION_BEGIN" in text, text[:4000]
    assert "Status: VALID" in text, text[:4000]


def assert_a5_verified(text: str) -> None:
    assert "OSS_IMPLEMENTATION_REPORT_BEGIN" in text, text[:4000]
    assert "Status: VERIFIED" in text, text[:4000]
    assert "Main workspace mutated: false" in text, text[:4000]


def burnin_cases(a4_model: str, a5_model: str) -> list[tuple[str, str, dict, callable]]:
    return [
        (
            "A4 DesiredState python_function_exists",
            a4_model,
            implementation_mission(
                mission_id="mission_live_burnin_a4_desired_state",
                tier="A4",
                owned_paths=[HTTP_TARGET],
                objective=(
                    "Patch tests/fixtures/a4_http_target.py only. Ensure a Python function named "
                    "live_runtime_patch_marker exists and returns the exact string 'live'. "
                    "Return DesiredStateV1 using assertion type python_function_exists, path "
                    "tests/fixtures/a4_http_target.py, function_name live_runtime_patch_marker, "
                    "and return_value live. Do not return a raw diff."
                ),
            ),
            assert_a4_valid,
        ),
        (
            "A5 DesiredState isolated apply",
            a5_model,
            implementation_mission(
                mission_id="mission_live_burnin_a5_desired_state",
                tier="A5",
                owned_paths=[HTTP_TARGET],
                objective=(
                    "Patch tests/fixtures/a4_http_target.py only. Ensure a Python function named "
                    "live_runtime_patch_marker exists and returns the exact string 'live'. "
                    "Return DesiredStateV1 using assertion type python_function_exists, path "
                    "tests/fixtures/a4_http_target.py, function_name live_runtime_patch_marker, "
                    "and return_value live. The runtime must apply only in an isolated worktree."
                ),
            ),
            assert_a5_verified,
        ),
        (
            "A4 PatchIntent append docs block",
            a4_model,
            implementation_mission(
                mission_id="mission_live_burnin_a4_append_docs",
                tier="A4",
                owned_paths=[NOTES_TARGET],
                objective=(
                    "Patch tests/fixtures/a4_live_notes.md only. Return PatchIntentV1, not DesiredStateV1 "
                    "and not a raw diff. Use exactly one append_to_file edit that appends this markdown block: "
                    "\\n## Runtime Burn-in Marker\\n\\nKimi proposed this bounded docs edit.\\n"
                ),
                verification_command=["python3", "-c", "print('docs patch validation only')"],
            ),
            assert_a4_valid,
        ),
        (
            "A5 PatchIntent replace exact in isolation",
            a5_model,
            implementation_mission(
                mission_id="mission_live_burnin_a5_replace_exact",
                tier="A5",
                owned_paths=[HTTP_TARGET],
                objective=(
                    "Patch tests/fixtures/a4_http_target.py only. Return PatchIntentV1, not DesiredStateV1 "
                    "and not a raw diff. Use exactly one replace_exact edit changing old_text "
                    "`VALUE = \"original\"` to new_text `VALUE = \"patched\"`. The runtime applies only "
                    "in an isolated worktree."
                ),
                verification_command=["python3", HTTP_TARGET],
            ),
            assert_a5_verified,
        ),
        (
            "A4 PatchRecipe anchor-slot insert",
            a4_model,
            implementation_mission(
                mission_id="mission_live_burnin_a4_patch_recipe",
                tier="A4",
                owned_paths=[HTTP_TARGET],
                objective=(
                    "Patch tests/fixtures/a4_http_target.py only. Return PatchRecipeV1, not DesiredStateV1, "
                    "PatchIntentV1, or a raw diff. Use recipe_type insert_block_at_anchor, target_file "
                    "tests/fixtures/a4_http_target.py, selected_anchor_id anchor:eof, and content exactly "
                    "\\n\\ndef live_recipe_patch_marker():\\n    return \"recipe\"\\n"
                ),
                verification_command=["python3", HTTP_TARGET],
            ),
            assert_a4_valid,
        ),
        (
            "A5 PatchIntent multi-file isolated apply",
            a5_model,
            implementation_mission(
                mission_id="mission_live_burnin_a5_multifile",
                tier="A5",
                owned_paths=[HTTP_TARGET, LIVE_TEST_TARGET],
                max_files_changed=2,
                objective=(
                    "Patch exactly two files: tests/fixtures/a4_http_target.py and "
                    "tests/test_a4_live_fixture_behavior.py. Return PatchIntentV1, not DesiredStateV1 "
                    "and not a raw diff. Use two append_to_file edits. In a4_http_target.py append "
                    "a function named live_multi_file_patch_marker returning 'multi'. In the test file append "
                    "a tiny test named test_live_multi_file_patch_marker_shape that contains assert 'multi'. "
                    "The runtime applies only in an isolated worktree."
                ),
                verification_command=[
                    "python3",
                    "-m",
                    "py_compile",
                    HTTP_TARGET,
                    LIVE_TEST_TARGET,
                ],
                objective_spec=multi_file_objective_spec(),
            ),
            assert_a5_verified,
        ),
        (
            "A5 DesiredState realistic test-only isolated apply",
            a5_model,
            implementation_mission(
                mission_id="mission_live_burnin_a5_test_only",
                tier="A5",
                owned_paths=[STRING_TEST_TARGET],
                read_only_paths=[STRING_UTIL_TARGET, STRING_TEST_TARGET],
                objective=(
                    "Patch only tests/test_a4_live_string_utils.py. Do not edit "
                    "tests/fixtures/a4_live_string_utils.py. Prefer DesiredStateV1, not a raw diff. "
                    "Use assertion type python_unittest_method_exists with path "
                    "tests/test_a4_live_string_utils.py, class_name NormalizeLabelTests, method_name "
                    "test_collapses_internal_whitespace, and body_lines containing "
                    "self.assertEqual(normalize_label(\"A   B\"), \"a b\"). The runtime applies only "
                    "in an isolated worktree."
                ),
                verification_command=["python3", STRING_TEST_TARGET],
            ),
            assert_a5_verified,
        ),
        (
            "A6 DesiredState critical-path isolated apply",
            a5_model.replace("mission-a5-", "mission-a6-"),
            implementation_mission(
                mission_id="mission_live_burnin_a6_critical",
                tier="A6",
                owned_paths=[A6_AUTH_TARGET],
                read_only_paths=[A6_AUTH_TARGET],
                risk_tier="critical",
                critical_path_write_allowed=True,
                critical_path_reason="live burn-in proves isolated critical-path patching only",
                objective=(
                    "Patch src/auth/a6_live_gate.py only. This is a critical-path isolated implementation. "
                    "Prefer DesiredStateV1, not a raw diff. Use assertion type python_function_exists with "
                    "path src/auth/a6_live_gate.py, function_name critical_runtime_patch_marker, and "
                    "return_value critical. The runtime must apply only in an isolated worktree."
                ),
                verification_command=["python3", "-m", "py_compile", A6_AUTH_TARGET],
                objective_spec=critical_path_objective_spec(),
            ),
            assert_a5_verified,
        ),
    ]


def main() -> None:
    if os.getenv("LIVE_A4A5_BURNIN") != "1":
        print("SKIP: set LIVE_A4A5_BURNIN=1 and OPENCODE_GO_API_KEY to run live A4/A5 burn-in")
        return
    if not os.getenv("OPENCODE_GO_API_KEY") and not os.getenv("UPSTREAM_API_KEY"):
        print("SKIP: OPENCODE_GO_API_KEY/UPSTREAM_API_KEY is not set")
        return

    assert_main_workspace_clean()
    env = {
        **os.environ,
        "LITELLM_MASTER_KEY": AUTH,
        "PROXY_PORT": str(BRIDGE_PORT),
        "GPT_MODEL_STRATEGY": "oss",
        "CONTINUATION_TOOLS": "none",
        "REQUEST_DEADLINE_SECONDS": os.getenv("LIVE_A4A5_DEADLINE_SECONDS", "180"),
        "UPSTREAM_TIMEOUT_SECONDS": os.getenv("LIVE_A4A5_UPSTREAM_TIMEOUT_SECONDS", "180"),
    }
    os.makedirs(LOG_DIR, exist_ok=True)
    stdout_path = os.path.join(LOG_DIR, "bridge.out.log")
    stderr_path = os.path.join(LOG_DIR, "bridge.err.log")
    stdout_handle = open(stdout_path, "w", encoding="utf-8")
    stderr_handle = open(stderr_path, "w", encoding="utf-8")
    bridge_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bridge.py")],
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        start_new_session=True,
    )
    failures: list[tuple[str, str]] = []
    try:
        wait_for_bridge()
        a4_model = os.getenv("LIVE_A4A5_A4_MODEL", "mission-a4-kimi")
        a5_model = os.getenv("LIVE_A4A5_A5_MODEL", "mission-a5-kimi")
        case_filter = os.getenv("LIVE_A4A5_CASE_FILTER", "").strip().lower()
        for name, model, mission, assertion in burnin_cases(a4_model, a5_model):
            if case_filter and case_filter not in name.lower() and case_filter not in mission["mission_id"].lower():
                continue
            try:
                text = run_case(model, mission)
                assertion(text)
                assert_main_workspace_clean()
                print(f"  PASS: {name} via {model}")
            except Exception as exc:
                failures.append((name, str(exc)))
                print(f"  FAIL: {name} via {model}: {exc}")
                try:
                    assert_main_workspace_clean()
                except Exception as clean_exc:
                    failures.append((f"{name} workspace cleanliness", str(clean_exc)))
                    print(f"  FAIL: {name} workspace cleanliness: {clean_exc}")
        if failures:
            print("FAIL: live A4/A5 burn-in ladder")
            for name, reason in failures:
                print(f"- {name}: {reason[:1000]}")
            raise SystemExit(1)
        print("PASS: live A4/A5 burn-in ladder")
    finally:
        bridge_proc.terminate()
        try:
            bridge_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(bridge_proc.pid, signal.SIGKILL)
            bridge_proc.wait(timeout=10)
        stdout_handle.close()
        stderr_handle.close()


if __name__ == "__main__":
    main()
