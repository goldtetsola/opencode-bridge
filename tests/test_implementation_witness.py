#!/usr/bin/env python3
"""Tests for ImplementationWitnessV1 terminal implementation gates."""

import sys

PASSED = 0
FAILED = 0


def _pass(name):
    global PASSED
    PASSED += 1
    print(f"  PASS {name}")


def _fail(name, detail=""):
    global FAILED
    FAILED += 1
    print(f"  FAIL {name}: {detail}")


class Mission:
    mission_id = "impl_witness_test"
    objective = "Change the owned target file"
    owned_paths = ["tests/owned.txt"]
    objective_spec = {
        "objective_type": "implementation_patch",
        "target": {"required_changed_files": ["tests/owned.txt"]},
    }


def assert_verified_requires_changed_files():
    name = "verified_requires_changed_files"
    from codex_oss.implementation import build_implementation_witness
    from codex_oss.read_evidence import build_canonical_patch_evidence

    witness = build_implementation_witness(
        status="VERIFIED",
        mission=Mission(),
        changed_files=[],
        canonical_patch=build_canonical_patch_evidence(mission_id="impl_witness_test", owned_paths=["tests/owned.txt"], changed_paths=[]),
        verification_scope={"level": "none"},
    )
    assert witness["ok"] is False, witness
    assert "changed_files_empty_for_successful_implementation" in witness["reasons"], witness
    assert "required_target_not_changed:tests/owned.txt" in witness["reasons"], witness
    _pass(name)


def assert_verified_requires_owned_paths():
    name = "verified_requires_owned_paths"
    from codex_oss.implementation import build_implementation_witness
    from codex_oss.read_evidence import build_canonical_patch_evidence

    witness = build_implementation_witness(
        status="VERIFIED",
        mission=Mission(),
        changed_files=["README.md"],
        canonical_patch=build_canonical_patch_evidence(mission_id="impl_witness_test", owned_paths=["tests/owned.txt"], changed_paths=["README.md"]),
        verification_scope={"level": "targeted"},
    )
    assert witness["ok"] is False, witness
    assert any(reason.startswith("changed_files_outside_owned_paths") for reason in witness["reasons"]), witness
    _pass(name)


def assert_verified_requires_targeted_verification():
    name = "verified_requires_targeted_verification"
    from codex_oss.implementation import build_implementation_witness
    from codex_oss.read_evidence import build_canonical_patch_evidence

    witness = build_implementation_witness(
        status="VERIFIED",
        mission=Mission(),
        changed_files=["tests/owned.txt"],
        canonical_patch=build_canonical_patch_evidence(mission_id="impl_witness_test", owned_paths=["tests/owned.txt"], changed_paths=["tests/owned.txt"]),
        verification_scope={"level": "broad"},
    )
    assert witness["ok"] is False, witness
    assert "verification_scope_not_targeted" in witness["reasons"], witness
    _pass(name)


def assert_verified_with_full_witness_passes():
    name = "verified_with_full_witness_passes"
    from codex_oss.implementation import build_implementation_witness
    from codex_oss.read_evidence import build_canonical_patch_evidence

    witness = build_implementation_witness(
        status="VERIFIED",
        mission=Mission(),
        changed_files=["tests/owned.txt"],
        canonical_patch=build_canonical_patch_evidence(mission_id="impl_witness_test", owned_paths=["tests/owned.txt"], changed_paths=["tests/owned.txt"]),
        verification_scope={"level": "targeted"},
    )
    assert witness["ok"] is True, witness
    assert witness["status_authority"] == "runtime", witness
    _pass(name)


def main():
    for test in [
        assert_verified_requires_changed_files,
        assert_verified_requires_owned_paths,
        assert_verified_requires_targeted_verification,
        assert_verified_with_full_witness_passes,
    ]:
        try:
            test()
        except Exception as exc:
            _fail(test.__name__, repr(exc))
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)

