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
    r"(file|command):([^\s#]+)(?:#(extract|match)[:=](\S+)|#(zero_match))?"
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
            if not isinstance(finding, dict):
                continue
            refs = finding.get("evidence_refs", [])
            for ref in refs:
                _check_ref(ref, ledger, errors)
            _check_semantic_claim_support(finding, ledger, errors)
        _check_report_matches_ledger(report, ledger, errors)

    is_valid = len(errors) == 0
    result_status = status if is_valid else "FAILED"
    return ValidationResult(is_valid, result_status, missing, errors)


def _check_ref(ref: str, ledger: Any, errors: List[str]):
    """Resolve a canonical evidence ref against the ledger. EvidenceRefResolutionPolicyV1."""
    m = EVIDENCE_REF_RE.match(str(ref))
    if not m:
        errors.append(f"evidence ref '{ref}' has invalid canonical format (expected file:<path>#extract:<id> or command:<turn>)")
        return
    ref_type, target, sub_type, sub_id, zero_match = m.groups()
    if zero_match:
        sub_type = "zero_match"

    if ref_type == "file":
        if not hasattr(ledger, 'files_inspected'):
            errors.append(f"evidence ref '{ref}': no files_inspected in ledger")
            return
        entry = ledger.files_inspected.get(target)
        if not entry:
            errors.append(f"evidence ref '{ref}' points to uninspected file: {target}")
            return
        if sub_type == "extract" and sub_id:
            # Check that the extract ID exists
            extracts = getattr(entry, 'extracts', []) or []
            found = any(e.get('id') == f"extract:{sub_id}" for e in extracts)
            if not found:
                errors.append(f"evidence ref '{ref}' extract '{sub_id}' not found in file {target}")

    elif ref_type == "command":
        if not hasattr(ledger, 'commands_run'):
            errors.append(f"evidence ref '{ref}': no commands_run in ledger")
            return
        try:
            idx = int(target)
            cmds = ledger.commands_run
            if idx < 0 or idx >= len(cmds):
                errors.append(f"evidence ref '{ref}' command index {idx} out of range (0-{len(cmds)-1})")
                return
            cmd = cmds[idx]
            if sub_type == "match" and sub_id:
                try:
                    match_n = int(sub_id)
                    if match_n >= getattr(cmd, 'matches_count', 0):
                        errors.append(f"evidence ref '{ref}' match {match_n} exceeds matches_count ({cmd.matches_count})")
                except ValueError:
                    errors.append(f"evidence ref '{ref}' has non-numeric match index: {sub_id}")
            elif sub_type == "zero_match":
                if cmd.exit_code != 1 or getattr(cmd, 'matches_count', -1) != 0:
                    errors.append(f"evidence ref '{ref}' claims zero_match but command has exit_code={cmd.exit_code}, matches={cmd.matches_count}")
        except ValueError:
            errors.append(f"evidence ref '{ref}' has non-numeric command index: {target}")


def _check_report_matches_ledger(report: dict, ledger: Any, errors: List[str]):
    if hasattr(ledger, "files_inspected"):
        for item in report.get("files_inspected", []) or []:
            if isinstance(item, dict):
                path = item.get("path")
                if path and path not in ledger.files_inspected:
                    errors.append(f"files_inspected entry not present in ledger: {path}")
    if hasattr(ledger, "commands_run"):
        ledger_commands = {(c.tool, json.dumps(c.args, sort_keys=True)) for c in ledger.commands_run}
        for item in report.get("commands_run", []) or []:
            if isinstance(item, dict):
                key = (item.get("tool"), json.dumps(item.get("args", {}), sort_keys=True))
                if key not in ledger_commands:
                    errors.append(f"commands_run entry not present in ledger: {item.get('tool')} {item.get('args', {})}")


def _check_semantic_claim_support(finding: dict, ledger: Any, errors: List[str]) -> None:
    """Reject definition claims backed only by symbol mentions.

    This is deliberately narrow: the runtime is allowed to report weak evidence
    about inspected files, but if it claims that an upper-snake symbol is defined
    in a file, the cited file evidence must include definition-shaped text for
    that symbol. This catches false positives where extractor/validator code
    merely mentions a target constant in strings or regexes.
    """
    claim = str(finding.get("claim", "") or "")
    lower = claim.lower()
    if "defined in" not in lower and "contains" not in lower and "dictionary" not in lower:
        return

    symbols = sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{4,}\b", claim)))
    if not symbols:
        return

    refs = [str(ref) for ref in finding.get("evidence_refs", []) or []]
    evidence_by_ref = _evidence_texts(refs, ledger)
    for symbol in symbols:
        if not _claim_requires_definition_support(claim, symbol):
            continue
        supported = False
        for _ref_name, text in evidence_by_ref.items():
            if _symbol_definition_supported(symbol, text):
                supported = True
                break
        if not supported:
            refs_summary = ", ".join(evidence_by_ref.keys()) or "(no evidence text)"
            errors.append(
                f"finding claim says {symbol} is defined/contained, but cited evidence "
                f"does not show a definition assignment for that symbol: {refs_summary}"
            )


def _claim_requires_definition_support(claim: str, symbol: str) -> bool:
    lower = claim.lower()
    return symbol in claim and (
        "defined in" in lower
        or "definition" in lower
        or "dictionary" in lower
        or "contains" in lower
    )


def _evidence_texts(refs: list[str], ledger: Any) -> Dict[str, str]:
    evidence: Dict[str, str] = {}
    for ref in refs:
        m = EVIDENCE_REF_RE.match(ref)
        if not m:
            continue
        ref_type, target, sub_type, sub_id, _zero_match = m.groups()
        if ref_type == "file":
            if not hasattr(ledger, "files_inspected"):
                continue
            entry = ledger.files_inspected.get(target)
            if not entry:
                continue
            chunks = []
            if sub_type == "extract" and sub_id:
                wanted = f"extract:{sub_id}"
                for extract in getattr(entry, "extracts", []) or []:
                    if extract.get("id") == wanted:
                        chunks.append(str(extract.get("text", "") or ""))
            else:
                chunks.extend(str(extract.get("text", "") or "") for extract in getattr(entry, "extracts", []) or [])
            if not chunks:
                chunks.append(str(getattr(entry, "cached_text", "") or ""))
            evidence[target] = "\n".join(chunk for chunk in chunks if chunk)
        elif ref_type == "command":
            if not hasattr(ledger, "commands_run"):
                continue
            try:
                idx = int(target)
            except ValueError:
                continue
            commands = getattr(ledger, "commands_run", []) or []
            if idx < 0 or idx >= len(commands):
                continue
            command = commands[idx]
            evidence[f"command:{idx}"] = str(getattr(command, "cached_text", "") or "")
    return evidence


def _symbol_definition_supported(symbol: str, text: str) -> bool:
    if not text:
        return False
    assignment = rf"\b{re.escape(symbol)}\s*(?::[^=\n]+)?=\s*"
    if re.search(assignment, text):
        return True
    return False


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
    report_source = report.get("report_source")
    explorer_model = report.get("explorer_model")
    finalizer_model = report.get("finalizer_model")
    fallback_model_used = report.get("fallback_model_used")
    findings = report.get("findings", [])
    files = report.get("files_inspected", [])
    commands_data = report.get("commands_run", [])
    uncertainties = report.get("uncertainties", [])
    caveats = report.get("caveats", [])
    escalation = report.get("escalation_recommendation", "")
    missing = report.get("missing_fields", [])
    visible_commentary_path = report.get("visible_commentary_path", "")
    summary_path = report.get("summary_path", "")
    commentary_delivery_path = report.get("commentary_delivery_path", "")
    commentary_delivery_summary = report.get("commentary_delivery_summary") or {}

    parts = [
        "OSS_REPORT_BEGIN",
        f"Status: {status}",
        f"Confidence: {confidence}",
    ]
    provenance = []
    if report_source:
        provenance.append(f"source={report_source}")
    if explorer_model:
        provenance.append(f"explorer={explorer_model}")
    if finalizer_model:
        provenance.append(f"finalizer={finalizer_model}")
    if fallback_model_used:
        provenance.append("fallback_model_used=true")
    if provenance:
        parts.append(f"Provenance: {', '.join(provenance)}")

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

    if visible_commentary_path or summary_path or commentary_delivery_path:
        parts.append("Visible work:")
        if visible_commentary_path:
            parts.append(f"- Trace: {visible_commentary_path}")
        if summary_path:
            parts.append(f"- Summary: {summary_path}")
        if commentary_delivery_path:
            parts.append(f"- Delivery: {commentary_delivery_path}")
        if isinstance(commentary_delivery_summary, dict) and commentary_delivery_summary:
            parts.append(
                "- Delivery status: "
                f"emitted={commentary_delivery_summary.get('sse_emitted', commentary_delivery_summary.get('emitted', 0))}, "
                f"consumer_observed={commentary_delivery_summary.get('consumer_observed', 0)}, "
                f"rendered_before_final={commentary_delivery_summary.get('rendered_before_final', commentary_delivery_summary.get('rendered', 0))}, "
                f"failed={commentary_delivery_summary.get('failed', 0)}"
            )

    if escalation:
        parts.append(f"Escalation:\n{escalation}")

    if missing:
        parts.append(f"Missing fields: {', '.join(missing)}")

    parts.append("OSS_REPORT_END")
    return "\n".join(parts)
