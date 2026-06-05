"""Native-style visible work notes for MissionV1 Desktop runs.

The runtime keeps authority in JSON artifacts. This module only shapes the
Desktop-visible prose so MissionV1 workers look like bounded native subagents
rather than certification harnesses.
"""

from __future__ import annotations

from typing import Any

from codex_oss.visible_commentary import sanitize_visible_text

JSON = dict[str, Any]


def emit_native_work_contract(commentary: Any, mission: Any) -> None:
    """Emit the initial human-readable work contract for a MissionV1 run."""
    if commentary is None:
        return
    contract = native_work_contract_text(mission)
    commentary.emit(
        "mission_started",
        "Mission accepted",
        contract,
        phase="PLAN",
        source="runtime",
        metadata={
            "ux_shape": "native_work_contract.v1",
            "tier": str(getattr(mission, "tier", "") or ""),
            "mode": str(getattr(mission, "mode", "") or ""),
        },
    )


def native_work_contract_text(mission: Any) -> str:
    role = _role_for_mission(mission)
    task_type = _task_type_for_mission(mission)
    objective = _limit_text(_safe(str(getattr(mission, "objective", "") or "Complete the bounded MissionV1 task.")), 150).rstrip(".")
    owned = _path_list(getattr(mission, "owned_paths", []) or [])
    read_only = _path_list(
        (getattr(mission, "read_only_paths", []) or [])
        or (getattr(mission, "allowed_paths", []) or [])
        or (getattr(mission, "allowed_roots", []) or [])
    )
    constraints = _constraints_for_mission(mission)
    return "\n".join([
        f"ROLE: {role}.",
        f"GOAL: {objective}.",
        f"TASK TYPE: {task_type}.",
        f"OWNED PATHS: {owned}.",
        f"READ-ONLY PATHS: {read_only}.",
        f"DO NOT TOUCH: {constraints}.",
        "COMPLETION RULE: Clear result only.",
    ])


def native_work_note(event: JSON) -> str:
    """Project runtime events into native-style visible work notes."""
    event_type = str(event.get("event_type") or "")
    title = str(event.get("title") or "").strip()
    message = str(event.get("message") or "").strip()
    phase = str(event.get("phase") or "").strip()

    if event_type == "mission_started":
        return message or title
    if event_type == "deterministic_fast_path_used":
        return "The task matched a deterministic runtime path, so I can answer without a tool loop."
    if event_type == "model_action_requested":
        if "final" in title.lower() or "final" in message.lower():
            return "Enough evidence is available for a bounded final report; checking the final narration."
        return "I’m choosing the next safe evidence step."
    if event_type == "tool_action_planned":
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        tool = _safe(str(metadata.get("tool_name") or "tool"))
        args = metadata.get("arguments") if isinstance(metadata.get("arguments"), dict) else {}
        path = _safe(str(args.get("path") or "")) if isinstance(args, dict) else ""
        if path:
            return f"I’m going to run {tool} on {path} next so I can ground the answer in the repo."
        return "I’m selecting the next bounded evidence check."
    if event_type == "tool_action_started":
        return message or "Running the selected read-only tool step now."
    if event_type == "tool_result_summary":
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        path = _safe(str(metadata.get("path") or ""))
        if path:
            return f"The {path} read finished; I’m checking whether it answers the requested deliverable."
        return "The tool result came back; I’m checking whether it answers the requested deliverable."
    if event_type == "coverage_update":
        return "The evidence is now in hand; I’m checking whether anything else is needed before the final."
    if event_type == "model_report_received":
        return message or "Draft report received; validating it against the MissionV1 contract."
    if event_type == "model_action_invalid":
        return message.replace("I'm asking", "Requesting") if message else "Model action was invalid; requesting a bounded repair."
    if event_type == "model_action_failed":
        return "The OSS model timed out while drafting the final narration, so I’m using the verified evidence already collected."
    if event_type == "mission_completed":
        return "I’ve saved the final report and evidence for review."
    if event_type in {"mission_partial", "mission_escalated"}:
        return message or "Mission stopped with unresolved evidence."
    body = f"{title}: {message}" if title and message else (message or title)
    if body:
        return body
    return (phase or event_type or "Progress").replace("_", " ").strip().capitalize()


def native_final_report_text(report: Any) -> str:
    """Render a MissionV1 report as a Desktop-native final answer."""
    if not isinstance(report, dict):
        return str(report or "No report was produced.")

    status = _safe(str(report.get("status") or "PARTIAL")).upper()
    confidence = _safe(str(report.get("confidence") or "LOW")).upper()
    lines: list[str] = [
        "Outcome",
        f"{status}. Confidence: {confidence}.",
    ]

    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    visible_findings = _visible_findings(report, findings)
    if visible_findings:
        lines.extend(["", "Findings"])
        for claim in visible_findings[:6]:
            lines.append(f"- {claim}")
    else:
        lines.extend(["", "Findings", "- No blocking findings were produced from the available evidence."])

    files = report.get("files_inspected") if isinstance(report.get("files_inspected"), list) else []
    commands = report.get("commands_run") if isinstance(report.get("commands_run"), list) else []
    if files or commands:
        lines.extend(["", "Verification"])
        for item in files[:5]:
            if isinstance(item, dict):
                path = _safe(str(item.get("path") or "unknown"))
                complete = "complete" if item.get("complete") else "partial"
                lines.append(f"- Inspected {path} ({complete}).")
        for item in commands[:5]:
            if isinstance(item, dict):
                tool = _safe(str(item.get("tool") or "tool"))
                args = item.get("args") if isinstance(item.get("args"), dict) else {}
                path = _safe(str(args.get("path") or "")) if isinstance(args, dict) else ""
                lines.append(f"- Ran {tool}{f' on {path}' if path else ''}.")

    caveats = _visible_caveats(report.get("caveats") if isinstance(report.get("caveats"), list) else [])
    uncertainties = report.get("uncertainties") if isinstance(report.get("uncertainties"), list) else []
    if caveats or uncertainties:
        lines.extend(["", "Residual Risks"])
        for item in (caveats + uncertainties)[:6]:
            lines.append(f"- {_safe(str(item))}")

    escalation = _safe(str(report.get("escalation_recommendation") or ""))
    if escalation:
        lines.extend(["", "Escalation", escalation])

    return "\n".join(lines).strip()


def _visible_findings(report: JSON, findings: list[Any]) -> list[str]:
    visible: list[str] = []
    for finding in findings:
        if isinstance(finding, dict):
            claim = _safe(str(finding.get("claim") or finding.get("summary") or ""))
            refs = finding.get("evidence_refs") if isinstance(finding.get("evidence_refs"), list) else []
        else:
            claim = _safe(str(finding))
            refs = []
        if not claim or _is_internal_finding(claim):
            continue
        if claim.startswith("Evidence gathered while testing hypothesis:"):
            claim = claim.removeprefix("Evidence gathered while testing hypothesis:").strip()
        if refs:
            claim = f"{claim} Evidence: {', '.join(_safe(str(ref)) for ref in refs[:3])}."
        visible.append(claim)

    if visible:
        return visible

    objective = _safe(str(report.get("objective") or report.get("mission_question") or ""))
    files = report.get("files_inspected") if isinstance(report.get("files_inspected"), list) else []
    inspected = [
        _safe(str(item.get("path") or ""))
        for item in files
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    ]
    refs = _evidence_refs_from_findings(findings)
    if inspected:
        if objective:
            sentence = f"I inspected {', '.join(inspected[:3])} for the requested check"
        else:
            sentence = f"I inspected {', '.join(inspected[:3])}"
        if refs:
            sentence += f". Evidence: {', '.join(refs[:3])}."
        else:
            sentence += "."
        visible.append(sentence)
    if not visible and objective:
        visible.append(f"I completed the requested check: {objective}.")
    return visible


def _evidence_refs_from_findings(findings: list[Any]) -> list[str]:
    refs: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        values = finding.get("evidence_refs")
        if not isinstance(values, list):
            continue
        for ref in values:
            safe = _safe(str(ref))
            if safe and safe not in refs:
                refs.append(safe)
    return refs


def _is_internal_finding(claim: str) -> bool:
    return claim.startswith("Answered obligation:")


def _visible_caveats(caveats: list[Any]) -> list[str]:
    visible: list[str] = []
    for item in caveats:
        text = _safe(str(item))
        if not text:
            continue
        if text == "Report rendered from runtime answer graph.":
            continue
        if text == "The model call timed out, so the runtime closed from the answer graph.":
            text = "The OSS model timed out during final narration, so the final wording was assembled from the verified evidence already collected."
        visible.append(text)
    return visible


def _role_for_mission(mission: Any) -> str:
    tier = str(getattr(mission, "tier", "") or "").upper()
    mode = str(getattr(mission, "mode", "") or "")
    if tier in {"A4", "A5"} or "implementation" in mode or "patch" in mode:
        return "OSS implementation subagent"
    if tier == "A2":
        return "OSS context subagent"
    return "OSS investigation subagent"


def _task_type_for_mission(mission: Any) -> str:
    tier = str(getattr(mission, "tier", "") or "").upper()
    mode = str(getattr(mission, "mode", "") or "")
    if tier == "A4":
        return "patch proposal"
    if tier == "A5":
        return "bounded implementation"
    if tier == "A2":
        return "guided exploration"
    if tier == "A3":
        return "managed investigation"
    return mode.replace("_", " ") or "MissionV1 task"


def _constraints_for_mission(mission: Any) -> str:
    parts = ["do not print secrets"]
    if not bool(getattr(mission, "write_allowed", False)):
        parts.insert(0, "No code edits")
    return "; ".join(parts)


def _verification_steps(mission: Any) -> list[str]:
    policy = getattr(mission, "verification_policy", {}) or {}
    commands = policy.get("allowed_commands") if isinstance(policy, dict) else []
    steps: list[str] = []
    if isinstance(commands, list):
        for command in commands[:3]:
            if isinstance(command, list):
                steps.append(" ".join(str(part) for part in command))
    return steps


def _path_list(values: Any, *, empty: str = "none") -> str:
    if not isinstance(values, list):
        return empty
    cleaned = [_safe(str(value)) for value in values if str(value or "").strip()]
    return ", ".join(cleaned[:4]) if cleaned else empty


def _safe(value: str) -> str:
    safe, _ = sanitize_visible_text(value)
    return " ".join(safe.split())


def _limit_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 3)].rstrip() + "..."
