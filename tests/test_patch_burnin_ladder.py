#!/usr/bin/env python3
"""Deterministic A4/A5 implementation burn-in ladder.

This is broader than the unit-style patch pipeline suite: each rung represents
one implementation capability we expect before trusting live OSS A4/A5 work.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.implementation import (  # noqa: E402
    apply_patch_in_isolated_worktree,
    build_patch_proposal_from_desired_state,
    build_patch_proposal_from_intent,
    build_patch_proposal_from_recipe,
    validate_patch_proposal,
)
from codex_oss.mission import _build_mission  # noqa: E402


BASE_TARGET = 'VALUE = "original"\n\n\ndef describe():\n    return VALUE\n'


def write_file(root: str, path: str, text: str) -> None:
    full_path = os.path.join(root, path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read_file(root: str, path: str) -> str:
    with open(os.path.join(root, path), encoding="utf-8") as handle:
        return handle.read()


def make_project() -> str:
    root = tempfile.mkdtemp(prefix="oss_patch_burnin_")
    write_file(root, "tests/fixtures/a4_http_target.py", BASE_TARGET)
    write_file(root, "tests/fixtures/a4_notes.md", "# Fixture Notes\n\nExisting note.\n")
    write_file(root, "tests/test_fixture_behavior.py", "def test_existing():\n    assert True\n")
    write_file(root, "tests/fixtures/a4_string_utils.py", "def normalize_label(value: str) -> str:\n    return \" \".join(value.strip().lower().split())\n")
    write_file(
        root,
        "tests/test_string_utils.py",
        "import os\nimport sys\nimport unittest\n\n"
        "ROOT = os.path.dirname(os.path.dirname(__file__))\n"
        "sys.path.insert(0, os.path.join(ROOT, 'tests', 'fixtures'))\n\n"
        "from a4_string_utils import normalize_label\n\n\n"
        "class NormalizeLabelTests(unittest.TestCase):\n"
        "    def test_strips_and_lowercases(self):\n"
        "        self.assertEqual(normalize_label('  Hello  '), 'hello')\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
    )
    return root


def mission(**overrides):
    raw = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": overrides.pop("mission_id", "mission_patch_burnin"),
        "tier": overrides.pop("tier", "A4"),
        "mode": overrides.pop("mode", "patch_proposal"),
        "objective": overrides.pop("objective", "Run a deterministic A4/A5 burn-in rung."),
        "risk_tier": "low",
        "write_allowed": overrides.pop("write_allowed", False),
        "allowed_roots": [],
        "allowed_paths": [
            "tests/fixtures/a4_http_target.py",
            "tests/fixtures/a4_notes.md",
            "tests/fixtures/a4_created.py",
            "tests/test_fixture_behavior.py",
            "tests/fixtures/a4_string_utils.py",
            "tests/test_string_utils.py",
        ],
        "owned_paths": [
            "tests/fixtures/a4_http_target.py",
            "tests/fixtures/a4_notes.md",
            "tests/fixtures/a4_created.py",
            "tests/test_fixture_behavior.py",
            "tests/fixtures/a4_string_utils.py",
            "tests/test_string_utils.py",
        ],
        "read_only_paths": [
            "tests/fixtures/a4_http_target.py",
            "tests/fixtures/a4_notes.md",
            "tests/test_fixture_behavior.py",
            "tests/fixtures/a4_string_utils.py",
            "tests/test_string_utils.py",
        ],
        "forbidden_roots": [".env", ".git", ".codex-oss/"],
        "allowed_tool_classes": ["read", "search"],
        "tool_budget": 4,
        "time_budget_seconds": 60,
        "stop_conditions": ["valid_patch", "deadline_reached"],
        "report_schema": "patch_validation_report.v1",
        "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
        "max_files_changed": 1,
        "max_patch_bytes": 12000,
        "verification_policy": {
            "allowed_commands": [["python3", "tests/fixtures/a4_http_target.py"]],
            "max_commands": 1,
            "timeout_seconds": 20,
        },
    }
    raw.update(overrides)
    return _build_mission(raw)


def assert_valid(proposal: dict, mission_obj, root: str) -> dict:
    validation = validate_patch_proposal(proposal, mission_obj, root)
    assert validation["status"] == "VALID", validation
    return validation


def rung_desired_state_python_function():
    root = make_project()
    try:
        mission_obj = mission(max_files_changed=1)
        desired_state = {
            "desired_state_version": "1.0",
            "summary": "Ensure live marker exists.",
            "assertions": [
                {
                    "type": "python_function_exists",
                    "path": "tests/fixtures/a4_http_target.py",
                    "function_name": "live_runtime_patch_marker",
                    "return_value": "live",
                }
            ],
        }
        proposal = build_patch_proposal_from_desired_state(desired_state, mission_obj, root)
        assert "+def live_runtime_patch_marker():" in proposal["unified_diff"], proposal
        assert_valid(proposal, mission_obj, root)
        assert read_file(root, "tests/fixtures/a4_http_target.py") == BASE_TARGET
    finally:
        shutil.rmtree(root)


def rung_patch_intent_append_docs_block():
    root = make_project()
    try:
        mission_obj = mission(owned_paths=["tests/fixtures/a4_notes.md"], max_files_changed=1)
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Append a docs-style fixture block.",
            "edits": [
                {
                    "operation": "append_to_file",
                    "path": "tests/fixtures/a4_notes.md",
                    "content": "\n## Runtime Patch Notes\n\nPatchBuilder created this block.\n",
                    "reason": "Exercise append_to_file on markdown text.",
                }
            ],
        }
        proposal = build_patch_proposal_from_intent(intent, mission_obj, root)
        assert "+## Runtime Patch Notes" in proposal["unified_diff"], proposal
        assert_valid(proposal, mission_obj, root)
        assert read_file(root, "tests/fixtures/a4_notes.md") == "# Fixture Notes\n\nExisting note.\n"
    finally:
        shutil.rmtree(root)


def rung_patch_intent_append_docs_block_with_live_aliases():
    root = make_project()
    try:
        mission_obj = mission(owned_paths=["tests/fixtures/a4_notes.md"], max_files_changed=1)
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Append a docs-style fixture block using live-style aliases.",
            "edits": [
                {
                    "operation": "append_to_file",
                    "target_file": "tests/fixtures/a4_notes.md",
                    "content_to_add": "\n## Runtime Patch Notes\n\nPatchBuilder normalized this block.\n",
                    "reason": "Exercise append aliases observed in live model outputs.",
                }
            ],
        }
        proposal = build_patch_proposal_from_intent(intent, mission_obj, root)
        assert "+## Runtime Patch Notes" in proposal["unified_diff"], proposal
        assert_valid(proposal, mission_obj, root)
    finally:
        shutil.rmtree(root)


def rung_patch_intent_replace_exact():
    root = make_project()
    try:
        mission_obj = mission(owned_paths=["tests/fixtures/a4_http_target.py"], max_files_changed=1)
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Replace a literal value.",
            "edits": [
                {
                    "operation": "replace_exact",
                    "path": "tests/fixtures/a4_http_target.py",
                    "old_text": 'VALUE = "original"',
                    "new_text": 'VALUE = "patched"',
                    "reason": "Exercise exact replacement.",
                }
            ],
        }
        proposal = build_patch_proposal_from_intent(intent, mission_obj, root)
        assert '+VALUE = "patched"' in proposal["unified_diff"], proposal
        assert_valid(proposal, mission_obj, root)
        assert read_file(root, "tests/fixtures/a4_http_target.py") == BASE_TARGET
    finally:
        shutil.rmtree(root)


def rung_patch_recipe_anchor_slot_insert():
    root = make_project()
    try:
        mission_obj = mission(owned_paths=["tests/fixtures/a4_http_target.py"], max_files_changed=1)
        recipe = {
            "patch_recipe_version": "1.0",
            "recipe_type": "insert_block_at_anchor",
            "target_file": "tests/fixtures/a4_http_target.py",
            "selected_anchor_id": "anchor:eof",
            "content": "\n\ndef recipe_runtime_patch_marker():\n    return \"recipe\"\n",
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False},
            "verification_plan": [{"command": ["python3", "tests/fixtures/a4_http_target.py"]}],
            "evidence_refs": ["file:tests/fixtures/a4_http_target.py#anchor:eof"],
            "caveats": [],
        }
        proposal = build_patch_proposal_from_recipe(recipe, mission_obj, root)
        assert "+def recipe_runtime_patch_marker():" in proposal["unified_diff"], proposal
        assert proposal["proposal_source"] == "patch_recipe_v1", proposal
        assert_valid(proposal, mission_obj, root)
        assert read_file(root, "tests/fixtures/a4_http_target.py") == BASE_TARGET
    finally:
        shutil.rmtree(root)


def rung_patch_intent_create_file():
    root = make_project()
    try:
        mission_obj = mission(
            allowed_paths=["tests/fixtures/a4_created.py"],
            owned_paths=["tests/fixtures/a4_created.py"],
            read_only_paths=[],
            max_files_changed=1,
        )
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Create a fixture file.",
            "edits": [
                {
                    "operation": "create_file",
                    "path": "tests/fixtures/a4_created.py",
                    "content": 'CREATED_VALUE = "runtime"\n',
                    "reason": "Exercise runtime-owned file creation.",
                }
            ],
        }
        proposal = build_patch_proposal_from_intent(intent, mission_obj, root)
        assert "+CREATED_VALUE" in proposal["unified_diff"], proposal
        assert_valid(proposal, mission_obj, root)
        assert not os.path.exists(os.path.join(root, "tests/fixtures/a4_created.py"))
    finally:
        shutil.rmtree(root)


def rung_a5_isolated_apply_multifile_low_risk():
    root = make_project()
    try:
        mission_obj = mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
            owned_paths=["tests/fixtures/a4_http_target.py", "tests/test_fixture_behavior.py"],
            max_files_changed=2,
            verification_policy={
                "allowed_commands": [["python3", "-m", "py_compile", "tests/fixtures/a4_http_target.py", "tests/test_fixture_behavior.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        )
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Add a helper and a matching low-risk test.",
            "edits": [
                {
                    "operation": "append_to_file",
                    "path": "tests/fixtures/a4_http_target.py",
                    "content": "\n\ndef multi_file_runtime_patch_marker():\n    return \"multi\"\n",
                    "reason": "Add fixture helper.",
                },
                {
                    "operation": "append_to_file",
                    "path": "tests/test_fixture_behavior.py",
                    "content": "\n\ndef test_multi_file_runtime_patch_marker_shape():\n    assert \"multi\"\n",
                    "reason": "Add matching low-risk test.",
                },
            ],
        }
        proposal = build_patch_proposal_from_intent(intent, mission_obj, root)
        assert "tests/fixtures/a4_http_target.py" in proposal["unified_diff"], proposal
        assert "tests/test_fixture_behavior.py" in proposal["unified_diff"], proposal
        report = apply_patch_in_isolated_worktree(proposal, mission_obj, root)
        assert report["status"] == "VERIFIED", report
        assert sorted(report["changed_files"]) == ["tests/fixtures/a4_http_target.py", "tests/test_fixture_behavior.py"], report
        assert read_file(root, "tests/fixtures/a4_http_target.py") == BASE_TARGET
        assert read_file(root, "tests/test_fixture_behavior.py") == "def test_existing():\n    assert True\n"
    finally:
        shutil.rmtree(root)


def rung_a5_isolated_realistic_test_only():
    root = make_project()
    original_test = read_file(root, "tests/test_string_utils.py")
    try:
        mission_obj = mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
            owned_paths=["tests/test_string_utils.py"],
            read_only_paths=["tests/fixtures/a4_string_utils.py", "tests/test_string_utils.py"],
            allowed_paths=["tests/fixtures/a4_string_utils.py", "tests/test_string_utils.py"],
            verification_policy={
                "allowed_commands": [["python3", "tests/test_string_utils.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        )
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Add a realistic unittest case only.",
            "edits": [
                {
                    "operation": "insert_after",
                    "path": "tests/test_string_utils.py",
                    "anchor": "    def test_strips_and_lowercases(self):\n        self.assertEqual(normalize_label('  Hello  '), 'hello')",
                    "content": "\n\n    def test_collapses_internal_whitespace(self):\n        self.assertEqual(normalize_label('A   B'), 'a b')",
                    "reason": "Add behavior coverage without changing source.",
                }
            ],
        }
        proposal = build_patch_proposal_from_intent(intent, mission_obj, root)
        assert "test_collapses_internal_whitespace" in proposal["unified_diff"], proposal
        report = apply_patch_in_isolated_worktree(proposal, mission_obj, root)
        assert report["status"] == "VERIFIED", report
        assert report["changed_files"] == ["tests/test_string_utils.py"], report
        assert read_file(root, "tests/test_string_utils.py") == original_test
    finally:
        shutil.rmtree(root)


def rung_a5_isolated_apply_keeps_main_workspace_clean():
    root = make_project()
    try:
        mission_obj = mission(
            tier="A5",
            mode="bounded_implementation",
            write_allowed=True,
            apply_mode="isolated_worktree",
            owned_paths=["tests/fixtures/a4_http_target.py"],
            verification_policy={
                "allowed_commands": [["python3", "tests/fixtures/a4_http_target.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        )
        desired_state = {
            "desired_state_version": "1.0",
            "summary": "Ensure isolated marker exists.",
            "assertions": [
                {
                    "type": "python_function_exists",
                    "path": "tests/fixtures/a4_http_target.py",
                    "function_name": "isolated_runtime_patch_marker",
                    "return_value": "isolated",
                }
            ],
        }
        proposal = build_patch_proposal_from_desired_state(desired_state, mission_obj, root)
        report = apply_patch_in_isolated_worktree(proposal, mission_obj, root)
        assert report["status"] == "VERIFIED", report
        assert "tests/fixtures/a4_http_target.py" in report["changed_files"], report
        assert read_file(root, "tests/fixtures/a4_http_target.py") == BASE_TARGET
        assert os.path.exists(os.path.join(root, report["patch_artifact"])), report
    finally:
        shutil.rmtree(root)


def main():
    rungs = [
        ("DesiredState python_function_exists", rung_desired_state_python_function),
        ("PatchIntent append_to_file docs block", rung_patch_intent_append_docs_block),
        ("PatchIntent append docs live aliases", rung_patch_intent_append_docs_block_with_live_aliases),
        ("PatchIntent replace_exact literal", rung_patch_intent_replace_exact),
        ("PatchRecipe anchor-slot insert", rung_patch_recipe_anchor_slot_insert),
        ("PatchIntent create_file", rung_patch_intent_create_file),
        ("A5 isolated multi-file low-risk", rung_a5_isolated_apply_multifile_low_risk),
        ("A5 isolated realistic test-only", rung_a5_isolated_realistic_test_only),
        ("A5 isolated apply keeps main clean", rung_a5_isolated_apply_keeps_main_workspace_clean),
    ]
    for name, func in rungs:
        func()
        print(f"  PASS: {name}")
    print("PASS: deterministic A4/A5 burn-in ladder")


if __name__ == "__main__":
    main()
