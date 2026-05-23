#!/usr/bin/env python3
"""v12 parser regression tests for structured Codex OSS handoffs."""

import os
import subprocess
import sys

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bridge import extract_allowed_paths, extract_required_deliverables, parse_task_envelope, select_mode
from bridge import build_context_pack, build_context_pack_deterministic_report, build_task_session
from bridge import build_patch_contract_report, collect_owned_path_changes, is_intent_or_status, validate_report_output
from bridge import evaluate_evidence_coverage, tool_output_indicates_failure, verification_contract_requested
from bridge import _extract_handoff_text, extract_search_terms_from_step
from codex_oss.handoff import validate_handoff_text


HANDOFF = """ROLE
OSS read-only scout for a local proof runbook.

GOAL
Produce a concise, evidence-backed checklist for the next local proof attempt.

TASK TYPE
read-only repo/runbook/run-artifact scouting

OWNED PATHS
none.

READ-ONLY PATHS
- /tmp/example-project/.codex/napkin.md
- /tmp/example-project/docs/CONTINUITY.md
- /tmp/example-project/docs/ops/local-proof-runbook.md
- /tmp/example-project/tmp/proof-run-001
- /tmp/example-project/scripts
- /tmp/example-project/docs

DO NOT TOUCH
Do not edit files. Do not run live proof.

VERIFICATION STEPS
Inspect only enough to answer: what must the next proof runner do before and during compute?

DELIVERABLE
Return exactly:
1. Confidence and caveats.
2. Five-to-eight item next-proof checklist.
3. One-line recommended monitor rule for compute.
4. Any runbook/doc gap you see, marked UNCONFIRMED if not fully proven.
"""


def main():
    root = os.path.dirname(os.path.dirname(__file__))
    paths = extract_allowed_paths(HANDOFF)
    assert len(paths) == 6, paths
    assert paths[0].endswith(".codex/napkin.md"), paths[0]
    assert paths[-1].endswith("docs"), paths[-1]

    deliverables = extract_required_deliverables(HANDOFF)
    assert any("Five-to-eight item" in d for d in deliverables), deliverables

    envelope = parse_task_envelope(HANDOFF)
    assert len(envelope["read_only_paths"]) == 6, envelope
    assert not envelope["no_tools_required"], envelope
    assert select_mode(envelope) == "context_pack_report", envelope

    scout = """ROLE: OSS read-only scout for a monitor-query blocker.
GOAL: Locate the run-scoped monitor wrapper/query.
TASK TYPE: read-only repo and artifact scouting.
OWNED PATHS: none.
READ-ONLY PATHS: tests, README.md
VERIFICATION STEPS: Search repo scripts/docs and the run dir for `missing_column_name`, `expected_column_name`, `execution_attempts.id`, `monitor`, and query snippets.
DELIVERABLE: Return confidence/caveats, exact file(s)/artifact(s) containing the bad query if found, recommended minimal fix, and commands/tests to run after fixing.
"""
    session = build_task_session({"input": []}, scout, "resp_test")
    pack = build_context_pack(session, os.path.dirname(os.path.dirname(__file__)))
    assert "rtk grep missing_column_name in tests" in pack, pack[:500]
    report = build_context_pack_deterministic_report(session, pack, "")
    assert "Summary:" in report, report
    assert "# Napkin" not in report, report

    unquoted_step = (
        "Search repo scripts/docs/run dir for terminal_blocker_state, "
        "append_timeout_reason, append, Notion, "
        "11111111-2222-3333-4444-555555555555, and query snippets. "
        "Inspect only relevant hits."
    )
    terms = extract_search_terms_from_step(unquoted_step)
    assert "terminal_blocker_state" in terms, terms
    assert "append_timeout_reason" in terms, terms
    assert "11111111-2222-3333-4444-555555555555" in terms, terms
    assert "query snippets" not in terms, terms

    direct_find_terms = extract_search_terms_from_step(
        "Find the OSS_HANDOFF_JSON and validate-handoff instructions"
    )
    assert "OSS_HANDOFF_JSON" in direct_find_terms, direct_find_terms
    assert "validate-handoff" in direct_find_terms, direct_find_terms

    unquoted_scout = """ROLE: OSS read-only scout for a terminal publish blocker.
GOAL: Locate exact evidence for the next diagnosis.
TASK TYPE: read-only repo and artifact scouting.
OWNED PATHS: none.
READ-ONLY PATHS: tests/fixtures, README.md
VERIFICATION STEPS: Search repo scripts/docs/run dir for terminal_blocker_state, append_timeout_reason, append, Notion, 11111111-2222-3333-4444-555555555555, and query snippets. Inspect only relevant hits.
DELIVERABLE: Return confidence/caveats, exact files/artifacts with evidence, likely owner code path, recommended next diagnostic/fix step, and commands/tests to run after a fix.
"""
    unquoted_session = build_task_session({"input": []}, unquoted_scout, "resp_unquoted")
    unquoted_pack = build_context_pack(unquoted_session, os.path.dirname(os.path.dirname(__file__)))
    assert "rtk grep terminal_blocker_state in tests/fixtures" in unquoted_pack, unquoted_pack[:500]
    assert "rtk grep query snippets" not in unquoted_pack, unquoted_pack[:1000]
    unquoted_report = build_context_pack_deterministic_report(unquoted_session, unquoted_pack, "")
    assert "terminal_blocker_state" in unquoted_report, unquoted_report
    assert "0 matches" not in unquoted_report.split("Summary:", 1)[1].split("Evidence snippets:", 1)[0], unquoted_report

    polluted_history = [
        {"role": "system", "content": "ROLE: old scout\nREAD-ONLY PATHS: old.txt\nDELIVERABLE: old result"},
        {"role": "user", "content": unquoted_scout},
    ]
    extracted = _extract_handoff_text(polluted_history)
    assert "terminal publish blocker" in extracted, extracted
    assert "old.txt" not in extracted, extracted

    escape_scout = """ROLE: OSS read-only scout.
TASK TYPE: read-only repo scouting.
READ-ONLY PATHS: /tmp/outside-project-secret.txt
DELIVERABLE: Return findings.
"""
    escape_session = build_task_session({"input": []}, escape_scout, "resp_escape")
    escape_pack = build_context_pack(escape_session, os.path.dirname(os.path.dirname(__file__)))
    assert "path escapes project root" in escape_pack, escape_pack

    structured = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Read-only scout","goal":"Find evidence","task_type":"scout","owned_paths":[],"read_only_paths":["tests/fixtures"],"forbidden_actions":["edit files"],"verification_steps":["Search for terminal_blocker_state"],"deliverable_fields":["confidence","evidence"],"completion_rule":"stop after report","escalation_rule":"stop on critical path"}
"""
    structured_envelope = parse_task_envelope(structured)
    assert structured_envelope["role"] == "Read-only scout", structured_envelope
    assert structured_envelope["read_only_paths"] == ["tests/fixtures"], structured_envelope
    assert structured_envelope["deliverable_fields"] == ["confidence", "evidence"], structured_envelope
    assert select_mode(structured_envelope) == "context_pack_report", structured_envelope
    structured_session = build_task_session({"input": []}, structured, "resp_structured")
    assert structured_session.required_paths == ["tests/fixtures"], structured_session
    assert structured_session.required_outputs == ["confidence", "evidence"], structured_session

    generic_report_contract = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Generic report scout","goal":"Produce a contract-shaped report from known files","task_type":"scout","owned_paths":[],"read_only_paths":["tests/fixtures","README.md"],"forbidden_actions":["Do not edit files"],"verification_steps":["Search read-only paths for terminal_blocker_state, append_timeout_reason, missing_generic_contract_term"],"deliverable_fields":["summary","evidence_table","touchpoint_check","falsification_notes","pending_gaps","confidence","caveats"],"completion_rule":"stop after report","escalation_rule":"stop if source pack is partial"}
"""
    generic_envelope = parse_task_envelope(generic_report_contract)
    assert select_mode(generic_envelope) == "context_pack_report", generic_envelope
    generic_session = build_task_session({"input": []}, generic_report_contract, "resp_contract")
    generic_pack = build_context_pack(generic_session, root)
    assert "rtk grep missing_generic_contract_term in README.md" in generic_pack, generic_pack[:2000]
    coverage_ok, missing_coverage, covered_paths, covered_terms = evaluate_evidence_coverage(generic_envelope, generic_pack)
    assert coverage_ok, missing_coverage
    assert "tests/fixtures" in covered_paths, covered_paths
    assert "missing_generic_contract_term" in covered_terms, covered_terms
    generic_report = build_context_pack_deterministic_report(generic_session, generic_pack, "")
    assert generic_report.startswith("PARTIAL\n"), generic_report
    assert "Transport status: PASS" in generic_report, generic_report
    assert "Task status: PARTIAL" in generic_report, generic_report
    assert "evidence_coverage:" in generic_report, generic_report
    assert "- status: PASS" in generic_report, generic_report
    generic_report_lower = generic_report.lower()
    for field in generic_envelope["deliverable_fields"]:
        assert f"{field.lower()}:" in generic_report_lower, generic_report
    first_person_structured_report = (
        "PARTIAL\n"
        "Summary: I inspected README.md and extracted the project purpose.\n"
        "Evidence: README.md says Rorschach provides automated diagnostics for ad creatives.\n"
        "Files gathered: README.md\n"
        "Confidence: MEDIUM\n"
        "Caveats: Only README.md was inspected.\n"
    )
    assert not is_intent_or_status(first_person_structured_report), first_person_structured_report
    thin_report = (
        "PASS\n"
        "Summary: gathered the requested source files and produced a short note from available context.\n"
        "Confidence: HIGH\n"
        "Caveats: none listed."
    )
    is_valid, missing = validate_report_output(thin_report, "context_pack_report", generic_envelope)
    assert not is_valid, missing
    assert "evidence_table" in missing, missing
    incomplete_pack = "=== README.md (10 chars) ===\nhello\n"
    coverage_ok, missing_coverage, _, _ = evaluate_evidence_coverage(generic_envelope, incomplete_pack)
    assert not coverage_ok, missing_coverage
    confident_but_uncovered = (
        "PASS\n"
        "summary: complete\n"
        "evidence_table: complete\n"
        "touchpoint_check: complete\n"
        "falsification_notes: complete\n"
        "pending_gaps: none\n"
        "confidence: high\n"
        "caveats: none\n"
    )
    is_valid, missing = validate_report_output(
        confident_but_uncovered, "context_pack_report", generic_envelope,
        evidence_coverage_complete=coverage_ok)
    assert not is_valid, missing
    assert "evidence_coverage" in missing, missing

    docs_support = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Docs support editor","goal":"Amend an existing spec without implementing code","task_type":"docs_support","owned_paths":["docs/specs/example.md"],"read_only_paths":["docs/specs/example.md"],"forbidden_actions":["Do not edit source code"],"verification_steps":["Run rtk git diff --check -- docs/specs/example.md"],"deliverable_fields":["changed_sections","verification","caveats"],"completion_rule":"stop after editing the spec and running diff check","escalation_rule":"stop if the spec structure is unclear"}
"""
    docs_envelope = parse_task_envelope(docs_support)
    assert docs_envelope["write_allowed"], docs_envelope
    assert not docs_envelope["exact_content"], docs_envelope
    assert select_mode(docs_envelope) == "bounded_write_patch", docs_envelope
    assert verification_contract_requested(docs_envelope), docs_envelope
    claimed_verified_report = (
        "PASS\n"
        "changed_sections: intro\n"
        "verification: passed\n"
        "caveats: none\n"
        "file: docs/specs/example.md\n"
        "confidence: high\n"
    )
    is_valid, missing = validate_report_output(claimed_verified_report, "bounded_write_patch", docs_envelope)
    assert not is_valid, missing
    assert "verification_observed" in missing, missing
    is_valid, missing = validate_report_output(
        claimed_verified_report, "bounded_write_patch", docs_envelope, verification_observed=True)
    assert is_valid, missing
    assert tool_output_indicates_failure("Process exited with code 1\nAssertionError: nope")
    assert not tool_output_indicates_failure("Process exited with code 0\nAll checks passed")
    patch_probe = os.path.join(root, "tests", "fixtures", "generated_patch_contract.txt")
    try:
        with open(patch_probe, "w", encoding="utf-8") as f:
            f.write("patch contract probe\n")
        changed = collect_owned_path_changes(["tests/fixtures/generated_patch_contract.txt"], root)
        assert "tests/fixtures/generated_patch_contract.txt" in changed, changed
        patch_report = build_patch_contract_report(
            docs_envelope, changed, "PARTIAL",
            "owned path changes were observed, but verification was not observed before the tool budget ended")
        assert patch_report.startswith("PARTIAL\n"), patch_report
        assert "Transport status: PASS" in patch_report, patch_report
        assert "Changed owned paths: tests/fixtures/generated_patch_contract.txt" in patch_report, patch_report
        assert "Verification status: not_observed" in patch_report, patch_report
        verified_patch_report = build_patch_contract_report(
            docs_envelope, changed, "PASS",
            "owned path changes and verification tool output were both observed",
            verification_seen=True,
            verification_output="rtk git diff --check\nProcess exited with code 0")
        assert verified_patch_report.startswith("PASS\n"), verified_patch_report
        assert "Verification status: observed" in verified_patch_report, verified_patch_report
        assert "Verification evidence:" in verified_patch_report, verified_patch_report
    finally:
        try:
            os.remove(patch_probe)
        except FileNotFoundError:
            pass

    exact_write = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Exact write tester","goal":"Create one exact file","task_type":"bounded_write","owned_paths":["tmp/exact.txt"],"read_only_paths":[],"forbidden_actions":["Do not edit other files"],"verification_steps":["Read back the file"],"deliverable_fields":["pass","file","confidence"],"completion_rule":"stop after deterministic write","escalation_rule":"stop if outside owned path is needed","write_allowed":true,"exact_content":"hello exact"}
"""
    exact_envelope = parse_task_envelope(exact_write)
    assert select_mode(exact_envelope) == "bounded_write_exact", exact_envelope

    generated_large = os.path.join(root, "tests", "fixtures", "generated_large_context_pack.txt")
    with open(generated_large, "w", encoding="utf-8") as f:
        f.write(("filler\n" * 1500) + "OSS_HANDOFF_JSON validate-handoff\n")
    large_structured = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Large-file scout","goal":"Find structured evidence in a large file","task_type":"scout","owned_paths":[],"read_only_paths":["tests/fixtures/generated_large_context_pack.txt"],"forbidden_actions":["edit files"],"verification_steps":["Find the OSS_HANDOFF_JSON and validate-handoff instructions"],"deliverable_fields":["confidence","evidence"],"completion_rule":"stop after report","escalation_rule":"stop on critical path"}
"""
    old_pack_max = os.environ.get("CONTEXT_PACK_MAX_CHARS")
    os.environ["CONTEXT_PACK_MAX_CHARS"] = "1000"
    try:
        large_session = build_task_session({"input": []}, large_structured, "resp_large")
        large_pack = build_context_pack(large_session, root)
    finally:
        if old_pack_max is None:
            os.environ.pop("CONTEXT_PACK_MAX_CHARS", None)
        else:
            os.environ["CONTEXT_PACK_MAX_CHARS"] = old_pack_max
        try:
            os.remove(generated_large)
        except FileNotFoundError:
            pass
    assert "rtk grep OSS_HANDOFF_JSON" in large_pack, large_pack[:1000]
    assert "OSS_HANDOFF_JSON validate-handoff" in large_pack, large_pack[:1000]

    invalid_structured = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Broken","goal":"Missing required fields"}
"""
    invalid_envelope = parse_task_envelope(invalid_structured)
    assert invalid_envelope["schema_error"], invalid_envelope
    assert select_mode(invalid_envelope) == "invalid_handoff", invalid_envelope

    malformed_structured = "OSS_HANDOFF_JSON: {not-json"
    malformed_envelope = parse_task_envelope(malformed_structured)
    assert "invalid JSON" in malformed_envelope["schema_error"], malformed_envelope

    valid_path = os.path.join(root, "tests", "fixtures", "valid_handoff.md")
    invalid_path = os.path.join(root, "tests", "fixtures", "invalid_handoff.md")
    with open(valid_path) as f:
        cli_envelope = validate_handoff_text(f.read())
    assert not cli_envelope["schema_error"], cli_envelope
    assert parse_task_envelope(open(valid_path).read()) == cli_envelope

    ok = subprocess.run(
        [sys.executable, "-m", "codex_oss.cli", "validate-handoff", valid_path],
        cwd=root, capture_output=True, text=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert '"valid": true' in ok.stdout, ok.stdout

    bad = subprocess.run(
        [sys.executable, "-m", "codex_oss.cli", "validate-handoff", invalid_path],
        cwd=root, capture_output=True, text=True,
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "missing required fields" in bad.stdout, bad.stdout
    print("PASS: multiline handoff labels and search scout fallback are useful")


if __name__ == "__main__":
    main()
