"""Implementation smoke ladder: A5.0 - A5.5 burn-in tests.

Runs owned-path implementation scenarios through the patch-mediated pipeline.
Offline tests use the implementation module directly. Live tests require
LIVE=1 and a running bridge at OSS_BRIDGE_URL.

Usage:
    python3 tests/test_implementation_smoke_ladder.py          # offline only
    LIVE=1 python3 tests/test_implementation_smoke_ladder.py   # full live burn-in
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

JSON = dict[str, Any]

LIVE_MODE = os.getenv("LIVE", "0") == "1"
BRIDGE_URL = os.getenv("OSS_BRIDGE_URL", "http://127.0.0.1:4000/v1")
AUTH = os.getenv("PROXY_API_KEY", "sk-local-codex-bridge")
LIVE_MODELS = os.getenv("LIVE_MODELS", "oss_deepseek_pro").split(",")

PASSED = 0
FAILED = 0
SKIPPED = 0


def _pass(name: str, detail: str = "") -> None:
    global PASSED; PASSED += 1
    print(f"  {'PASS' if not detail else 'PASS: ' + detail:60s} {name}")


def _fail(name: str, detail: str = "") -> None:
    global FAILED; FAILED += 1
    print(f"  {'FAIL' if not detail else 'FAIL: ' + detail:60s} {name}")


def _skip(name: str, reason: str = "") -> None:
    global SKIPPED; SKIPPED += 1
    print(f"  SKIP ({reason}){'':53s} {name}")


# ═══════════════════════════════════════════════════════════════════════════
# A5.0 — Exact owned file write
# ═══════════════════════════════════════════════════════════════════════════


def a5_0_exact_owned_file_write_offline():
    """A5.0: Runtime builds and validates exact owned-file write offline."""
    name = "a5_0_exact_owned_file_write"
    from codex_oss.implementation import build_patch_proposal_from_intent

    with tempfile.TemporaryDirectory() as tmp:
        owned = os.path.join(tmp, "scratch.txt")
        with open(owned, "w") as f:
            f.write("line 1\nline 2\nline 3\n")

        intent = {
            "patch_intent_version": "1.0",
            "summary": "Replace line 1",
            "edits": [{
                "operation": "replace_exact",
                "path": "scratch.txt",
                "old_text": "line 1\n",
                "new_text": "replaced line 1\n",
                "reason": "Test A5.0",
            }],
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "scratch-only"},
            "verification_plan": [{"command": ["cat", "scratch.txt"], "reason": "verify content"}],
            "evidence_refs": ["file:scratch.txt"],
            "caveats": ["Scratch-only test."],
        }

        # Create a mission stub
        class Mission:
            mission_id = "a5_0_test"
            tier = "A5"
            mode = "bounded_implementation"
            objective = "Replace line in owned file"
            owned_paths = ["scratch.txt"]
            read_only_paths = ["scratch.txt"]
            allowed_paths = ["scratch.txt"]
            risk_tier = "low"
            max_files_changed = 1
            max_patch_bytes = 12000
            apply_mode = "isolated_worktree"
            verification_policy = {"allowed_commands": [["cat", "scratch.txt"]], "max_commands": 1, "timeout_seconds": 10}
            critical_path_write_allowed = False

        mission = Mission()

        try:
            proposal = build_patch_proposal_from_intent(intent, mission, tmp)
        except ValueError as exc:
            _fail(name, f"build failed: {exc}")
            return

        # Verify the proposal
        assert proposal["status"] == "PROPOSED"
        assert len(proposal["changed_files"]) == 1
        assert proposal["changed_files"][0]["path"] == "scratch.txt"
        assert "replaced line 1" in proposal["unified_diff"]

        # Apply the diff manually
        with open(owned) as f:
            content = f.read()
        assert "replaced line 1" not in content  # before
        # Apply via patch
        import subprocess
        patch_file = os.path.join(tmp, "patch.diff")
        with open(patch_file, "w") as f:
            f.write(proposal["unified_diff"])
        subprocess.run(["git", "apply", patch_file], cwd=tmp, check=True)
        with open(owned) as f:
            content = f.read()
        assert "replaced line 1" in content  # after
        _pass(name, "owned file write validated")


def a5_0_exact_owned_file_write_live():
    """A5.0: Live owned-file write via bridge."""
    name = "a5_0_exact_owned_file_write_live"
    if not LIVE_MODE:
        _skip(name, "set LIVE=1")
        return

    for model in LIVE_MODELS:
        _run_a5_live_test(
            f"{name} [{model}]",
            model,
            mission_tier="A5",
            owned_paths=["tests/scratch_a5_0.txt"],
            intent_edits=[{
                "operation": "replace_exact",
                "path": "tests/scratch_a5_0.txt",
                "old_text": "MARKER_A5_0\n",
                "new_text": "A5_0_PASS\n",
                "reason": "A5.0 burn-in",
            }],
        )


# ═══════════════════════════════════════════════════════════════════════════
# A5.1 — Marker append
# ═══════════════════════════════════════════════════════════════════════════


def a5_1_marker_append_offline():
    """A5.1: Runtime appends marker to owned file."""
    name = "a5_1_marker_append"
    from codex_oss.implementation import build_patch_proposal_from_intent

    with tempfile.TemporaryDirectory() as tmp:
        owned = os.path.join(tmp, "scratch.txt")
        with open(owned, "w") as f:
            f.write("existing content\n")

        intent = {
            "patch_intent_version": "1.0",
            "summary": "Append marker",
            "edits": [{
                "operation": "append_to_file",
                "path": "scratch.txt",
                "content": "\nMARKER_APPENDED",
                "reason": "Test A5.1",
            }],
            "risk_assessment": {"risk_tier": "low"},
            "verification_plan": [],
            "evidence_refs": [],
            "caveats": ["Test only."],
        }

        class Mission:
            mission_id = "a5_1_test"
            tier = "A5"
            objective = "Append marker"
            owned_paths = ["scratch.txt"]
            read_only_paths = ["scratch.txt"]
            allowed_paths = ["scratch.txt"]
            risk_tier = "low"
            max_files_changed = 1
            max_patch_bytes = 12000
            apply_mode = "isolated_worktree"
            verification_policy = {"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10}
            critical_path_write_allowed = False

        try:
            proposal = build_patch_proposal_from_intent(intent, Mission(), tmp)
        except ValueError as exc:
            _fail(name, f"build failed: {exc}")
            return

        assert "MARKER_APPENDED" in proposal["unified_diff"]
        _pass(name, "marker append validated")


def a5_1_marker_append_live():
    """A5.1: Live marker append via bridge."""
    name = "a5_1_marker_append_live"
    if not LIVE_MODE:
        _skip(name, "set LIVE=1")
        return

    for model in LIVE_MODELS:
        _run_a5_live_test(
            f"{name} [{model}]",
            model,
            mission_tier="A5",
            owned_paths=["tests/scratch_a5_1.txt"],
            intent_edits=[{
                "operation": "append_to_file",
                "path": "tests/scratch_a5_1.txt",
                "content": "\nA5_1_MARKER_APPENDED",
                "reason": "A5.1 burn-in",
            }],
        )


# ═══════════════════════════════════════════════════════════════════════════
# A5.2 — Replace exact line
# ═══════════════════════════════════════════════════════════════════════════


def a5_2_replace_exact_line_offline():
    """A5.2: Exact line replacement in owned file."""
    name = "a5_2_replace_exact_line"
    from codex_oss.implementation import build_patch_proposal_from_intent

    with tempfile.TemporaryDirectory() as tmp:
        owned = os.path.join(tmp, "scratch.txt")
        content = "line_a\nline_b\nline_c\n"
        with open(owned, "w") as f:
            f.write(content)

        intent = {
            "patch_intent_version": "1.0",
            "summary": "Replace line_b",
            "edits": [{
                "operation": "replace_exact",
                "path": "scratch.txt",
                "old_text": "line_b\n",
                "new_text": "LINE_B_REPLACED\n",
                "reason": "Test A5.2",
            }],
            "risk_assessment": {"risk_tier": "low"},
            "verification_plan": [],
            "evidence_refs": [],
            "caveats": ["Test only."],
        }

        class Mission:
            mission_id = "a5_2_test"
            tier = "A5"
            owned_paths = ["scratch.txt"]
            read_only_paths = ["scratch.txt"]
            allowed_paths = ["scratch.txt"]
            risk_tier = "low"
            max_files_changed = 1
            max_patch_bytes = 12000
            apply_mode = "isolated_worktree"
            verification_policy = {"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10}
            critical_path_write_allowed = False

        try:
            proposal = build_patch_proposal_from_intent(intent, Mission(), tmp)
        except ValueError as exc:
            _fail(name, f"build failed: {exc}")
            return

        assert "LINE_B_REPLACED" in proposal["unified_diff"]
        assert "line_b" in proposal["unified_diff"]  # removed
        _pass(name, "exact line replace validated")


# ═══════════════════════════════════════════════════════════════════════════
# A5.3 — Two-file owned docs edit
# ═══════════════════════════════════════════════════════════════════════════


def a5_3_two_file_owned_edit_offline():
    """A5.3: Two-file owned edit."""
    name = "a5_3_two_file_owned_edit"
    from codex_oss.implementation import build_patch_proposal_from_intent

    with tempfile.TemporaryDirectory() as tmp:
        file_a = os.path.join(tmp, "doc_a.md")
        file_b = os.path.join(tmp, "doc_b.md")
        with open(file_a, "w") as f:
            f.write("# Doc A\n\nContent A\n")
        with open(file_b, "w") as f:
            f.write("# Doc B\n\nContent B\n")

        intent = {
            "patch_intent_version": "1.0",
            "summary": "Update two docs",
            "edits": [
                {
                    "operation": "replace_exact",
                    "path": "doc_a.md",
                    "old_text": "Content A\n",
                    "new_text": "Content A (updated)\n",
                    "reason": "Update doc A",
                },
                {
                    "operation": "replace_exact",
                    "path": "doc_b.md",
                    "old_text": "Content B\n",
                    "new_text": "Content B (updated)\n",
                    "reason": "Update doc B",
                },
            ],
            "risk_assessment": {"risk_tier": "low"},
            "verification_plan": [],
            "evidence_refs": [],
            "caveats": ["Two-file test."],
        }

        class Mission:
            mission_id = "a5_3_test"
            tier = "A5"
            owned_paths = ["doc_a.md", "doc_b.md"]
            read_only_paths = ["doc_a.md", "doc_b.md"]
            allowed_paths = ["doc_a.md", "doc_b.md"]
            risk_tier = "low"
            max_files_changed = 2
            max_patch_bytes = 20000
            apply_mode = "isolated_worktree"
            verification_policy = {"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10}
            critical_path_write_allowed = False

        try:
            proposal = build_patch_proposal_from_intent(intent, Mission(), tmp)
        except ValueError as exc:
            _fail(name, f"build failed: {exc}")
            return

        changed = proposal["changed_files"]
        assert len(changed) == 2, f"expected 2 changed files, got {len(changed)}"
        assert "Content A (updated)" in proposal["unified_diff"]
        assert "Content B (updated)" in proposal["unified_diff"]
        _pass(name, "two-file edit validated")


# ═══════════════════════════════════════════════════════════════════════════
# A5.4 — Failed forbidden path
# ═══════════════════════════════════════════════════════════════════════════


def a5_4_forbidden_path_blocked():
    """A5.4: Writing to a forbidden path is blocked by validation."""
    name = "a5_4_forbidden_path_blocked"
    from codex_oss.implementation import build_patch_proposal_from_intent, validate_patch_proposal

    with tempfile.TemporaryDirectory() as tmp:
        # Create a file outside owned paths
        forbidden = os.path.join(tmp, ".git", "config")
        os.makedirs(os.path.dirname(forbidden), exist_ok=True)
        with open(forbidden, "w") as f:
            f.write("[core]\n")

        intent = {
            "patch_intent_version": "1.0",
            "summary": "Try to edit .git/config",
            "edits": [{
                "operation": "append_to_file",
                "path": ".git/config",
                "content": "\n# malicious",
                "reason": "This should be blocked",
            }],
            "risk_assessment": {"risk_tier": "critical"},
            "verification_plan": [],
            "evidence_refs": [],
            "caveats": [],
        }

        class Mission:
            mission_id = "a5_4_test"
            tier = "A5"
            owned_paths = [".git/config"]  # Declared as owned but should still be denied
            read_only_paths = [".git/config"]
            allowed_paths = [".git/config"]
            risk_tier = "critical"
            max_files_changed = 1
            max_patch_bytes = 12000
            apply_mode = "isolated_worktree"
            verification_policy = {"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10}
            critical_path_write_allowed = False

        mission = Mission()

        try:
            proposal = build_patch_proposal_from_intent(intent, mission, tmp)
        except ValueError as exc:
            # Should fail because .git/config path is denied
            if "deny" in str(exc).lower() or "forbidden" in str(exc).lower() or "critical" in str(exc).lower() or "safe" in str(exc).lower():
                _pass(name, f"correctly blocked: {exc}")
                return
            _fail(name, f"unexpected error: {exc}")
            return

        # If it didn't fail at build time, it should fail at validation
        validation = validate_patch_proposal(proposal, mission, tmp)
        if validation["status"] == "INVALID":
            _pass(name, "blocked at validation")
        else:
            _fail(name, f"should be INVALID, got {validation['status']}")


# ═══════════════════════════════════════════════════════════════════════════
# A5.5 — Readback verification failure
# ═══════════════════════════════════════════════════════════════════════════


def a5_5_readback_mismatch_detected():
    """A5.5: Base hash mismatch is detected during validation."""
    name = "a5_5_readback_mismatch_detected"
    from codex_oss.implementation import build_patch_proposal_from_intent, validate_patch_proposal

    with tempfile.TemporaryDirectory() as tmp:
        owned = os.path.join(tmp, "scratch.txt")
        with open(owned, "w") as f:
            f.write("original content\n")

        # Create intent with wrong base hash
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Replace content",
            "edits": [{
                "operation": "replace_exact",
                "path": "scratch.txt",
                "old_text": "original content\n",
                "new_text": "new content\n",
                "reason": "Test A5.5",
            }],
            "risk_assessment": {"risk_tier": "low"},
            "verification_plan": [],
            "evidence_refs": [],
            "caveats": [],
        }

        class Mission:
            mission_id = "a5_5_test"
            tier = "A5"
            owned_paths = ["scratch.txt"]
            read_only_paths = ["scratch.txt"]
            allowed_paths = ["scratch.txt"]
            risk_tier = "low"
            max_files_changed = 1
            max_patch_bytes = 12000
            apply_mode = "isolated_worktree"
            verification_policy = {"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10}
            critical_path_write_allowed = False

        mission = Mission()

        try:
            proposal = build_patch_proposal_from_intent(intent, mission, tmp)
        except ValueError as exc:
            _fail(name, f"unexpected build error: {exc}")
            return

        # Corrupt the base hash to simulate mismatch
        proposal["changed_files"][0]["base_sha256"] = "deadbeef" * 8

        validation = validate_patch_proposal(proposal, mission, tmp)
        if not validation["checks"].get("base_sha_match", True):
            _pass(name, "base hash mismatch correctly detected")
        elif validation["status"] == "INVALID":
            _pass(name, f"validation failed: {validation.get('reasons', [])}")
        else:
            _fail(name, f"should detect mismatch, got status={validation['status']}")


# ═══════════════════════════════════════════════════════════════════════════
# CanonicalPatchEvidenceV1 integration
# ═══════════════════════════════════════════════════════════════════════════


def canonical_patch_evidence_schema():
    """Verify CanonicalPatchEvidenceV1 produces valid JSON."""
    name = "canonical_patch_evidence_schema"
    from codex_oss.read_evidence import build_canonical_patch_evidence

    evidence = build_canonical_patch_evidence(
        mission_id="test",
        owned_paths=["tests/scratch.txt"],
        changed_paths=["tests/scratch.txt"],
        write_status="applied",
        readback_status="verified",
        before_hashes={"tests/scratch.txt": "abc123"},
        after_hashes={"tests/scratch.txt": "def456"},
        writes_outside_owned_paths=False,
        verification_status="passed",
        verification_method="readback_exact_match",
        rollback_available=True,
    )
    dumped = json.dumps(evidence)
    parsed = json.loads(dumped)
    assert parsed["schema_version"] == "canonical_patch_evidence.v1"
    assert parsed["writes_outside_owned_paths"] is False
    assert parsed["verification"]["status"] == "passed"
    _pass(name, "schema valid")


def implementation_narrative_schema():
    """Verify ImplementationNarrativeDraftV1 produces valid JSON."""
    name = "implementation_narrative_schema"
    from codex_oss.read_evidence import (
        build_implementation_narrative_draft,
        validate_implementation_narrative_draft,
    )

    narrative = build_implementation_narrative_draft(
        change_summary="Updated scratch file with marker",
        verification_summary="Readback matched expected content",
        risk_summary="Only the declared owned path was changed",
        caveats=["No broader test suite was run"],
    )
    valid, errors = validate_implementation_narrative_draft(narrative, ["scratch.txt"])
    assert valid, f"validation errors: {errors}"

    # Test rejection of forbidden authority claims
    bad_narrative = {
        "schema_version": "implementation_narrative_draft.v1",
        "change_summary": "I verified the patch and it passed",
    }
    valid2, errors2 = validate_implementation_narrative_draft(bad_narrative)
    assert not valid2, "should reject forbidden authority claim"

    _pass(name, "schema and validation OK")


# ═══════════════════════════════════════════════════════════════════════════
# Live helpers
# ═══════════════════════════════════════════════════════════════════════════


def _run_a5_live_test(
    name: str,
    model: str,
    mission_tier: str,
    owned_paths: list[str],
    intent_edits: list[JSON],
) -> None:
    """Run a live A5 implementation test through the bridge."""
    mission_id = f"mission_live_{name.replace(' ', '_').replace('[','').replace(']','')}_{int(time.time())}"
    scratch_path = owned_paths[0] if owned_paths else "tests/scratch.txt"

    # Create scratch file
    scratch = Path(os.getcwd()) / scratch_path
    scratch.parent.mkdir(parents=True, exist_ok=True)
    original = "MARKER_A5_0\n" if "a5_0" in name.lower() else "A5_SCRATCH_CONTENT\n"
    scratch.write_text(original, encoding="utf-8")

    try:
        mission = {
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": mission_tier,
            "mode": "bounded_implementation",
            "objective": f"Implementation smoke: {name}",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": owned_paths,
            "owned_paths": owned_paths,
            "read_only_paths": owned_paths,
            "forbidden_roots": [".env", ".git"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": len(owned_paths),
            "max_patch_bytes": 12000,
            "apply_mode": "isolated_worktree",
            "verification_policy": {
                "allowed_commands": [["cat", scratch_path]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        }

        intent = {
            "patch_intent_version": "1.0",
            "status": "PROPOSED",
            "summary": f"Implementation smoke: {name}",
            "edits": intent_edits,
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "scratch-only"},
            "verification_plan": [{"command": ["cat", scratch_path], "reason": "Verify content"}],
            "evidence_refs": [f"file:{scratch_path}"],
            "caveats": ["Scratch-only burn-in test."],
        }

        body = {
            "model": f"mission-a5-{model.replace('oss_', '')}",
            "stream": False,
            "input": [{
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    + json.dumps(mission)
                    + "\n</OSS_HANDOFF_JSON>\n\n<OSS_PATCH_INTENT_JSON>\n"
                    + json.dumps(intent)
                    + "\n</OSS_PATCH_INTENT_JSON>"
                ),
            }],
        }

        req = urllib.request.Request(
            f"{BRIDGE_URL}/responses",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {AUTH}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        text = _extract_text(payload)
        status = payload.get("status", "unknown")

        # Check results
        artifact_dir = Path(os.getcwd()) / ".codex-oss" / "missions" / mission_id
        report = _read_json(artifact_dir / "report.json")
        verification = _read_json(artifact_dir / "verification.json")

        main_unchanged = scratch.read_text(encoding="utf-8") == original

        checks = [
            ("Status VERIFIED", "Status: VERIFIED" in text or (isinstance(report, dict) and report.get("status") == "VERIFIED")),
            ("Main workspace unchanged", main_unchanged),
            ("No writes outside owned", isinstance(report, dict) and not report.get("main_workspace_mutated", True)),
            ("Visible commentary exists", (artifact_dir / "visible_commentary.jsonl").exists()),
            ("Summary exists", (artifact_dir / "summary.md").exists()),
            ("Report exists", (artifact_dir / "report.json").exists()),
        ]

        all_ok = True
        for check_name, ok in checks:
            if not ok:
                _fail(f"{name}: {check_name}", f"model={model}")
                all_ok = False

        if all_ok:
            _pass(name, f"model={model}")

    except Exception as exc:
        _fail(name, f"model={model}: {exc}")
    finally:
        try:
            scratch.unlink()
        except FileNotFoundError:
            pass


def _extract_text(payload: JSON) -> str:
    parts = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    print()
    print("=" * 70)
    print(" Implementation Smoke Ladder — A5.0 to A5.5")
    print("=" * 70)
    print(f" Live mode: {LIVE_MODE}")
    if LIVE_MODE:
        print(f" Bridge URL: {BRIDGE_URL}")
        print(f" Models: {LIVE_MODELS}")
    print()

    print("── A5.0: Exact owned file write ──")
    a5_0_exact_owned_file_write_offline()
    if LIVE_MODE:
        a5_0_exact_owned_file_write_live()

    print("\n── A5.1: Marker append ──")
    a5_1_marker_append_offline()
    if LIVE_MODE:
        a5_1_marker_append_live()

    print("\n── A5.2: Replace exact line ──")
    a5_2_replace_exact_line_offline()

    print("\n── A5.3: Two-file owned docs edit ──")
    a5_3_two_file_owned_edit_offline()

    print("\n── A5.4: Forbidden path blocked ──")
    a5_4_forbidden_path_blocked()

    print("\n── A5.5: Readback mismatch detected ──")
    a5_5_readback_mismatch_detected()

    print("\n── CanonicalPatchEvidenceV1 + ImplementationNarrativeDraftV1 ──")
    canonical_patch_evidence_schema()
    implementation_narrative_schema()

    print()
    print("=" * 70)
    print(f" Results: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    print("=" * 70)
    print()

    return FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)
