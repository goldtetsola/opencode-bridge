"""ValidatedReportV1 — strict report schema validation with evidence refs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

JSON = Dict[str, Any]

REPORT_FIELDS = [
    "oss_report_version", "mission_id", "status", "confidence",
    "files_inspected", "commands_run", "findings",
    "uncertainties", "caveats", "escalation_recommendation", "missing_fields",
]

VALID_STATUSES = {"COMPLETE", "PARTIAL", "ESCALATE", "FAILED"}
VALID_CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}

INTENT_PATTERN = re.compile(
    r"\b(I am|I'm|I will|I'll|I.m going to|Running|Starting|About to|Next, I|Let me|I need to|I should|First, I|Now I)\b",
    re.IGNORECASE,
)

EVIDENCE_REF_RE = re.compile(
    r"(file|command):([^\s#]+)(?:#(extract|match|zero_match)[:=](\S+))?"
)


@dataclass
class ValidationResult:
    is_valid: bool
    status: str = "FAILED"
    missing_fields: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def is_intent_or_status(text: str) -> bool:
    """Check if text is intent/status rather than a structured report."""
    if len(text.strip()) < 50:
        return True
    if INTENT_PATTERN.search(text):
        return not any(marker in text.lower() for marker in
            ("oss_report_begin", "pass\n", "fail\n", "status:", "confidence:",
             "caveat:", "files inspected:", "commands run:", "commands used:"))
    return False


def parse_report(text: str) -> Optional[dict]:
    """Parse a report from text — tries JSON first, then markdown block."""
    text = text.strip()
    # Try JSON
    try:
        d = json.loads(text)
        if d.get("oss_report_version"):
            return d
    except (json.JSONDecodeError, ValueError):
        pass

    # Try JSON fenced block
    m = re.search(r'```json\s*\n(.*?)\n```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Try <OSS_HANDOFF_JSON> block
    m = re.search(r'<OSS_HANDOFF_JSON>\s*\n?(.*?)\n?</OSS_HANDOFF_JSON>', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def validate_report(report: dict, ledger: Any = None) -> ValidationResult:
    """Validate a report against the spec schema."""
    if not isinstance(report, dict):
        return ValidationResult(False, "FAILED", errors=["report is not a JSON object"])

    version = str(report.get("oss_report_version", ""))
    if not version:
        return ValidationResult(False, "FAILED", errors=["missing oss_report_version"])

    status = str(report.get("status", "")).upper()
    if status not in VALID_STATUSES:
        return ValidationResult(False, "FAILED", errors=[f"invalid status: {status}"])

    confidence = str(report.get("confidence", "")).upper()
    if confidence not in VALID_CONFIDENCES:
        return ValidationResult(False, status, errors=[f"invalid confidence: {confidence}"])

    missing = []
    for field in REPORT_FIELDS:
        if field not in report or report[field] is None:
            missing.append(field)

    errors = []
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        errors.append("findings must be a list")

    # Check evidence refs in findings
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"finding[{i}] is not an object")
            continue
        refs = finding.get("evidence_refs", [])
        if not refs:
            errors.append(f"finding[{i}] has no evidence_refs")
            continue
        for j, ref in enumerate(refs):
            if not EVIDENCE_REF_RE.match(str(ref)):
                errors.append(f"finding[{i}].evidence_refs[{j}] invalid format: {ref}")

    # Confidence gates
    if confidence == "HIGH":
        if missing:
            errors.append("HIGH confidence forbidden when fields are missing")
        if ledger and hasattr(ledger, 'risk_flags') and ledger.risk_flags:
            errors.append("HIGH confidence forbidden with risk flags present")

    file_inspected = report.get("files_inspected", [])
    commands = report.get("commands_run", [])
    if not isinstance(file_inspected, list):
        errors.append("files_inspected must be a list")
    if not isinstance(commands, list):
        errors.append("commands_run must be a list")

    # Evidence ref resolution against ledger
    if ledger:
        for finding in findings:
            refs = finding.get("evidence_refs", [])
            for ref in refs:
                _check_ref(ref, ledger, errors)

    is_valid = len(errors) == 0
    result_status = status if is_valid else "FAILED"
    return ValidationResult(is_valid, result_status, missing, errors)


def _check_ref(ref: str, ledger: Any, errors: List[str]):
    m = EVIDENCE_REF_RE.match(str(ref))
    if not m:
        return
    ref_type, target, sub_type, sub_id = m.groups()
    if ref_type == "file":
        entry = ledger.files_inspected.get(target) if hasattr(ledger, 'files_inspected') else None
        if not entry:
            errors.append(f"evidence ref '{ref}' points to uninspected file: {target}")
    elif ref_type == "command":
        try:
            idx = int(target)
            cmds = ledger.commands_run if hasattr(ledger, 'commands_run') else []
            if idx < 0 or idx >= len(cmds):
                errors.append(f"evidence ref '{ref}' points to nonexistent command: {target}")
        except ValueError:
            errors.append(f"evidence ref '{ref}' has non-numeric command index: {target}")


def validate_text_report(text: str, ledger: Any = None) -> ValidationResult:
    """Validate a text report — rejects intent/status, parses, validates."""
    if is_intent_or_status(text):
        return ValidationResult(False, "FAILED", errors=["intent_or_status_text_not_report"])

    report = parse_report(text)
    if not report:
        return ValidationResult(False, "FAILED", errors=["report_parse_failed"])

    return validate_report(report, ledger)


def render_report(report: dict) -> str:
    """Render a ValidatedReportV1 as markdown."""
    status = report.get("status", "FAILED")
    confidence = report.get("confidence", "LOW")
    findings = report.get("findings", [])
    files = report.get("files_inspected", [])
    commands_data = report.get("commands_run", [])
    uncertainties = report.get("uncertainties", [])
    caveats = report.get("caveats", [])
    escalation = report.get("escalation_recommendation", "")
    missing = report.get("missing_fields", [])

    parts = [
        "OSS_REPORT_BEGIN",
        f"Status: {status}",
        f"Confidence: {confidence}",
    ]

    if findings:
        parts.append("Findings:")
        for f in findings:
            claim = f.get("claim", "?")
            refs = ", ".join(f.get("evidence_refs", []))
            parts.append(f"- {claim} [evidence_refs: {refs}]")

    if files:
        parts.append("Evidence:")
        for e in (files if isinstance(files, list) else [files]):
            if isinstance(e, dict):
                parts.append(f"- {e.get('path', '?')} (complete={e.get('complete', False)})")
            else:
                parts.append(f"- {e}")

    if commands_data:
        parts.append("Commands:")
        for c in (commands_data if isinstance(commands_data, list) else [commands_data]):
            if isinstance(c, dict):
                parts.append(f"- {c.get('tool', '?')} {c.get('args', {})}")
            else:
                parts.append(f"- {c}")

    if uncertainties:
        parts.append("Uncertainties:")
        for u in uncertainties:
            parts.append(f"- {u}")

    if caveats:
        parts.append("Caveats:")
        for c in caveats:
            parts.append(f"- {c}")

    if escalation:
        parts.append(f"Escalation:\n{escalation}")

    if missing:
        parts.append(f"Missing fields: {', '.join(missing)}")

    parts.append("OSS_REPORT_END")
    return "\n".join(parts)
