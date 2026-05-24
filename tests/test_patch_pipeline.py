#!/usr/bin/env python3
"""A4/A5 patch-mediated implementation contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.implementation import (
    apply_patch_in_workspace,
    apply_patch_in_isolated_worktree,
    apply_patch_in_temp_project,
    build_patch_proposal_from_desired_state,
    build_patch_proposal_from_intent,
    evaluate_desired_state,
    run_implementation_mission,
    validate_patch_proposal,
)
from codex_oss.audit import audit_mission
from codex_oss.mission import InvalidHandoffError, _build_mission
from codex_oss.visible_commentary import VisibleCommentarySink


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_project() -> tuple[str, str]:
    root = tempfile.mkdtemp(prefix="oss_patch_pipeline_")
    original = "def test_existing():\n    assert True\n"
    write(os.path.join(root, "tests/test_config.py"), original)
    write(os.path.join(root, "src/config.py"), "def parse_config(value):\n    return value\n")
    return root, original


def implementation_mission(**overrides):
    raw = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": "mission_patch_test",
        "tier": "A4",
        "mode": "patch_proposal",
        "objective": "Propose a test-only patch.",
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": [],
        "allowed_paths": ["src/config.py", "tests/test_config.py"],
        "owned_paths": ["tests/test_config.py"],
        "read_only_paths": ["src/config.py", "tests/test_config.py"],
        "forbidden_roots": [".env", ".git", "migrations/", "src/auth/"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": 60,
        "stop_conditions": ["valid_patch", "deadline_reached"],
        "report_schema": "patch_validation_report.v1",
        "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
        "max_files_changed": 1,
        "max_patch_bytes": 12000,
        "verification_policy": {
            "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
            "max_commands": 1,
            "timeout_seconds": 20,
        },
    }
    raw.update(overrides)
    return _build_mission(raw)


def implementation_objective_spec(**overrides):
    spec = {
        "schema_version": "objective_spec.v1",
        "objective_type": "implementation_test_only",
        "target": {
            "test_file": "tests/test_config.py",
            "source_files": ["src/config.py"],
            "required_test_names": ["test_empty_value"],
        },
        "required_outputs": ["changed_test_file"],
        "required_evidence_shapes": ["test_definition"],
        "completion_criteria": ["required_test_present", "source_files_unchanged"],
    }
    spec.update(overrides)
    return spec


def implementation_patch_objective_spec(**overrides):
    spec = {
        "schema_version": "objective_spec.v1",
        "objective_type": "implementation_patch",
        "target": {
            "required_changed_files": ["src/config.py", "tests/test_config.py"],
            "required_source_files": ["src/config.py"],
            "required_test_files": ["tests/test_config.py"],
            "required_symbols": [{"path": "src/config.py", "kind": "function", "name": "parse_empty_value"}],
            "required_test_names": ["test_empty_value"],
            "forbidden_removed_patterns": ["def test_existing"],
        },
        "required_outputs": ["changed_source_file", "changed_test_file"],
        "required_evidence_shapes": ["source_change", "test_definition"],
        "completion_criteria": ["required_symbols_present", "required_tests_present", "verification_required"],
    }
    spec.update(overrides)
    return spec


def proposal(diff: str, base_sha: str, path: str = "tests/test_config.py") -> dict:
    return {
        "patch_proposal_version": "1.0",
        "status": "PROPOSED",
        "summary": "Add a focused test.",
        "base": {"git_head": "fixture", "dirty_worktree_allowed": False},
        "changed_files": [
            {
                "path": path,
                "change_type": "modify",
                "reason": "Add regression coverage.",
                "base_sha256": base_sha,
            }
        ],
        "unified_diff": diff,
        "risk_assessment": {
            "risk_tier": "low",
            "critical_paths_touched": False,
            "blast_radius": "test-only",
        },
        "verification_plan": [
            {
                "command": ["python3", "-m", "py_compile", "tests/test_config.py"],
                "reason": "Compile the changed test file.",
            }
        ],
        "evidence_refs": ["file:src/config.py#extract:1"],
        "caveats": [],
    }


def valid_diff() -> str:
    return (
        "diff --git a/tests/test_config.py b/tests/test_config.py\n"
        "--- a/tests/test_config.py\n"
        "+++ b/tests/test_config.py\n"
        "@@ -1,2 +1,5 @@\n"
        " def test_existing():\n"
        "     assert True\n"
        "+\n"
        "+def test_empty_value():\n"
        "+    assert True\n"
    )


def broad_source_and_test_diff() -> str:
    return (
        "diff --git a/src/config.py b/src/config.py\n"
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "@@ -1,2 +1,6 @@\n"
        " def parse_config(value):\n"
        "     return value\n"
        "+\n"
        "+def parse_empty_value(value):\n"
        "+    if value == \"\":\n"
        "+        return None\n"
        "diff --git a/tests/test_config.py b/tests/test_config.py\n"
        "--- a/tests/test_config.py\n"
        "+++ b/tests/test_config.py\n"
        "@@ -1,2 +1,5 @@\n"
        " def test_existing():\n"
        "     assert True\n"
        "+\n"
        "+def test_empty_value():\n"
        "+    assert True\n"
    )


def broad_proposal(diff: str, test_sha: str, source_sha: str) -> dict:
    data = proposal(diff, test_sha)
    data["changed_files"] = [
        {"path": "src/config.py", "change_type": "modify", "reason": "Add helper.", "base_sha256": source_sha},
        {"path": "tests/test_config.py", "change_type": "modify", "reason": "Add test.", "base_sha256": test_sha},
    ]
    data["risk_assessment"] = {
        "risk_tier": "low",
        "critical_paths_touched": False,
        "blast_radius": "source-and-test",
    }
    data["verification_plan"] = [
        {"command": ["python3", "-m", "py_compile", "src/config.py", "tests/test_config.py"], "reason": "Compile changed files."}
    ]
    return data


def assert_a4_accepts_valid_owned_patch():
    root, original = make_project()
    try:
        mission = implementation_mission()
        result = validate_patch_proposal(proposal(valid_diff(), sha256_text(original)), mission, root)
        assert result["status"] == "VALID", result
        assert result["checks"]["unified_diff_parse"] is True, result
        assert result["checks"]["paths_allowed"] is True, result
        assert result["checks"]["applies_cleanly"] is True, result
        assert result["report_source"] == "runtime", result
        assert result["proposal_source"] == "raw_patch_proposal_v1", result
        assert result["runtime_built_diff"] is False, result
        assert result["semantic_review_ok"] is True, result
        assert result["verification_plan_ok"] is True, result
    finally:
        shutil.rmtree(root)


def assert_patch_intent_builds_runtime_owned_diff():
    root, original = make_project()
    try:
        mission = implementation_mission()
        intent = {
            "patch_intent_version": "1.0",
            "status": "PROPOSED",
            "summary": "Add a focused test through typed intent.",
            "edits": [
                {
                    "operation": "insert_after",
                    "path": "tests/test_config.py",
                    "anchor": "def test_existing():\n    assert True",
                    "content": "\n\ndef test_from_intent():\n    assert True\n",
                    "reason": "Exercise runtime-owned diff construction.",
                }
            ],
            "verification_plan": [
                {
                    "command": ["python3", "-m", "py_compile", "tests/test_config.py"],
                    "reason": "Compile the changed test file.",
                }
            ],
            "risk_assessment": {
                "risk_tier": "low",
                "critical_paths_touched": False,
                "blast_radius": "test-only",
            },
            "evidence_refs": ["file:tests/test_config.py#extract:test_existing"],
            "caveats": [],
        }
        patch = build_patch_proposal_from_intent(intent, mission, root)
        assert patch["proposal_source"] == "patch_intent_v1", patch
        assert patch["patch_proposal_version"] == "1.0", patch
        assert patch["changed_files"][0]["base_sha256"] == sha256_text(original), patch
        assert patch["unified_diff"].startswith("diff --git a/tests/test_config.py b/tests/test_config.py"), patch
        assert "+def test_from_intent():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
        assert result["runtime_built_diff"] is True, result
    finally:
        shutil.rmtree(root)


def assert_desired_state_builds_python_function_patch():
    root, original = make_project()
    try:
        mission = implementation_mission()
        desired_state = {
            "desired_state_version": "1.0",
            "summary": "Ensure marker function exists.",
            "assertions": [
                {
                    "type": "python_function_exists",
                    "path": "tests/test_config.py",
                    "function_name": "test_desired_state_marker",
                    "body": "def test_desired_state_marker():\n    assert True\n",
                }
            ],
        }
        evaluation = evaluate_desired_state(desired_state, mission, root)
        assert evaluation["status"] == "MISSING", evaluation
        patch = build_patch_proposal_from_desired_state(desired_state, mission, root)
        assert patch["changed_files"][0]["base_sha256"] == sha256_text(original), patch
        assert "+def test_desired_state_marker():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_desired_state_builds_unittest_method_patch():
    root, original = make_project()
    try:
        write(
            os.path.join(root, "tests/test_config.py"),
            "import unittest\n\n\nclass ConfigTests(unittest.TestCase):\n"
            "    def test_existing(self):\n"
            "        self.assertTrue(True)\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    unittest.main()\n",
        )
        original = open(os.path.join(root, "tests/test_config.py"), encoding="utf-8").read()
        mission = implementation_mission()
        desired_state = {
            "desired_state_version": "1.0",
            "summary": "Ensure unittest method exists.",
            "assertions": [
                {
                    "type": "python_unittest_method_exists",
                    "path": "tests/test_config.py",
                    "class_name": "ConfigTests",
                    "method_name": "test_empty_value",
                    "body_lines": ["self.assertEqual('', '')"],
                }
            ],
        }
        evaluation = evaluate_desired_state(desired_state, mission, root)
        assert evaluation["status"] == "MISSING", evaluation
        patch = build_patch_proposal_from_desired_state(desired_state, mission, root)
        assert patch["changed_files"][0]["base_sha256"] == sha256_text(original), patch
        assert "+    def test_empty_value(self):" in patch["unified_diff"], patch
        assert "+        self.assertEqual('', '')" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_desired_state_infers_unittest_method_type_from_shape():
    root, _ = make_project()
    try:
        write(
            os.path.join(root, "tests/test_config.py"),
            "import unittest\n\n\nclass ConfigTests(unittest.TestCase):\n"
            "    def test_existing(self):\n"
            "        self.assertTrue(True)\n",
        )
        mission = implementation_mission()
        desired_state = {
            "desired_state_version": "1.0",
            "summary": "Infer unittest assertion type.",
            "assertions": [
                {
                    "path": "tests/test_config.py",
                    "class_name": "ConfigTests",
                    "test_name": "test_empty_value",
                    "body_lines": ["self.assertEqual('', '')"],
                }
            ],
        }
        patch = build_patch_proposal_from_desired_state(desired_state, mission, root)
        assert "+    def test_empty_value(self):" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_desired_state_ignores_malformed_optional_metadata():
    root, _ = make_project()
    try:
        mission = implementation_mission()
        desired_state = {
            "desired_state_version": "1.0",
            "summary": "Ensure marker function exists.",
            "assertions": [
                {
                    "type": "python_function_exists",
                    "path": "tests/test_config.py",
                    "function_name": "test_optional_metadata_marker",
                    "return_value": True,
                }
            ],
            "risk_assessment": ["low"],
            "verification_plan": {"command": ["python3", "tests/test_config.py"]},
            "evidence_refs": "file:tests/test_config.py#extract:test_existing",
            "caveats": "malformed optional metadata should not crash",
        }
        patch = build_patch_proposal_from_desired_state(desired_state, mission, root)
        assert "+def test_optional_metadata_marker():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_desired_state_already_satisfied_returns_status():
    root, _ = make_project()
    try:
        write(
            os.path.join(root, "tests/test_config.py"),
            "def test_existing():\n    assert True\n\n\ndef test_desired_state_marker():\n    assert True\n",
        )
        mission = implementation_mission()
        desired_state = {
            "desired_state_version": "1.0",
            "assertions": [
                {
                    "type": "python_function_exists",
                    "path": "tests/test_config.py",
                    "function_name": "test_desired_state_marker",
                    "body_contains": "assert True",
                }
            ],
        }
        evaluation = evaluate_desired_state(desired_state, mission, root)
        assert evaluation["status"] == "ALREADY_SATISFIED", evaluation
        try:
            build_patch_proposal_from_desired_state(desired_state, mission, root)
        except ValueError as exc:
            assert "already satisfied" in str(exc), exc
        else:
            raise AssertionError("already satisfied desired state should not build a patch")
    finally:
        shutil.rmtree(root)


def assert_patch_intent_rejects_ambiguous_or_missing_anchors():
    root, _ = make_project()
    try:
        write(
            os.path.join(root, "tests/test_config.py"),
            "marker = 1\nmarker = 2\n",
        )
        mission = implementation_mission()
        ambiguous = {
            "patch_intent_version": "1.0",
            "summary": "Ambiguous anchor",
            "edits": [
                {
                    "operation": "insert_after",
                    "path": "tests/test_config.py",
                    "anchor": "marker",
                    "content": "\nadded = True\n",
                }
            ],
        }
        try:
            build_patch_proposal_from_intent(ambiguous, mission, root)
        except ValueError as exc:
            assert "anchor must match exactly once" in str(exc), exc
        else:
            raise AssertionError("ambiguous anchor must be rejected")

        missing = dict(ambiguous)
        missing["edits"] = [dict(ambiguous["edits"][0], anchor="not present")]
        try:
            build_patch_proposal_from_intent(missing, mission, root)
        except ValueError as exc:
            assert "anchor must match exactly once" in str(exc), exc
        else:
            raise AssertionError("missing anchor must be rejected")
    finally:
        shutil.rmtree(root)


def assert_patch_intent_supports_replace_and_create_file():
    root, _ = make_project()
    try:
        mission = implementation_mission(
            owned_paths=["tests/test_config.py", "tests/test_new.py"],
            allowed_paths=["src/config.py", "tests/test_config.py", "tests/test_new.py"],
            read_only_paths=["src/config.py", "tests/test_config.py"],
            max_files_changed=2,
        )
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Replace exact text and create a fixture test.",
            "edits": [
                {
                    "operation": "replace_exact",
                    "path": "tests/test_config.py",
                    "old_text": "assert True",
                    "new_text": "assert 1 == 1",
                },
                {
                    "operation": "create_file",
                    "path": "tests/test_new.py",
                    "content": "def test_new_file():\n    assert True\n",
                },
            ],
        }
        patch = build_patch_proposal_from_intent(intent, mission, root)
        assert "diff --git a/tests/test_config.py b/tests/test_config.py" in patch["unified_diff"], patch
        assert "diff --git a/tests/test_new.py b/tests/test_new.py" in patch["unified_diff"], patch
        assert "+def test_new_file():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_patch_intent_normalizes_common_model_aliases():
    root, original = make_project()
    try:
        mission = implementation_mission()
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Use common model aliases.",
            "risk_assessment": ["low risk"],
            "verification_plan": {"command": ["python3", "tests/test_config.py"]},
            "evidence_refs": "file:tests/test_config.py#extract:test_existing",
            "caveats": "model supplied scalar caveat",
            "edits": [
                {
                    "op": "append_to_file",
                    "target_file": "tests/test_config.py",
                    "append_content": "\n\ndef test_alias_intent():\n    assert True\n",
                }
            ],
        }
        patch = build_patch_proposal_from_intent(intent, mission, root)
        assert patch["changed_files"][0]["path"] == "tests/test_config.py", patch
        assert patch["changed_files"][0]["base_sha256"] == sha256_text(original), patch
        assert "+def test_alias_intent():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_patch_intent_uses_top_level_path_default():
    root, original = make_project()
    try:
        mission = implementation_mission()
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Use top-level path default.",
            "file_path": "tests/test_config.py",
            "edits": [
                {
                    "operation": "append_to_file",
                    "content": "\n\ndef test_top_level_path():\n    assert True\n",
                }
            ],
        }
        patch = build_patch_proposal_from_intent(intent, mission, root)
        assert patch["changed_files"][0]["path"] == "tests/test_config.py", patch
        assert patch["changed_files"][0]["base_sha256"] == sha256_text(original), patch
        assert "+def test_top_level_path():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def assert_patch_intent_infers_append_when_operation_missing():
    root, original = make_project()
    try:
        mission = implementation_mission()
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Infer append operation from content.",
            "edits": [
                {
                    "path": "tests/test_config.py",
                    "content": "\n\ndef test_inferred_append():\n    assert True\n",
                }
            ],
        }
        patch = build_patch_proposal_from_intent(intent, mission, root)
        assert patch["changed_files"][0]["base_sha256"] == sha256_text(original), patch
        assert "+def test_inferred_append():" in patch["unified_diff"], patch
        result = validate_patch_proposal(patch, mission, root)
        assert result["status"] == "VALID", result
    finally:
        shutil.rmtree(root)


def fake_chat_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def assert_patch_intent_noop_gets_targeted_repair():
    root, _ = make_project()
    try:
        mission = implementation_mission()
        calls = []
        noop_intent = {
            "patch_intent_version": "1.0",
            "summary": "No-op edit",
            "edits": [
                {
                    "operation": "replace_exact",
                    "path": "tests/test_config.py",
                    "old_text": "assert True",
                    "new_text": "assert True",
                }
            ],
        }
        repaired_intent = {
            "patch_intent_version": "1.0",
            "summary": "Add a repaired test",
            "edits": [
                {
                    "operation": "insert_after",
                    "path": "tests/test_config.py",
                    "anchor": "def test_existing():\n    assert True",
                    "content": "\n\ndef test_repaired_intent():\n    assert True\n",
                }
            ],
        }

        def call_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return fake_chat_response(json_dumps(noop_intent))
            return fake_chat_response(json_dumps(repaired_intent))

        result = run_implementation_mission(
            mission=mission,
            raw_model_alias="mission-a4-kimi",
            handoff="",
            call_model=call_model,
            timeout=30,
            project_root=root,
        )
        assert result["status"] == "VALID", result
        assert len(calls) == 2, calls
        assert "could not be turned into a patch" in calls[1], calls[1]
    finally:
        shutil.rmtree(root)


def assert_malformed_desired_state_gets_targeted_repair():
    root, _ = make_project()
    try:
        mission = implementation_mission()
        calls = []
        repaired_state = {
            "desired_state_version": "1.0",
            "summary": "Ensure repaired desired state marker exists.",
            "assertions": [
                {
                    "type": "python_function_exists",
                    "path": "tests/test_config.py",
                    "function_name": "test_repaired_desired_state",
                    "return_value": True,
                }
            ],
        }

        def call_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return fake_chat_response('{"desired_state_version":"1.0","assertions":[{"type":"python_function_exists"')
            return fake_chat_response(json_dumps(repaired_state))

        result = run_implementation_mission(
            mission=mission,
            raw_model_alias="mission-a4-kimi",
            handoff="",
            call_model=call_model,
            timeout=30,
            project_root=root,
        )
        assert result["status"] == "VALID", result
        assert len(calls) == 2, calls
        assert "not valid JSON" in calls[1], calls[1]
    finally:
        shutil.rmtree(root)


def assert_a4_writes_runtime_artifact_bundle():
    root, original = make_project()
    try:
        mission = implementation_mission(mission_id="mission_a4_artifacts")
        diff = (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/tests/test_config.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def test_existing():\n"
            "     assert True\n"
            "+\n"
            "+def test_a4_artifact_bundle():\n"
            "+    assert True\n"
        )
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(proposal(diff, sha256_text(original)), indent=2)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )
        result = run_implementation_mission(
            mission=mission,
            raw_model_alias="mission-a4-kimi",
            handoff=handoff,
            call_model=lambda messages, tools, timeout: fake_chat_response("{}"),
            timeout=30,
            project_root=root,
        )
        assert result["status"] == "VALID", result
        artifact_dir = os.path.join(root, ".codex-oss", "missions", "mission_a4_artifacts")
        for name in ("mission.json", "patch.diff", "rollback.diff", "validation.json", "verification.json", "report.json", "ledger.json", "trace.jsonl", "summary.md"):
            assert os.path.exists(os.path.join(artifact_dir, name)), name
        with open(os.path.join(artifact_dir, "report.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        assert report["rollback"]["artifact"].endswith("rollback.diff"), report
        audited = audit_mission(root, "mission_a4_artifacts")
        assert audited["ok"] is True, audited
    finally:
        shutil.rmtree(root)


def assert_a4_writes_visible_commentary_and_patch_intent_artifacts():
    root, _ = make_project()
    try:
        mission = implementation_mission(mission_id="mission_a4_visible_implementation")
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Add visible implementation commentary fixture.",
            "edits": [
                {
                    "operation": "insert_after",
                    "path": "tests/test_config.py",
                    "anchor": "def test_existing():\n    assert True",
                    "content": "\n\ndef test_visible_implementation_commentary():\n    assert True\n",
                }
            ],
            "verification_plan": [
                {"command": ["python3", "-m", "pytest", "tests/test_config.py"]}
            ],
        }
        handoff = (
            "<OSS_PATCH_INTENT_JSON>\n"
            + json.dumps(intent, indent=2)
            + "\n</OSS_PATCH_INTENT_JSON>\n"
        )
        artifact_dir = os.path.join(root, ".codex-oss", "missions", mission.mission_id)
        commentary = VisibleCommentarySink(mission.mission_id, artifact_dir)
        result = run_implementation_mission(
            mission=mission,
            raw_model_alias="mission-a4-kimi",
            handoff=handoff,
            call_model=lambda messages, tools, timeout: fake_chat_response("{}"),
            timeout=30,
            project_root=root,
            commentary=commentary,
        )
        assert result["status"] == "VALID", result
        for name in ("visible_commentary.jsonl", "patch_proposal.json", "patch_intent.json"):
            assert os.path.exists(os.path.join(artifact_dir, name)), name
        with open(os.path.join(artifact_dir, "visible_commentary.jsonl"), encoding="utf-8") as handle:
            event_types = [json.loads(line)["event_type"] for line in handle if line.strip()]
        assert "mission_started" in event_types, event_types
        assert "patch_intent_received" in event_types, event_types
        assert "patch_validation_passed" in event_types, event_types
        assert "mission_completed" in event_types, event_types
        with open(os.path.join(artifact_dir, "patch_intent.json"), encoding="utf-8") as handle:
            saved_intent = json.load(handle)
        assert saved_intent["patch_intent_version"] == "1.0", saved_intent
    finally:
        shutil.rmtree(root)


def json_dumps(value: dict) -> str:
    import json

    return json.dumps(value)


def assert_patch_validator_rejects_path_escape_and_forbidden_paths():
    root, original = make_project()
    try:
        mission = implementation_mission(owned_paths=["tests/test_config.py", "../outside.py"])
        escape_diff = (
            "diff --git a/tests/test_config.py b/../outside.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/../outside.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def test_existing():\n"
            "     assert True\n"
        )
        result = validate_patch_proposal(proposal(escape_diff, sha256_text(original), "../outside.py"), mission, root)
        assert result["status"] == "INVALID", result
        assert result["checks"]["path_escape"] is True, result

        env_diff = (
            "diff --git a/tests/test_config.py b/.env\n"
            "--- a/tests/test_config.py\n"
            "+++ b/.env\n"
            "@@ -1,2 +1,1 @@\n"
            "-def test_existing():\n"
            "-    assert True\n"
            "+OPENAI_API_KEY=sk-nope\n"
        )
        result = validate_patch_proposal(proposal(env_diff, sha256_text(original), ".env"), mission, root)
        assert result["status"] == "INVALID", result
        assert result["checks"]["paths_allowed"] is False, result
    finally:
        shutil.rmtree(root)


def assert_patch_validator_escalates_critical_paths_and_blocks_secrets():
    root, original = make_project()
    try:
        write(os.path.join(root, "src/auth/login.py"), "def login():\n    return True\n")
        mission = implementation_mission(
            allowed_paths=["src/auth/login.py"],
            owned_paths=["src/auth/login.py"],
            critical_path_write_allowed=False,
        )
        critical_diff = (
            "diff --git a/src/auth/login.py b/src/auth/login.py\n"
            "--- a/src/auth/login.py\n"
            "+++ b/src/auth/login.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def login():\n"
            "-    return True\n"
            "+    return False\n"
        )
        result = validate_patch_proposal(proposal(critical_diff, sha256_text("def login():\n    return True\n"), "src/auth/login.py"), mission, root)
        assert result["status"] == "ESCALATE", result
        assert result["checks"]["critical_paths_touched"] is True, result

        secret_diff = (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/tests/test_config.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def test_existing():\n"
            "     assert True\n"
            "+TOKEN = \"sk-abcdefghijklmnopqrstuvwxyz\"\n"
        )
        mission = implementation_mission()
        result = validate_patch_proposal(proposal(secret_diff, sha256_text(original)), mission, root)
        assert result["status"] == "INVALID", result
        assert result["checks"]["secret_scan_ok"] is False, result
    finally:
        shutil.rmtree(root)


def assert_patch_validator_rejects_stale_base_and_oversized_patch():
    root, _ = make_project()
    try:
        mission = implementation_mission(max_patch_bytes=20)
        result = validate_patch_proposal(proposal(valid_diff(), "bad-sha"), mission, root)
        assert result["status"] == "INVALID", result
        assert result["checks"]["base_sha_match"] is False, result
        assert result["checks"]["max_patch_size_ok"] is False, result
    finally:
        shutil.rmtree(root)


def assert_patch_validator_enforces_test_only_objective_spec():
    root, original = make_project()
    try:
        mission = implementation_mission(objective_spec=implementation_objective_spec())
        valid = validate_patch_proposal(proposal(valid_diff(), sha256_text(original)), mission, root)
        assert valid["status"] == "VALID", valid
        assert valid["checks"]["objective_satisfied"] is True, valid

        wrong_test_diff = valid_diff().replace("test_empty_value", "test_wrong_behavior")
        wrong = validate_patch_proposal(proposal(wrong_test_diff, sha256_text(original)), mission, root)
        assert wrong["status"] == "INVALID", wrong
        assert wrong["checks"]["objective_satisfied"] is False, wrong
        assert any("required test name" in reason for reason in wrong["reasons"]), wrong

        source_mission = implementation_mission(
            owned_paths=["tests/test_config.py", "src/config.py"],
            max_files_changed=2,
            objective_spec=implementation_objective_spec(),
        )
        source_diff = (
            "diff --git a/src/config.py b/src/config.py\n"
            "--- a/src/config.py\n"
            "+++ b/src/config.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def parse_config(value):\n"
            "-    return value\n"
            "+    return value.strip()\n"
        )
        source = validate_patch_proposal(
            proposal(source_diff, sha256_text("def parse_config(value):\n    return value\n"), "src/config.py"),
            source_mission,
            root,
        )
        assert source["status"] == "INVALID", source
        assert source["checks"]["objective_satisfied"] is False, source
        assert any("source file changed" in reason for reason in source["reasons"]), source
    finally:
        shutil.rmtree(root)


def assert_patch_validator_enforces_broad_implementation_objective_spec():
    root, original = make_project()
    try:
        source_original = "def parse_config(value):\n    return value\n"
        mission = implementation_mission(
            owned_paths=["src/config.py", "tests/test_config.py"],
            max_files_changed=2,
            objective_spec=implementation_patch_objective_spec(),
        )
        valid = validate_patch_proposal(
            broad_proposal(broad_source_and_test_diff(), sha256_text(original), sha256_text(source_original)),
            mission,
            root,
        )
        assert valid["status"] == "VALID", valid
        assert valid["checks"]["objective_satisfied"] is True, valid
        assert valid["checks"]["semantic_review_ok"] is True, valid
        assert valid["checks"]["verification_plan_ok"] is True, valid
        assert valid["implementation_readiness_graph"]["coverage_status"]["recommended_status"] == "VALID", valid
        assert valid["implementation_readiness_graph"]["coverage_status"]["can_apply"] is True, valid

        no_test = broad_source_and_test_diff().split("diff --git a/tests/test_config.py", 1)[0]
        invalid = validate_patch_proposal(
            broad_proposal(no_test, sha256_text(original), sha256_text(source_original)),
            mission,
            root,
        )
        assert invalid["status"] == "INVALID", invalid
        assert any("required changed file missing" in reason for reason in invalid["reasons"]), invalid
        assert invalid["implementation_readiness_graph"]["coverage_status"]["recommended_status"] == "INVALID", invalid
        assert "required_changed_file:tests/test_config.py" in invalid["implementation_readiness_graph"]["coverage_status"]["missing_obligation_ids"], invalid

        removed_test = broad_source_and_test_diff().replace(
            " def test_existing():\n     assert True\n",
            "-def test_existing():\n-    assert True\n",
        )
        invalid = validate_patch_proposal(
            broad_proposal(removed_test, sha256_text(original), sha256_text(source_original)),
            mission,
            root,
        )
        assert invalid["status"] == "INVALID", invalid
        assert any("forbidden removal pattern" in reason for reason in invalid["reasons"]), invalid
        assert invalid["implementation_readiness_graph"]["coverage_status"]["contradicted_total"] >= 1, invalid
        assert "forbidden_removed_pattern:def test_existing" in invalid["implementation_readiness_graph"]["coverage_status"]["contradicted_obligation_ids"], invalid
    finally:
        shutil.rmtree(root)


def assert_patch_validator_requires_evidence_shapes_for_broad_implementation():
    root, original = make_project()
    try:
        source_original = "def parse_config(value):\n    return value\n"
        mission = implementation_mission(
            owned_paths=["src/config.py", "tests/test_config.py"],
            max_files_changed=2,
            objective_spec=implementation_patch_objective_spec(required_evidence_shapes=["source_change", "flag_parameter"]),
        )
        patch = broad_proposal(broad_source_and_test_diff(), sha256_text(original), sha256_text(source_original))
        invalid = validate_patch_proposal(patch, mission, root)
        assert invalid["status"] == "INVALID", invalid
        assert invalid["implementation_readiness_graph"]["coverage_status"]["insufficient_evidence_total"] >= 1, invalid
        assert any(
            item.get("status") == "insufficient_evidence" and item.get("shape") == "flag_parameter"
            for item in invalid["implementation_readiness_graph"]["obligations"]
        ), invalid
    finally:
        shutil.rmtree(root)


def assert_a5_isolated_apply_verifies_without_mutating_main_workspace():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
        )
        patch = proposal(valid_diff(), sha256_text(original))
        report = apply_patch_in_isolated_worktree(patch, mission, root)
        assert report["status"] == "VERIFIED", report
        assert report["report_source"] == "runtime", report
        assert report["proposal_source"] == "raw_patch_proposal_v1", report
        assert report["runtime_built_diff"] is False, report
        assert report["model_repair_count"] == 0, report
        assert report["semantic_review_ok"] is True, report
        assert report["verification_plan_ok"] is True, report
        assert report["implementation_readiness"]["recommended_status"] == "VALID", report
        assert report["changed_files"] == ["tests/test_config.py"], report
        assert report["verification"][0]["exit_code"] == 0, report
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert handle.read() == original
        assert os.path.exists(os.path.join(root, report["patch_artifact"])), report
        artifact_dir = os.path.join(root, ".codex-oss", "missions", mission.mission_id)
        assert os.path.exists(os.path.join(artifact_dir, "implementation_readiness_graph.json")), artifact_dir
    finally:
        shutil.rmtree(root)


def assert_a5_isolated_apply_inside_repo_does_not_escape_to_parent_git():
    root = tempfile.mkdtemp(prefix="oss_nested_project_", dir=ROOT)
    try:
        write(os.path.join(root, "tests/test_config.py"), "def test_existing():\n    assert True\n")
        write(os.path.join(root, "src/config.py"), "def parse_config(value):\n    return value\n")
        mission = implementation_mission(
            mission_id="mission_a5_nested_worktree",
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
            allowed_paths=["tests/fixtures/nested_apply_smoke.py"],
            owned_paths=["tests/fixtures/nested_apply_smoke.py"],
            read_only_paths=[],
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_patch",
                "target": {
                    "required_changed_files": ["tests/fixtures/nested_apply_smoke.py"],
                    "required_test_files": ["tests/fixtures/nested_apply_smoke.py"],
                    "required_test_names": ["test_nested_apply_smoke"],
                },
                "required_outputs": ["changed_test_file"],
                "required_evidence_shapes": ["test_definition"],
                "completion_criteria": ["required_tests_present", "verification_required"],
            },
            verification_policy={
                "allowed_commands": [["python3", "tests/fixtures/nested_apply_smoke.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        )
        intent = {
            "patch_intent_version": "1.0",
            "status": "PROPOSED",
            "summary": "Create an isolated nested-repo verification smoke.",
            "edits": [
                {
                    "operation": "create_file",
                    "path": "tests/fixtures/nested_apply_smoke.py",
                    "content": (
                        "def test_nested_apply_smoke():\n"
                        "    assert True\n\n\n"
                        "if __name__ == '__main__':\n"
                        "    test_nested_apply_smoke()\n"
                    ),
                    "reason": "Prove isolated git apply stays inside the copied sandbox.",
                }
            ],
            "verification_plan": [
                {"command": ["python3", "tests/fixtures/nested_apply_smoke.py"], "reason": "Run smoke."}
            ],
        }
        patch = build_patch_proposal_from_intent(intent, mission, root)
        report = apply_patch_in_isolated_worktree(patch, mission, root)
        assert report["status"] == "VERIFIED", report
        assert report["main_workspace_mutated"] is False, report
        assert not os.path.exists(os.path.join(root, "tests/fixtures/nested_apply_smoke.py")), report
        assert os.path.exists(
            os.path.join(root, ".codex-oss", "worktrees", mission.mission_id, "tests/fixtures/nested_apply_smoke.py")
        ), report
    finally:
        shutil.rmtree(root)


def assert_a5_temp_project_apply_verifies_without_mutating_main_workspace():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="temp_project",
            objective_spec=implementation_objective_spec(),
        )
        report = apply_patch_in_temp_project(proposal(valid_diff(), sha256_text(original)), mission, root)
        assert report["status"] == "VERIFIED", report
        assert report["main_workspace_mutated"] is False, report
        assert report["execution_mode"] == "temp_project", report
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert handle.read() == original
    finally:
        shutil.rmtree(root)


def assert_a5_workspace_apply_is_policy_gated_and_reversible():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="workspace",
            objective_spec=implementation_objective_spec(),
            verification_policy={
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        )
        patch = proposal(valid_diff(), sha256_text(original))
        blocked = apply_patch_in_workspace(patch, mission, root)
        assert blocked["status"] == "FAILED", blocked
        assert any("ALLOW_A5_WORKSPACE_APPLY" in caveat for caveat in blocked["caveats"]), blocked
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert handle.read() == original

        old_env = os.environ.get("ALLOW_A5_WORKSPACE_APPLY")
        os.environ["ALLOW_A5_WORKSPACE_APPLY"] = "1"
        try:
            applied = apply_patch_in_workspace(patch, mission, root)
        finally:
            if old_env is None:
                os.environ.pop("ALLOW_A5_WORKSPACE_APPLY", None)
            else:
                os.environ["ALLOW_A5_WORKSPACE_APPLY"] = old_env
        assert applied["status"] == "VERIFIED", applied
        assert applied["main_workspace_mutated"] is True, applied
        assert applied["rollback"]["available"] is True, applied
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert "test_empty_value" in handle.read()
    finally:
        shutil.rmtree(root)


def assert_a5_workspace_apply_can_be_enabled_by_mission_policy_without_env():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="workspace",
            objective_spec=implementation_objective_spec(),
            workspace_apply_policy={"allow_direct_workspace_apply": True},
        )
        patch = proposal(valid_diff(), sha256_text(original))
        old_env = os.environ.pop("ALLOW_A5_WORKSPACE_APPLY", None)
        try:
            applied = apply_patch_in_workspace(patch, mission, root)
        finally:
            if old_env is not None:
                os.environ["ALLOW_A5_WORKSPACE_APPLY"] = old_env
        assert applied["status"] == "VERIFIED", applied
        assert applied["main_workspace_mutated"] is True, applied
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert "test_empty_value" in handle.read()
    finally:
        shutil.rmtree(root)


def assert_a5_workspace_apply_rolls_back_on_verification_failure():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="workspace",
            objective_spec=implementation_objective_spec(),
            workspace_apply_policy={"allow_direct_workspace_apply": True},
            verification_policy={
                "allowed_commands": [["python3", "-c", "raise SystemExit(1)"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        )
        patch = proposal(valid_diff(), sha256_text(original))
        patch["verification_plan"] = [{"command": ["python3", "-c", "raise SystemExit(1)"], "reason": "force rollback"}]
        report = apply_patch_in_workspace(patch, mission, root)
        assert report["status"] == "VERIFICATION_FAILED", report
        assert report["main_workspace_mutated"] is False, report
        assert any("rolled back" in caveat for caveat in report["caveats"]), report
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert handle.read() == original
    finally:
        shutil.rmtree(root)


def assert_a5_workspace_apply_respects_existing_workspace_lock():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="workspace",
            objective_spec=implementation_objective_spec(),
            workspace_apply_policy={"allow_direct_workspace_apply": True},
        )
        os.makedirs(os.path.join(root, ".codex-oss", "locks"), exist_ok=True)
        write(os.path.join(root, ".codex-oss", "locks", "workspace-apply.lock"), "busy")
        report = apply_patch_in_workspace(proposal(valid_diff(), sha256_text(original)), mission, root)
        assert report["status"] == "FAILED", report
        assert any("Workspace apply lock" in caveat for caveat in report["caveats"]), report
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert handle.read() == original
    finally:
        shutil.rmtree(root)


def assert_a5_workspace_apply_requires_clean_git_when_policy_enabled():
    root, original = make_project()
    try:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "fixture"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        write(os.path.join(root, "README.md"), "dirty\n")
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="workspace",
            objective_spec=implementation_objective_spec(),
            workspace_apply_policy={"allow_direct_workspace_apply": True, "require_clean_worktree": True},
        )
        report = apply_patch_in_workspace(proposal(valid_diff(), sha256_text(original)), mission, root)
        assert report["status"] == "FAILED", report
        assert any("clean worktree" in caveat for caveat in report["caveats"]), report
        with open(os.path.join(root, "tests/test_config.py"), encoding="utf-8") as handle:
            assert handle.read() == original
    finally:
        shutil.rmtree(root)


def assert_a5_workspace_apply_rejects_dirty_target_files_by_default():
    root, original = make_project()
    try:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "fixture"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        dirty_content = original + "\n# dirty target\n"
        write(os.path.join(root, "tests/test_config.py"), dirty_content)
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="workspace_explicit",
            objective_spec=implementation_objective_spec(),
            workspace_apply_policy={"allow_direct_workspace_apply": True},
        )
        dirty_diff = (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/tests/test_config.py\n"
            "@@ -1,4 +1,7 @@\n"
            " def test_existing():\n"
            "     assert True\n"
            " \n"
            " # dirty target\n"
            "+\n"
            "+def test_empty_value():\n"
            "+    assert True\n"
        )
        report = apply_patch_in_workspace(proposal(dirty_diff, sha256_text(dirty_content)), mission, root)
        assert report["status"] == "FAILED", report
        assert any("clean target files" in caveat for caveat in report["caveats"]), report
    finally:
        shutil.rmtree(root)


def assert_path_locks_block_conflicting_apply():
    root, original = make_project()
    try:
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
        )
        lock_dir = os.path.join(root, ".codex-oss", "locks", "paths")
        os.makedirs(lock_dir, exist_ok=True)
        digest = hashlib.sha256("tests/test_config.py".encode("utf-8")).hexdigest()[:16]
        write(os.path.join(lock_dir, f"{digest}.lock"), "busy")
        report = apply_patch_in_isolated_worktree(proposal(valid_diff(), sha256_text(original)), mission, root)
        assert report["status"] == "FAILED", report
        assert any("validation did not pass" in caveat or "Path lock" in caveat for caveat in report["caveats"]), report
    finally:
        shutil.rmtree(root)


def assert_a5_critical_path_never_applies():
    root, _ = make_project()
    try:
        write(os.path.join(root, "src/auth/login.py"), "def login():\n    return True\n")
        mission = implementation_mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
            allowed_paths=["src/auth/login.py"],
            owned_paths=["src/auth/login.py"],
            critical_path_write_allowed=False,
            verification_policy={"allowed_commands": [], "max_commands": 0, "timeout_seconds": 20},
        )
        critical_diff = (
            "diff --git a/src/auth/login.py b/src/auth/login.py\n"
            "--- a/src/auth/login.py\n"
            "+++ b/src/auth/login.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def login():\n"
            "-    return True\n"
            "+    return False\n"
        )
        critical = proposal(critical_diff, sha256_text("def login():\n    return True\n"), "src/auth/login.py")
        report = apply_patch_in_isolated_worktree(critical, mission, root)
        assert report["status"] == "ESCALATE", report
        with open(os.path.join(root, "src/auth/login.py"), encoding="utf-8") as handle:
            assert handle.read() == "def login():\n    return True\n"
    finally:
        shutil.rmtree(root)


def assert_a6_critical_path_can_apply_in_isolation_only_with_explicit_policy():
    root, _ = make_project()
    try:
        write(os.path.join(root, "src/auth/login.py"), "def login():\n    return True\n")
        mission = implementation_mission(
            mission_id="mission_patch_critical",
            tier="A6",
            mode="critical_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
            risk_tier="critical",
            allowed_paths=["src/auth/login.py"],
            owned_paths=["src/auth/login.py"],
            read_only_paths=["src/auth/login.py"],
            critical_path_write_allowed=True,
            critical_path_reason="deterministic fixture critical-path proof",
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "critical_path_patch",
                "target": {
                    "required_changed_files": ["src/auth/login.py"],
                },
                "completion_criteria": ["isolated_apply_only", "verification_required"],
            },
            verification_policy={"allowed_commands": [["python3", "-m", "py_compile", "src/auth/login.py"]], "max_commands": 1, "timeout_seconds": 20},
        )
        critical_diff = (
            "diff --git a/src/auth/login.py b/src/auth/login.py\n"
            "--- a/src/auth/login.py\n"
            "+++ b/src/auth/login.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def login():\n"
            "-    return True\n"
            "+    return bool(True)\n"
        )
        critical = proposal(critical_diff, sha256_text("def login():\n    return True\n"), "src/auth/login.py")
        critical["risk_assessment"] = {
            "risk_tier": "critical",
            "critical_paths_touched": True,
            "blast_radius": "critical-path-fixture",
        }
        critical["verification_plan"] = [
            {"command": ["python3", "-m", "py_compile", "src/auth/login.py"], "reason": "Compile critical fixture."}
        ]
        report = apply_patch_in_isolated_worktree(critical, mission, root)
        assert report["status"] == "VERIFIED", report
        assert report["validation"]["checks"]["critical_paths_touched"] is True, report
        assert report["main_workspace_mutated"] is False, report
        with open(os.path.join(root, "src/auth/login.py"), encoding="utf-8") as handle:
            assert handle.read() == "def login():\n    return True\n"
    finally:
        shutil.rmtree(root)


def assert_a6_workspace_apply_requires_certification_artifact():
    root, _ = make_project()
    try:
        write(os.path.join(root, "src/auth/login.py"), "def login():\n    return True\n")
        mission = implementation_mission(
            mission_id="mission_patch_critical_workspace_missing_cert",
            tier="A6",
            mode="critical_implementation",
            write_allowed=True,
            apply_mode="critical_workspace_certified",
            risk_tier="critical",
            allowed_paths=["src/auth/login.py"],
            owned_paths=["src/auth/login.py"],
            read_only_paths=["src/auth/login.py"],
            critical_path_write_allowed=True,
            critical_path_reason="critical workspace certification test",
            workspace_apply_policy={
                "allow_critical_workspace_apply": True,
                "certification_required": True,
                "require_gpt_review": True,
                "reviewer_models": ["mission-a6-kimi"],
            },
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "critical_path_patch",
                "target": {"required_changed_files": ["src/auth/login.py"]},
                "completion_criteria": ["verification_required"],
            },
            verification_policy={"allowed_commands": [["python3", "-m", "py_compile", "src/auth/login.py"]], "max_commands": 1, "timeout_seconds": 20},
        )
        critical_diff = (
            "diff --git a/src/auth/login.py b/src/auth/login.py\n"
            "--- a/src/auth/login.py\n"
            "+++ b/src/auth/login.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def login():\n"
            "-    return True\n"
            "+    return bool(True)\n"
        )
        critical = proposal(critical_diff, sha256_text("def login():\n    return True\n"), "src/auth/login.py")
        critical["risk_assessment"] = {
            "risk_tier": "critical",
            "critical_paths_touched": True,
            "blast_radius": "critical-path-fixture",
        }
        critical["verification_plan"] = [
            {"command": ["python3", "-m", "py_compile", "src/auth/login.py"], "reason": "Compile critical fixture."}
        ]
        report = apply_patch_in_workspace(critical, mission, root)
        assert report["status"] == "FAILED", report
        assert any("certification artifact" in caveat for caveat in report["caveats"]), report
        with open(os.path.join(root, "src/auth/login.py"), encoding="utf-8") as handle:
            assert handle.read() == "def login():\n    return True\n"
    finally:
        shutil.rmtree(root)


def assert_a6_workspace_apply_can_be_certified_and_audited():
    root, _ = make_project()
    try:
        write(os.path.join(root, "src/auth/login.py"), "def login():\n    return True\n")
        raw = {
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "mission_patch_critical_workspace_certified",
            "tier": "A6",
            "mode": "critical_implementation",
            "objective": "Apply a critical-path fixture patch only after runtime certification review.",
            "risk_tier": "critical",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["src/auth/login.py"],
            "owned_paths": ["src/auth/login.py"],
            "read_only_paths": ["src/auth/login.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 6,
            "time_budget_seconds": 60,
            "stop_conditions": ["verified_apply", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "critical_workspace_certified",
            "critical_path_write_allowed": True,
            "critical_path_reason": "critical workspace certification deterministic test",
            "workspace_apply_policy": {
                "allow_critical_workspace_apply": True,
                "certification_required": True,
                "require_gpt_review": True,
                "reviewer_models": ["mission-a6-kimi"],
                "min_reviewer_approvals": 1,
                "require_isolated_preflight": True,
                "require_rollback_proof": True,
                "invariant_commands": [["python3", "-c", "pass"]],
            },
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "src/auth/login.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "critical_path_patch",
                "target": {"required_changed_files": ["src/auth/login.py"]},
                "completion_criteria": ["verification_required", "workspace_certification_required"],
            },
        }
        mission = _build_mission(raw)
        critical_diff = (
            "diff --git a/src/auth/login.py b/src/auth/login.py\n"
            "--- a/src/auth/login.py\n"
            "+++ b/src/auth/login.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def login():\n"
            "-    return True\n"
            "+    return bool(True)\n"
        )
        critical = proposal(critical_diff, sha256_text("def login():\n    return True\n"), "src/auth/login.py")
        critical["risk_assessment"] = {
            "risk_tier": "critical",
            "critical_paths_touched": True,
            "blast_radius": "critical-path-fixture",
        }
        critical["verification_plan"] = [
            {"command": ["python3", "-m", "py_compile", "src/auth/login.py"], "reason": "Compile critical fixture."}
        ]
        handoff = (
            "<OSS_HANDOFF_JSON>\n"
            + json.dumps(raw)
            + "\n</OSS_HANDOFF_JSON>\n"
            + "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(critical)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )

        def fake_call_model(messages, tools, timeout, model_alias_override=None):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "certification_review_version": "1.0",
                                    "approved": True,
                                    "confidence": "HIGH",
                                    "findings": ["Patch is scoped to the owned critical fixture path."],
                                    "rationale": "Validation is green, verification targets the changed file, and scope matches the mission.",
                                }
                            )
                        }
                    }
                ]
            }

        result = run_implementation_mission(
            mission=mission,
            raw_model_alias="mission-a6-kimi",
            handoff=handoff,
            call_model=fake_call_model,
            timeout=30,
            project_root=root,
        )
        assert result["status"] == "VERIFIED", result
        with open(os.path.join(root, ".codex-oss", "missions", "mission_patch_critical_workspace_certified", "certification.json"), encoding="utf-8") as handle:
            certification = json.load(handle)
        with open(os.path.join(root, ".codex-oss", "missions", "mission_patch_critical_workspace_certified", "report.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        with open(os.path.join(root, ".codex-oss", "missions", "mission_patch_critical_workspace_certified", "ledger.json"), encoding="utf-8") as handle:
            ledger = json.load(handle)
        with open(os.path.join(root, ".codex-oss", "missions", "mission_patch_critical_workspace_certified", "summary.md"), encoding="utf-8") as handle:
            summary = handle.read()
        assert os.path.exists(os.path.join(root, ".codex-oss", "missions", "mission_patch_critical_workspace_certified", "rollback.diff"))
        assert certification["approved_for_workspace_apply"] is True, certification
        assert certification["status"] == "APPROVED", certification
        assert certification["preflight"]["ok"] is True, certification
        assert certification["rollback_proof"]["ok"] is True, certification
        assert certification["approved_count"] == 1, certification
        assert certification["min_reviewer_approvals"] == 1, certification
        assert certification["invariant_results"][0]["exit_code"] == 0, certification
        assert report["certification_status"] == "APPROVED", report
        assert report["main_workspace_mutated"] is True, report
        assert report["semantic_review_score"] >= 100 - 25, report
        assert report["verification_plan_score"] >= 80, report
        assert report["rollback"]["artifact"].endswith("rollback.diff"), report
        assert ledger["report_status"] == "VERIFIED", ledger
        assert ledger["rollback_artifact"].endswith("rollback.diff"), ledger
        assert "Implementation Mission Summary" in summary, summary
        audited = audit_mission(root, "mission_patch_critical_workspace_certified")
        assert audited["ok"] is True, audited
    finally:
        shutil.rmtree(root)


def assert_a4_a5_mission_schema_is_explicit_about_writes():
    a4 = implementation_mission()
    assert a4.tier == "A4"
    assert a4.write_allowed is False
    assert a4.owned_paths == ["tests/test_config.py"]

    try:
        implementation_mission(tier="A4", write_allowed=True)
    except InvalidHandoffError as exc:
        assert "A4" in str(exc) and "write_allowed=false" in str(exc), exc
    else:
        raise AssertionError("A4 must not accept write_allowed=true")

    try:
        implementation_mission(tier="A5", mode="bounded_implementation", write_allowed=False)
    except InvalidHandoffError as exc:
        assert "A5" in str(exc) and "write_allowed=true" in str(exc), exc
    else:
        raise AssertionError("A5 must require explicit write_allowed=true")

    compiled = implementation_mission(
        tier="A5",
        mode="bounded_implementation",
        write_allowed=True,
        apply_mode="workspace_low_risk",
        workspace_apply_policy={"allow_direct_workspace_apply": True, "allow_dirty_target_files": False},
    )
    assert compiled.apply_mode == "workspace_low_risk"

    try:
        implementation_mission(
            mission_id="mission_patch_critical_workspace",
            tier="A6",
            mode="critical_implementation",
            write_allowed=True,
            apply_mode="critical_workspace_certified",
            risk_tier="critical",
            allowed_paths=["src/auth/login.py"],
            owned_paths=["src/auth/login.py"],
            read_only_paths=["src/auth/login.py"],
            critical_path_write_allowed=True,
            critical_path_reason="critical workspace certification test",
            workspace_apply_policy={
                "allow_critical_workspace_apply": True,
                "certification_required": True,
                "reviewer_models": ["gpt-5.5"],
                "min_reviewer_approvals": 1,
                "require_isolated_preflight": True,
                "require_rollback_proof": True,
                "invariant_commands": [["python3", "-c", "pass"]],
            },
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "critical_path_patch",
                "target": {"required_changed_files": ["src/auth/login.py"]},
                "completion_criteria": ["verification_required"],
            },
        )
    except InvalidHandoffError as exc:
        raise AssertionError(f"A6 critical_workspace_certified should parse: {exc}")


def main():
    assert_a4_accepts_valid_owned_patch()
    assert_patch_intent_builds_runtime_owned_diff()
    assert_desired_state_builds_python_function_patch()
    assert_desired_state_builds_unittest_method_patch()
    assert_desired_state_infers_unittest_method_type_from_shape()
    assert_desired_state_ignores_malformed_optional_metadata()
    assert_desired_state_already_satisfied_returns_status()
    assert_patch_intent_rejects_ambiguous_or_missing_anchors()
    assert_patch_intent_supports_replace_and_create_file()
    assert_patch_intent_normalizes_common_model_aliases()
    assert_patch_intent_uses_top_level_path_default()
    assert_patch_intent_infers_append_when_operation_missing()
    assert_patch_intent_noop_gets_targeted_repair()
    assert_malformed_desired_state_gets_targeted_repair()
    assert_a4_writes_runtime_artifact_bundle()
    assert_a4_writes_visible_commentary_and_patch_intent_artifacts()
    assert_patch_validator_rejects_path_escape_and_forbidden_paths()
    assert_patch_validator_escalates_critical_paths_and_blocks_secrets()
    assert_patch_validator_rejects_stale_base_and_oversized_patch()
    assert_patch_validator_enforces_test_only_objective_spec()
    assert_patch_validator_enforces_broad_implementation_objective_spec()
    assert_patch_validator_requires_evidence_shapes_for_broad_implementation()
    assert_a5_isolated_apply_verifies_without_mutating_main_workspace()
    assert_a5_isolated_apply_inside_repo_does_not_escape_to_parent_git()
    assert_a5_temp_project_apply_verifies_without_mutating_main_workspace()
    assert_a5_workspace_apply_is_policy_gated_and_reversible()
    assert_a5_workspace_apply_can_be_enabled_by_mission_policy_without_env()
    assert_a5_workspace_apply_rolls_back_on_verification_failure()
    assert_a5_workspace_apply_respects_existing_workspace_lock()
    assert_a5_workspace_apply_requires_clean_git_when_policy_enabled()
    assert_a5_workspace_apply_rejects_dirty_target_files_by_default()
    assert_path_locks_block_conflicting_apply()
    assert_a5_critical_path_never_applies()
    assert_a6_critical_path_can_apply_in_isolation_only_with_explicit_policy()
    assert_a6_workspace_apply_requires_certification_artifact()
    assert_a6_workspace_apply_can_be_certified_and_audited()
    assert_a4_a5_mission_schema_is_explicit_about_writes()
    print("PASS: A4/A5 patch pipeline suite")


if __name__ == "__main__":
    main()
