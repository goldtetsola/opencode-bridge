"""ReadEvidenceV1 — canonical read evidence and model-authored read narration.

Builds structured CanonicalReadEvidenceV1, ReadReportSkeletonV1, and
ReadNarrativeDraftV1 artifacts. Writes finalizer attempt logs. Integrates
with model-authored narration over runtime-owned evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Optional

JSON = dict[str, Any]

# ── Prompt limits for bounded finalizer calls ──────────────────────────────

MAX_TOTAL_FINALIZER_PROMPT_CHARS = int(os.getenv("OSS_READ_FINALIZER_MAX_PROMPT_CHARS", "4000"))
MAX_EXCERPT_CHARS_PER_FILE = int(os.getenv("OSS_READ_FINALIZER_MAX_EXCERPT_CHARS", "350"))
MAX_FINALIZER_FILES = int(os.getenv("OSS_READ_FINALIZER_MAX_FILES", "20"))


# ── CanonicalReadEvidenceV1 ────────────────────────────────────────────────


def build_canonical_read_evidence(
    *,
    mission_id: str,
    required_paths: list[str],
    evidence: dict[str, JSON],
    failures: list[str],
    writes_performed: bool = False,
) -> JSON:
    """Build CanonicalReadEvidenceV1 from completed evidence and failures."""
    fulfilled = []
    failed = []
    for path in sorted(required_paths or []):
        item = evidence.get(path)
        if item:
            excerpt = str(item.get("output_excerpt", "") or "")
            fulfilled.append({
                "path": path,
                "read_status": "success",
                "sha256": str(item.get("output_sha256", "") or ""),
                "bytes": int(item.get("output_chars", 0) or 0),
                "excerpt": excerpt,
                "excerpt_truncated": bool(item.get("redactions_applied", False) or len(excerpt) < int(item.get("output_chars", 0) or 0)),
                "source": str(item.get("source", "unknown")),
            })
        elif any(_path_satisfies_required_path(failure, path) for failure in failures):
            failure_text = next(
                str(f) for f in failures
                if _path_satisfies_required_path(f, path)
            )
            failed.append({
                "path": path,
                "read_status": "failure",
                "error": failure_text[:300],
            })
        else:
            failed.append({
                "path": path,
                "read_status": "missing",
                "error": "declared required path was not read",
            })

    can_complete = not bool(failed)
    return {
        "schema_version": "canonical_read_evidence.v1",
        "mission_id": mission_id,
        "required_paths": sorted(required_paths or []),
        "fulfilled_paths": fulfilled,
        "failed_paths": failed,
        "writes_performed": writes_performed,
        "status_entitlement": {
            "can_complete": can_complete,
            "reason": (
                "all_required_read_only_sources_inspected"
                if can_complete
                else "one_or_more_required_sources_could_not_be_inspected"
            ),
        },
        "runtime_caveats": [
            "The bridge completed the declared read-only evidence floor server-side to avoid client continuation replay; no writes were performed.",
        ] if not writes_performed else [],
    }


# ── ReadReportSkeletonV1 ───────────────────────────────────────────────────


def build_read_report_skeleton(
    *,
    mission_id: str,
    status: str,
    synthesis_status: str,
    evidence: dict[str, JSON],
    required_paths: list[str],
    failures: list[str],
    parent_response_id: str = "none",
    child_response_id: str = "none",
    replay_count: int = 0,
    reason: str = "",
) -> JSON:
    """Build ReadReportSkeletonV1 with runtime-owned and model-fillable fields."""
    completed = set(evidence.keys())
    return {
        "schema_version": "read_report_skeleton.v1",
        "mission_id": mission_id,
        "status": status,
        "synthesis_status": synthesis_status,
        "reason": reason,
        "parent_response_id": parent_response_id,
        "child_response_id": child_response_id,
        "replay_count": replay_count,
        "runtime_owned_fields": {
            "files_inspected": sorted(completed),
            "missing_required_sources": sorted(
                _remaining_required_paths(required_paths or [], completed)
            ),
            "writes_performed": False,
            "evidence_count": len(evidence),
            "failure_count": len(failures),
            "pending_response": child_response_id if child_response_id != "none" else "none",
            "replay_count": replay_count,
        },
        "model_fillable_fields": [
            "findings",
            "confidence_rationale",
            "caveat_wording",
        ],
        "forbidden_model_moves": [
            "Do not claim additional files were inspected.",
            "Do not remove runtime caveats.",
            "Do not claim writes or tests were performed.",
            "Do not quote full file contents.",
            "Do not reveal secrets or hidden reasoning.",
            "Do not override the synthesis status.",
            "Do not claim the bridge is unavailable.",
            "Do not deny file access that the evidence shows was successful.",
        ],
    }


# ── ReadNarrativeDraftV1 ───────────────────────────────────────────────────


def build_read_narrative_draft(
    *,
    findings: list[str],
    confidence_rationale: str = "",
    caveat_wording: list[str] | None = None,
) -> JSON:
    return {
        "schema_version": "read_narrative_draft.v1",
        "findings": findings,
        "confidence_rationale": confidence_rationale,
        "caveat_wording": caveat_wording or [],
    }


# ── ReadNarrativeDraftV1 parsing and validation ────────────────────────────


READ_NARRATIVE_DRAFT_PARSE = re.compile(
    r'(?:^|\n)\s*(?:\{[\s\n]*"schema_version"[\s\n]*:[\s\n]*"read_narrative_draft\.v1")',
    re.MULTILINE,
)


READ_NARRATIVE_FORBIDDEN_AUTHORITY_PATTERNS = re.compile(
    r"^("
    r"complete|partial|failed|fail|pass|"
    r"synthesis status|reason|parent response|pending response|replay count|"
    r"evidence-gathering status|files inspected|missing required sources|"
    r"no writes performed|evidence|canonical evidence|"
    r"status|evidence authority|narrative authority"
    r")\s*:"
    r"|^(complete|partial|failed|fail|pass)\s*$"
    r"|no writes (?:were )?performed"
    r"|sha256\s*="
    r"|\b("
    r"cannot\s+(?:read|open|access|inspect)|"
    r"can't\s+(?:read|open|access|inspect)|"
    r"unable\s+to\s+(?:read|open|access|inspect)|"
    r"no\s+(?:file\s*system|filesystem)\s+access|"
    r"bridge\s+runtime\s+(?:is\s+)?(?:unavailable|not\s+available)|"
    r"runtime\s+(?:is\s+)?(?:unavailable|not\s+available)|"
    r"files\s+inspected\s*:\s*none"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)

READ_NARRATIVE_EVIDENCE_NEGATION = re.compile(
    r"\b("
    r"cannot\s+(?:read|open|access|inspect)|"
    r"can't\s+(?:read|open|access|inspect)|"
    r"unable\s+to\s+(?:read|open|access|inspect)|"
    r"no\s+(?:file\s*system|filesystem)\s+access|"
    r"bridge\s+runtime\s+(?:is\s+)?(?:unavailable|not\s+available)|"
    r"runtime\s+(?:is\s+)?(?:unavailable|not\s+available)|"
    r"files\s+inspected\s*:\s*none|"
    r"none\s+\(unable\s+to\s+open\)"
    r")\b",
    re.IGNORECASE,
)


def parse_read_narrative_draft(text: str) -> tuple[JSON | None, str | None]:
    """Parse ReadNarrativeDraftV1 from model finalizer text."""
    raw = (text or "").strip()
    if not raw:
        return None, "empty narrative response"

    # Try structured JSON first
    match = READ_NARRATIVE_DRAFT_PARSE.search(raw)
    if match:
        start = match.start()
        try:
            parsed = json.loads(raw[start:].split("\n}\n")[0] + "\n}" if "\n}\n" in raw[start:] else _extract_first_json_object(raw[start:]) or raw[start:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("schema_version") == "read_narrative_draft.v1":
            return parsed, None

    # Try any JSON object
    json_obj = _extract_first_json_object(raw)
    if json_obj:
        try:
            parsed = json.loads(json_obj)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("schema_version") == "read_narrative_draft.v1":
            return parsed, None

    # Fallback: unstructured prose
    return _parse_unstructured_narrative(raw)


def _parse_unstructured_narrative(text: str) -> tuple[JSON | None, str | None]:
    """Parse natural-language narrative into ReadNarrativeDraftV1 structure."""
    stripped = text.strip()
    # Reject very short text that is clearly not a narrative
    if len(stripped) < 50:
        return None, "narrative too short for structured extraction"

    # Reject text that looks like an action/intent, not a report
    if _looks_like_action_not_narrative(stripped):
        return None, "text appears to be an action or intent, not a read report narrative"

    findings: list[str] = []
    confidence: str = ""
    caveats: list[str] = []
    current_section: str | None = None
    for line in stripped.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        lower = stripped_line.lower()
        if lower.startswith(("finding", "key finding", "- finding", "* finding")):
            current_section = "findings"
            findings.append(_clean_bullet(stripped_line))
        elif lower.startswith(("confidence", "i am ", "i'm ")):
            current_section = "confidence"
            confidence = stripped_line
        elif lower.startswith(("caveat", "limitation", "note", "- caveat", "* caveat")):
            current_section = "caveats"
            caveats.append(_clean_bullet(stripped_line))
        elif current_section == "findings" and (stripped_line.startswith(("-", "*", "•"))):
            findings.append(_clean_bullet(stripped_line))
        elif current_section == "caveats" and (stripped_line.startswith(("-", "*", "•"))):
            caveats.append(_clean_bullet(stripped_line))

    if not findings:
        if len(stripped) >= 50 and not _looks_like_action_not_narrative(stripped):
            findings = [stripped[:500]]
        else:
            return None, "no narrative content extracted from model output"

    return build_read_narrative_draft(
        findings=findings[:10],
        confidence_rationale=confidence[:300] if confidence else "",
        caveat_wording=caveats[:5] if caveats else [],
    ), None


def _looks_like_action_not_narrative(text: str) -> bool:
    """Heuristic to detect text that is an action/intent rather than a report narrative."""
    lowered = text.strip().lower()
    # Single-line action indicators
    action_indicators = [
        "running ", "executing ", "reading ", "searching ", "looking ",
        "checking ", "verifying ", "compiling ", "building ", "testing ",
        "inspecting ", "opening ", "calling ", "fetching ", "gathering ",
        "i will ", "i'll ", "let me ", "now i ", "i need to ",
    ]
    if any(lowered.startswith(indicator) for indicator in action_indicators):
        return True
    # Short imperative/planning text
    if len(text.strip()) < 100:
        planning_keywords = ["step", "first", "next", "then", "now", "run", "check", "read", "call"]
        words = lowered.split()
        if words and words[0] in planning_keywords:
            return True
    return False


def _clean_bullet(line: str) -> str:
    cleaned = re.sub(r'^[-*•]\s*', '', line).strip()
    cleaned = re.sub(r'^(?:finding|caveat|limitation|note)\s*:?\s*', '', cleaned, flags=re.IGNORECASE).strip()
    return cleaned[:500]


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def validate_read_narrative_draft(draft: JSON, evidence: dict[str, JSON]) -> tuple[bool, list[str]]:
    """Validate a ReadNarrativeDraftV1 against canonical evidence."""
    reasons: list[str] = []
    if not isinstance(draft, dict) or draft.get("schema_version") != "read_narrative_draft.v1":
        reasons.append("not a valid ReadNarrativeDraftV1")
        return False, reasons

    findings = draft.get("findings", []) or []
    if not findings:
        reasons.append("no findings provided")

    # Check for forbidden authority claims
    text = json.dumps(draft)
    if READ_NARRATIVE_FORBIDDEN_AUTHORITY_PATTERNS.search(text):
        reasons.append("narrative contains forbidden authority claims")

    # Check for evidence negation
    if evidence and READ_NARRATIVE_EVIDENCE_NEGATION.search(text):
        reasons.append("narrative negates available evidence")

    # Check narrative doesn't claim writes
    if re.search(r'\b(wrote|written|modified|changed|edited|patched|applied|created file|deleted)\b', text, re.IGNORECASE):
        reasons.append("narrative claims writes or modifications")

    return not reasons, reasons


# ── Finalizer attempt logging ──────────────────────────────────────────────


def log_finalizer_attempt(
    *,
    mission_dir: str,
    attempt_id: str = "",
    model_alias: str = "",
    prompt_chars: int = 0,
    files_count: int = 0,
    excerpt_chars_total: int = 0,
    timeout_seconds: float = 0,
    elapsed_seconds: float = 0,
    result: str = "unknown",
    validation_errors: list[str] | None = None,
) -> None:
    """Write a finalizer attempt record to server_side_read_finalizer_attempts.jsonl."""
    attempts_path = os.path.join(mission_dir, "server_side_read_finalizer_attempts.jsonl")
    os.makedirs(mission_dir, exist_ok=True)
    record = {
        "attempt_id": attempt_id or f"read_finalizer_{int(time.time() * 1000)}",
        "model_alias": model_alias,
        "prompt_chars": prompt_chars,
        "files_count": files_count,
        "excerpt_chars_total": excerpt_chars_total,
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "result": result,
        "validation_errors": validation_errors or [],
        "timestamp": int(time.time()),
    }
    with open(attempts_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


# ── Bounded finalizer prompt builder ───────────────────────────────────────


def build_bounded_finalizer_prompt(
    *,
    handoff_text: str,
    required_paths: list[str],
    evidence: dict[str, JSON],
    failures: list[str],
) -> tuple[str, JSON]:
    """Build a bounded no-tools prompt for model-authored read narration.

    Returns (prompt_text, prompt_stats) where stats includes char counts
    for observability.
    """
    requested = ", ".join(sorted(required_paths or [])) or "none"

    # Build evidence lines respecting excerpt bounds
    evidence_lines: list[str] = []
    total_excerpt_chars = 0
    files_included = 0

    for path in sorted(evidence.keys())[:MAX_FINALIZER_FILES]:
        item = evidence[path]
        excerpt = str(item.get("output_excerpt", "") or "")
        # Truncate excerpt to per-file limit
        if len(excerpt) > MAX_EXCERPT_CHARS_PER_FILE:
            excerpt = excerpt[:MAX_EXCERPT_CHARS_PER_FILE].rsplit("\n", 1)[0].rstrip() + "\n[truncated]"
        total_excerpt_chars += len(excerpt)
        files_included += 1

        evidence_lines.append(
            f"PATH: {path}\n"
            f"SOURCE: {item.get('source')}\n"
            f"EXIT_CODE: {item.get('exit_code')}\n"
            f"CHARS: {item.get('output_chars')}\n"
            f"SHA256: {item.get('output_sha256')}\n"
            f"EXCERPT:\n{excerpt}"
        )

    failure_text = "\n".join(f"- {f}" for f in failures[:10]) if failures else "none"
    handoff_snippet = (handoff_text or "")[:1000]

    header = (
        "You are writing only the narrative section for a read-only OSS subagent task.\n"
        "The bridge/runtime already gathered the evidence below. Do not call tools. "
        "Do not claim files were modified. Do not mention hidden reasoning or private scratchpads.\n\n"
        "The bridge/runtime owns status, files inspected, missing sources, hashes, and write safety. "
        "Do not include those as authority fields. Write natural, user-facing prose only. Include:\n"
        "- concise findings based only on the evidence excerpts\n"
        "- confidence\n"
        "- caveats\n\n"
        f"Original handoff:\n{handoff_snippet}\n\n"
        f"Required paths: {requested}\n"
        f"Failures: {failure_text}\n\n"
        "Canonical evidence:\n"
    )
    evidence_text = "\n".join(evidence_lines)
    remaining_budget = MAX_TOTAL_FINALIZER_PROMPT_CHARS - len(header) - 100
    if len(evidence_text) > remaining_budget:
        evidence_text = evidence_text[:remaining_budget].rsplit("\n", 1)[0] + "\n[evidence truncated]"
    prompt = header + evidence_text

    # Ensure we don't exceed total limit
    if len(prompt) > MAX_TOTAL_FINALIZER_PROMPT_CHARS:
        prompt = prompt[:MAX_TOTAL_FINALIZER_PROMPT_CHARS - 100] + "\n[prompt truncated]"

    stats = {
        "prompt_chars": len(prompt),
        "files_included": files_included,
        "excerpt_chars_total": total_excerpt_chars,
        "handoff_chars": len(handoff_snippet),
        "exceeded_max": len(prompt) >= MAX_TOTAL_FINALIZER_PROMPT_CHARS,
    }

    return prompt, stats


# ── Model-authored read report builder ─────────────────────────────────────


def build_model_authored_read_report(
    *,
    parent_response_id: str,
    child_response_id: str,
    replay_count: int,
    required_paths: list[str],
    evidence: dict[str, JSON],
    failures: list[str],
    reason: str,
    narrative_draft: JSON,
) -> str:
    """Build the final MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION report."""
    completed = set(evidence.keys())
    missing = _remaining_required_paths(required_paths, completed)
    status = "COMPLETE" if not missing and not failures else "PARTIAL"

    findings = narrative_draft.get("findings", []) or []
    confidence_text = str(narrative_draft.get("confidence_rationale", "") or "")
    caveats = list(narrative_draft.get("caveat_wording", []) or [])

    lines = [
        status,
        "Synthesis status: MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION",
        f"Reason: {reason}",
        "Evidence authority: bridge_runtime",
        "Narrative authority: model_finalizer",
        f"Parent response: {parent_response_id}",
        f"Pending response: {child_response_id}",
        f"Replay count: {replay_count}",
        f"Evidence-gathering status: {'PASS' if status == 'COMPLETE' else 'PARTIAL'}",
        f"Files inspected: {', '.join(sorted(completed)) if completed else 'none'}",
        f"Missing required sources: {', '.join(missing) if missing else 'none'}",
        "No writes performed: true",
        "Evidence:",
    ]
    for path in sorted(evidence):
        item = evidence[path]
        lines.append(
            f"- {path}: source={item.get('source')}, chars={item.get('output_chars')}, "
            f"sha256={item.get('output_sha256')}"
        )
    if failures:
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in failures)

    # Build model-authored narrative section as prose
    narrative_parts: list[str] = []
    if findings:
        narrative_parts.append("Findings:")
        for finding in findings:
            narrative_parts.append(f"- {finding}")
    if confidence_text:
        narrative_parts.append(f"Confidence: {confidence_text}")
    if caveats:
        narrative_parts.append("Caveats:")
        for caveat in caveats:
            narrative_parts.append(f"- {caveat}")
    narrative_text = "\n".join(narrative_parts)

    lines.extend([
        "",
        "Model-authored narrative:",
        narrative_text.strip(),
    ])
    return "\n".join(lines)


# ── Artifact persistence ───────────────────────────────────────────────────


def persist_read_artifacts(
    *,
    mission_dir: str,
    mission_id: str,
    required_paths: list[str],
    evidence: dict[str, JSON],
    failures: list[str],
    status: str,
    synthesis_status: str,
    parent_response_id: str = "none",
    child_response_id: str = "none",
    replay_count: int = 0,
    reason: str = "",
    narrative_draft: JSON | None = None,
) -> JSON:
    """Write CanonicalReadEvidenceV1, ReadReportSkeletonV1, and related artifacts."""
    os.makedirs(mission_dir, exist_ok=True)

    # Canonical Read Evidence
    canonical = build_canonical_read_evidence(
        mission_id=mission_id,
        required_paths=required_paths,
        evidence=evidence,
        failures=failures,
        writes_performed=False,
    )
    _write_json(os.path.join(mission_dir, "canonical_read_evidence.json"), canonical)

    # Read Report Skeleton
    skeleton = build_read_report_skeleton(
        mission_id=mission_id,
        status=status,
        synthesis_status=synthesis_status,
        evidence=evidence,
        required_paths=required_paths,
        failures=failures,
        parent_response_id=parent_response_id,
        child_response_id=child_response_id,
        replay_count=replay_count,
        reason=reason,
    )
    _write_json(os.path.join(mission_dir, "read_report_skeleton.json"), skeleton)

    # Narrative Draft (if available)
    if narrative_draft:
        _write_json(os.path.join(mission_dir, "read_narrative_draft.json"), narrative_draft)
        # Also write merged report
        merged = {
            "schema_version": "merged_read_report.v1",
            "mission_id": mission_id,
            "status": status,
            "synthesis_status": synthesis_status,
            "canonical_evidence_hash": _hash_json(canonical),
            "report_skeleton_hash": _hash_json(skeleton),
            "narrative_draft_hash": _hash_json(narrative_draft),
            "runtime_caveats": canonical.get("runtime_caveats", []),
        }
        _write_json(os.path.join(mission_dir, "merged_read_report.json"), merged)

    return {
        "canonical_read_evidence": canonical,
        "read_report_skeleton": skeleton,
        "read_narrative_draft": narrative_draft,
    }


# ── Helpers ────────────────────────────────────────────────────────────────


def _remaining_required_paths(required_paths: list[str], completed: set[str]) -> list[str]:
    remaining = []
    for required in (required_paths or []):
        if not any(_path_satisfies_required_path(completed_path, required) for completed_path in completed):
            remaining.append(required)
    return remaining


def _path_satisfies_required_path(completed: str, required: str) -> bool:
    """Check if a completed path satisfies a required path entry."""
    if completed == required:
        return True
    norm_completed = completed.replace("\\", "/").rstrip("/")
    norm_required = required.replace("\\", "/").rstrip("/")
    if norm_completed == norm_required:
        return True
    if norm_required.endswith("/*") or norm_required.endswith("/**"):
        prefix = norm_required.rstrip("/*").rstrip("/")
        return norm_completed.startswith(prefix + "/") or norm_completed == prefix
    base_completed = os.path.basename(norm_completed)
    base_required = os.path.basename(norm_required)
    return base_completed == base_required


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)


def _hash_json(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── RuntimeContractCompleterV1 ─────────────────────────────────────────────


def execute_runtime_contract_action(
    *,
    action: JSON,
    project_root: str,
) -> tuple[int, str]:
    """Execute a single RequiredActionV1 deterministically.

    Returns (exit_code, output_text).
    """
    kind = str(action.get("kind", "") or action.get("action_type", ""))
    tool_name = str(action.get("tool_name", "") or "")
    args = action.get("arguments", {}) if isinstance(action.get("arguments"), dict) else {}

    if kind in ("required_read", "required_file_read") or tool_name in ("rtk_read", "read"):
        return _execute_required_read(args, project_root)
    if kind in ("required_grep", "required_search") or tool_name in ("rtk_grep", "grep"):
        return _execute_required_grep(args, project_root)
    if kind in ("required_ls", "required_list") or tool_name in ("rtk_ls", "ls"):
        return _execute_required_ls(args, project_root)
    if kind in ("required_grep_zero_match", "required_search_zero_match"):
        return _execute_required_grep(args, project_root)
    if kind in ("required_git_status") or tool_name in ("rtk_git_status", "git_status"):
        return _execute_required_git_status(args, project_root)

    return 1, f"unsupported required action kind: {kind or tool_name}"


def _execute_required_read(args: JSON, project_root: str) -> tuple[int, str]:
    path = str(args.get("path", "") or args.get("file", "") or "")
    if not path:
        return 1, "read requires a path argument"
    target = _resolve_safe_path(path, project_root)
    if not target:
        return 1, f"path not found or unsafe: {path}"
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
        max_bytes = int(os.getenv("OSS_SERVER_SIDE_READ_MAX_BYTES", "1048576"))
        if len(content) > max_bytes:
            content = content[:max_bytes] + f"\n[server-side read truncated at {max_bytes} bytes]"
        return 0, content
    except Exception as exc:
        return 1, str(exc)


def _execute_required_grep(args: JSON, project_root: str) -> tuple[int, str]:
    pattern = str(args.get("pattern", "") or "")
    path = str(args.get("path", "") or args.get("file", "") or ".")
    if not pattern:
        return 1, "grep requires a pattern argument"

    target = _resolve_safe_path(path, project_root)
    if not target:
        return 1, f"path not found or unsafe: {path}"

    import subprocess
    try:
        proc = subprocess.run(
            ["grep", "-n", "-I", "--", pattern, target] if os.path.isfile(target)
            else ["grep", "-r", "-n", "-I", "--", pattern, target],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 1:
            # grep returns 1 for no matches - this is valid
            return 0, f"[rtk_grep RESULT]\npath: {path}\npattern: {pattern}\nexit_code: 1\nmatches: 0\nstdout_bytes: 0"
        if proc.returncode != 0:
            return proc.returncode, proc.stderr or f"grep failed with exit code {proc.returncode}"
        return 0, proc.stdout
    except FileNotFoundError:
        return 1, "grep command not found"
    except subprocess.TimeoutExpired:
        return 1, "grep timed out after 30s"


def _execute_required_ls(args: JSON, project_root: str) -> tuple[int, str]:
    path = str(args.get("path", "") or args.get("dir", "") or ".")
    target = _resolve_safe_path(path, project_root)
    if not target:
        return 1, f"path not found or unsafe: {path}"

    import subprocess
    try:
        proc = subprocess.run(
            ["ls", "-la", target],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode, proc.stdout or proc.stderr
    except FileNotFoundError:
        return 1, "ls command not found"
    except subprocess.TimeoutExpired:
        return 1, "ls timed out after 10s"


def _execute_required_git_status(args: JSON, project_root: str) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_root,
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode, proc.stdout or proc.stderr
    except FileNotFoundError:
        return 1, "git command not found"
    except subprocess.TimeoutExpired:
        return 1, "git status timed out after 10s"


def _resolve_safe_path(path: str, project_root: str) -> str | None:
    """Resolve a path within project_root, rejecting escapes."""
    if not path:
        return None
    root = os.path.realpath(project_root)
    if os.path.isabs(path):
        full = os.path.realpath(path)
    else:
        full = os.path.realpath(os.path.join(root, path))
    if not (full == root or full.startswith(root + os.sep)):
        return None
    if ".." in path.replace("\\", "/").split("/"):
        return None
    if os.path.isdir(full):
        return full  # Directories are valid for ls/grep
    if os.path.isfile(full):
        return full
    return None


class RuntimeContractCompleter:
    """Completes required evidence actions deterministically when model has stopped.

    Supports reads, greps, ls, and git status. No writes allowed.
    """

    ALLOWED_TOOLS = {"rtk_read", "rtk_grep", "rtk_ls", "rtk_git_status"}

    def __init__(self, project_root: str):
        self.project_root = project_root

    def complete_actions(
        self,
        *,
        required_actions: list[JSON],
        existing_evidence: dict[str, JSON] | None = None,
    ) -> dict[str, JSON]:
        """Execute all required actions and return evidence results."""
        evidence = dict(existing_evidence or {})
        for action in required_actions:
            if not isinstance(action, dict):
                continue
            tool_name = str(action.get("tool_name", "") or "")
            if tool_name not in self.ALLOWED_TOOLS:
                continue
            exit_code, output = execute_runtime_contract_action(
                action=action,
                project_root=self.project_root,
            )
            key = self._evidence_key(action)
            excerpt, redacted = _safe_evidence_excerpt(output)
            evidence[key] = {
                "path": key,
                "source": "bridge_server_side_action",
                "exit_code": exit_code,
                "output_chars": len(output),
                "output_sha256": _hash_text(output),
                "output_excerpt": excerpt,
                "redactions_applied": redacted,
                "action_kind": str(action.get("kind", "") or "required_action"),
            }
        return evidence

    def complete_required_evidence(
        self,
        *,
        required_actions: list[JSON],
        required_paths: list[str],
        existing_evidence: dict[str, JSON] | None = None,
    ) -> tuple[dict[str, JSON], list[str]]:
        """Complete required actions + any remaining required paths. Returns (evidence, failures)."""
        evidence = dict(existing_evidence or {})
        failures: list[str] = []

        # Execute explicit required actions
        for action in required_actions:
            if not isinstance(action, dict):
                continue
            tool_name = str(action.get("tool_name", "") or "")
            if tool_name not in self.ALLOWED_TOOLS:
                continue
            exit_code, output = execute_runtime_contract_action(
                action=action,
                project_root=self.project_root,
            )
            key = self._evidence_key(action)
            excerpt, redacted = _safe_evidence_excerpt(output)
            if exit_code != 0 and self._action_allows_zero_match(action):
                # grep zero-match is valid evidence
                evidence[key] = {
                    "path": key,
                    "source": "bridge_server_side_action",
                    "exit_code": exit_code,
                    "output_chars": len(output),
                    "output_sha256": _hash_text(output),
                    "output_excerpt": excerpt,
                    "redactions_applied": redacted,
                    "action_kind": "required_grep_zero_match",
                    "zero_match": True,
                }
            elif exit_code == 0:
                evidence[key] = {
                    "path": key,
                    "source": "bridge_server_side_action",
                    "exit_code": exit_code,
                    "output_chars": len(output),
                    "output_sha256": _hash_text(output),
                    "output_excerpt": excerpt,
                    "redactions_applied": redacted,
                    "action_kind": str(action.get("kind", "") or "required_action"),
                }
            else:
                failures.append(f"{key}: exit_code={exit_code} output={str(output)[:200]}")

        # Complete any remaining required reads
        for path in required_paths:
            if any(_path_satisfies_required_path(completed, path) for completed in evidence):
                continue
            exit_code, output = _execute_required_read(
                {"path": path, "file": path}, self.project_root
            )
            if exit_code == 0:
                excerpt, redacted = _safe_evidence_excerpt(output)
                evidence[path] = {
                    "path": path,
                    "source": "bridge_server_side_read",
                    "exit_code": exit_code,
                    "output_chars": len(output),
                    "output_sha256": _hash_text(output),
                    "output_excerpt": excerpt,
                    "redactions_applied": redacted,
                }
            else:
                failures.append(f"{path}: exit_code={exit_code} output={str(output)[:200]}")

        return evidence, failures

    @staticmethod
    def _evidence_key(action: JSON) -> str:
        tool_name = str(action.get("tool_name", "") or "")
        args = action.get("arguments", {}) if isinstance(action.get("arguments"), dict) else {}
        path = str(args.get("path", "") or args.get("file", "") or "")
        pattern = str(args.get("pattern", "") or "")
        if tool_name in ("rtk_grep", "grep") and path and pattern:
            return f"grep:{path}:{pattern[:40]}"
        if tool_name in ("rtk_ls", "ls") and path:
            return f"ls:{path}"
        if path:
            return path
        return f"action:{tool_name}"

    @staticmethod
    def _action_allows_zero_match(action: JSON) -> bool:
        kind = str(action.get("kind", "") or "")
        return "zero_match" in kind or "grep_zero_match" in kind


SECRET_PATTERNS: list = [
    (r'(?:OPENCODE_GO|OPENCODE|OPENAI|ANTHROPIC|PROXY)_(?:API_)?KEY\s*=\s*(sk-[A-Za-z0-9\-_]+)', '[redacted key]'),
    (r'Bearer\s+(sk-[A-Za-z0-9\-_]+)', 'Bearer [redacted]'),
    (r'sk-[A-Za-z0-9\-_]{12,}', '[redacted key]'),
]


def _safe_evidence_excerpt(content: str, max_chars: int = 3000) -> tuple[str, bool]:
    """Extract a safe excerpt from evidence content."""
    text = str(content or "")
    found_secret = False
    for pattern, replacement, *flags in SECRET_PATTERNS:
        fl = flags[0] if flags else 0
        if re.search(pattern, text, fl):
            text = re.sub(pattern, replacement, text, flags=fl)
            found_secret = True
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n[truncated]"
    return text, found_secret


# ── CanonicalPatchEvidenceV1 ───────────────────────────────────────────────


def build_canonical_patch_evidence(
    *,
    mission_id: str,
    owned_paths: list[str],
    changed_paths: list[str],
    write_status: str = "applied",
    readback_status: str = "verified",
    before_hashes: dict[str, str] | None = None,
    after_hashes: dict[str, str] | None = None,
    writes_outside_owned_paths: bool = False,
    verification_status: str = "passed",
    verification_method: str = "readback_exact_match",
    rollback_available: bool = True,
) -> JSON:
    """Build CanonicalPatchEvidenceV1 for implementation missions."""
    before = before_hashes or {}
    after = after_hashes or {}
    return {
        "schema_version": "canonical_patch_evidence.v1",
        "mission_id": mission_id,
        "owned_paths": list(owned_paths or []),
        "changed_paths": list(changed_paths or []),
        "write_status": write_status,
        "readback_status": readback_status,
        "before_hashes": before,
        "after_hashes": after,
        "writes_outside_owned_paths": writes_outside_owned_paths,
        "verification": {
            "status": verification_status,
            "method": verification_method,
        },
        "rollback_available": rollback_available,
    }


# ── ImplementationNarrativeDraftV1 ─────────────────────────────────────────


def build_implementation_narrative_draft(
    *,
    change_summary: str = "",
    verification_summary: str = "",
    risk_summary: str = "",
    caveats: list[str] | None = None,
) -> JSON:
    return {
        "schema_version": "implementation_narrative_draft.v1",
        "change_summary": change_summary,
        "verification_summary": verification_summary,
        "risk_summary": risk_summary,
        "caveats": caveats or [],
    }


def validate_implementation_narrative_draft(
    draft: JSON,
    changed_paths: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Validate ImplementationNarrativeDraftV1 against runtime-owned facts."""
    reasons: list[str] = []
    if not isinstance(draft, dict) or draft.get("schema_version") != "implementation_narrative_draft.v1":
        reasons.append("not a valid ImplementationNarrativeDraftV1")
        return False, reasons

    text = json.dumps(draft)

    # Model must not claim runtime authority
    forbidden = [
        r'\b(applied|modified|changed|verified|validated)\s+(?:the\s+)?(?:file|patch|code)\b',
        r'\bmain\s+workspace\s+(?:was\s+)?mutated\b',
        r'\bverification\s+(?:passed|failed)\b',
        r'\bstatus\s*:\s*(?:applied|verified|validated)\b',
        r'\bwrites?\s+(?:were\s+)?(?:applied|performed|executed)\b',
    ]
    for pattern in forbidden:
        if re.search(pattern, text, re.IGNORECASE):
            reasons.append(f"forbidden authority claim: {pattern}")
            break

    # Must have at least change_summary
    if not str(draft.get("change_summary", "") or "").strip():
        reasons.append("missing change_summary")

    return not reasons, reasons
