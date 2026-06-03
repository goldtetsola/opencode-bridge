"""Runtime-owned task envelopes and final-report contracts for OSS agents.

The bridge must not treat raw OSS text as authority. Every delegated request
is normalized into a TaskEnvelopeV1, then model output is classified against
that envelope before it can become a terminal report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable


EXECUTION_MODES = (
    "no_tool_exact",
    "context_pack_report",
    "managed_autonomy",
    "bounded_write_exact",
    "bounded_write_patch",
    "escalate",
    "invalid_handoff",
)

REPORT_CLASSIFICATIONS = (
    "VALID_FINAL_REPORT",
    "STATUS_OR_INTENT",
    "ACTION_REQUEST",
    "POLICY_REFUSAL",
    "INVALID",
    "STALL",
)

INTENT_PATTERNS = (
    r"\b(I am|I'm|I will|I'll|I.m going to|I am going to|Running|Starting|"
    r"About to|Next, I|Let me|I need to|I should|First, I|Now I)\b"
)

REPORT_MARKERS = (
    "oss_report_begin",
    "pass\n",
    "fail\n",
    "partial\n",
    "status:",
    "task status:",
    "summary:",
    "evidence:",
    "evidence snippets:",
    "evidence table:",
    "confidence:",
    "caveat:",
    "caveats:",
    "files inspected:",
    "files gathered:",
    "commands run:",
    "commands used:",
    "command used:",
)

MIN_REPORT_LENGTH = 80


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def field_label(field: str) -> str:
    label = str(field or "").strip().strip("- ").strip()
    if not label:
        return "deliverable"
    label = re.sub(r"\s+", "_", label.lower())
    label = re.sub(r"[^a-z0-9_/-]", "", label)
    return label.strip("_") or "deliverable"


def is_exact_deliverable_field(field: str) -> bool:
    text = str(field or "").strip()
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_/-]{0,80}", text))


@dataclass
class TaskEnvelopeV1:
    mission_id: str = ""
    agent: str = ""
    task_class: str = "managed_autonomy"
    risk_tier: str = "medium"
    write_allowed: bool = False
    owned_paths: list[str] = field(default_factory=list)
    read_only_paths: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    required_commands: list[str] = field(default_factory=list)
    verification_steps: list[str] = field(default_factory=list)
    deliverable_fields: list[str] = field(default_factory=list)
    completion_policy: str = "validated_report_required"
    exact_content: str = ""
    no_tools_required: bool = False
    proof_critical: bool = False
    schema_error: str = ""
    source: str = "runtime_compiler"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "task_envelope.v1",
            "mission_id": self.mission_id,
            "agent": self.agent,
            "task_class": self.task_class,
            "risk_tier": self.risk_tier,
            "write_allowed": self.write_allowed,
            "owned_paths": list(self.owned_paths),
            "read_only_paths": list(self.read_only_paths),
            "forbidden_actions": list(self.forbidden_actions),
            "required_commands": list(self.required_commands),
            "verification_steps": list(self.verification_steps),
            "deliverable_fields": list(self.deliverable_fields),
            "completion_policy": self.completion_policy,
            "exact_content": self.exact_content,
            "no_tools_required": self.no_tools_required,
            "proof_critical": self.proof_critical,
            "schema_error": self.schema_error,
            "source": self.source,
        }


def compile_task_envelope_v1(raw: dict[str, Any] | None, *, handoff_text: str = "") -> dict[str, Any]:
    """Normalize any parsed handoff dict into TaskEnvelopeV1 shape."""
    data = dict(raw or {})
    text = str(handoff_text or "")
    task_type = data.get("task_type") or data.get("task_class") or ""
    if not task_type:
        task_type = "managed_autonomy"
    lower_text = text.lower()
    proof_critical = _bool(data.get("proof_critical")) or any(
        term in lower_text
        for term in ("proof-critical", "proof critical", "recovery path", "schema authority", "deployment")
    )
    write_allowed = _bool(data.get("write_allowed"))
    read_only_paths = [str(p).strip() for p in _as_list(data.get("read_only_paths")) if str(p).strip()]
    owned_paths = [str(p).strip() for p in _as_list(data.get("owned_paths")) if str(p).strip()]
    required_commands = [str(c).strip() for c in _as_list(data.get("required_commands")) if str(c).strip()]
    verification_steps = [str(c).strip() for c in _as_list(data.get("verification_steps")) if str(c).strip()]
    deliverable_fields = [str(f).strip() for f in _as_list(data.get("deliverable_fields")) if str(f).strip()]
    forbidden_actions = [str(f).strip() for f in _as_list(data.get("forbidden_actions")) if str(f).strip()]
    if not forbidden_actions and not write_allowed:
        forbidden_actions = ["edit", "create", "delete", "stage", "commit"]

    risk_tier = str(data.get("risk_tier") or ("high" if proof_critical else "low" if read_only_paths and not write_allowed else "medium"))
    envelope = TaskEnvelopeV1(
        mission_id=str(data.get("mission_id") or ""),
        agent=str(data.get("agent") or ""),
        task_class=str(task_type),
        risk_tier=risk_tier,
        write_allowed=write_allowed,
        owned_paths=owned_paths,
        read_only_paths=read_only_paths,
        forbidden_actions=forbidden_actions,
        required_commands=required_commands,
        verification_steps=verification_steps,
        deliverable_fields=deliverable_fields,
        completion_policy=str(data.get("completion_policy") or "validated_report_required"),
        exact_content=str(data.get("exact_content") or ""),
        no_tools_required=_bool(data.get("no_tools_required")),
        proof_critical=proof_critical,
        schema_error=str(data.get("schema_error") or ""),
        source=str(data.get("source") or "runtime_compiler"),
    ).to_dict()
    for key, value in data.items():
        envelope.setdefault(key, value)
    return envelope


def select_execution_mode_v1(envelope: dict[str, Any]) -> str:
    data = compile_task_envelope_v1(envelope)
    if data.get("schema_error"):
        return "invalid_handoff"
    if data.get("proof_critical"):
        return "escalate"
    if data.get("write_allowed") and data.get("owned_paths") and data.get("exact_content"):
        return "bounded_write_exact"
    if data.get("write_allowed") and data.get("owned_paths"):
        return "bounded_write_patch"
    if data.get("write_allowed"):
        return "invalid_handoff"
    if data.get("read_only_paths"):
        return "context_pack_report"
    if data.get("no_tools_required"):
        return "no_tool_exact"
    return "managed_autonomy"


def is_intent_or_status(text: str, *, min_report_length: int = MIN_REPORT_LENGTH) -> bool:
    content = str(text or "")
    if len(content.strip()) < min_report_length:
        return True
    lower = content.lower()
    if re.search(INTENT_PATTERNS, content, re.IGNORECASE):
        if not any(marker in lower for marker in REPORT_MARKERS):
            return True
    first_line = content.strip().splitlines()[0].strip().lower() if content.strip() else ""
    if first_line.startswith(("i will ", "i'm ", "i am ", "running ", "starting ", "about to ")):
        return True
    return False


def _output_has_status(text: str) -> bool:
    return bool(re.search(r"\b(pass|fail|partial|complete|escalate|blocked)\b", text.lower()))


def _output_claims_verification(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "verification passed",
            "verification: pass",
            "tests passed",
            "check passed",
            "lint passed",
            "verified",
        )
    )


def _verification_contract_requested(envelope: dict[str, Any]) -> bool:
    fields = [field_label(field) for field in envelope.get("deliverable_fields", [])]
    if any("verification" in field or field in ("tests", "check", "checks") for field in fields):
        return True
    for step in envelope.get("verification_steps", []):
        lowered = str(step).lower()
        if any(word in lowered for word in ("run ", "test", "diff --check", "typecheck", "lint", "doctor", "verify")):
            return True
    return False


def classify_report_output(
    text: str,
    mode: str,
    envelope: dict[str, Any] | None,
    *,
    verification_observed: bool = False,
    evidence_coverage_complete: bool = True,
) -> tuple[str, list[str]]:
    content = str(text or "")
    data = compile_task_envelope_v1(envelope or {})
    missing: list[str] = []
    lowered = content.lower()

    if not content.strip():
        return "STALL", ["empty_report"]
    if is_intent_or_status(content):
        return "STATUS_OR_INTENT", ["intent_or_status_detected"]
    if re.search(r"\b(action|tool|function_call)\s*:", lowered) and not _output_has_status(content):
        return "ACTION_REQUEST", ["action_request_not_terminal"]
    if "policy_refusal" in lowered or "cannot comply" in lowered or "forbidden action" in lowered:
        return "POLICY_REFUSAL", []

    required_by_mode = {
        "context_pack_report": ["confidence", "caveat"],
        "managed_autonomy": ["confidence", "caveat"],
        "bounded_write_exact": ["file", "confidence"],
        "bounded_write_patch": ["file", "confidence", "caveat"],
        "no_tool_exact": [],
        "escalate": [],
    }
    if mode in ("context_pack", "context_pack_report", "managed_autonomy", "bounded_write_exact", "bounded_write_patch"):
        if not _output_has_status(content):
            missing.append("status")

    for field in required_by_mode.get(mode, []):
        if field not in lowered:
            missing.append(field)

    exact_deliverables = [
        field_label(field)
        for field in data.get("deliverable_fields", [])
        if is_exact_deliverable_field(field)
    ]
    for field in exact_deliverables:
        label_text = field.replace("_", " ")
        if field not in lowered and label_text not in lowered:
            missing.append(field)

    if mode in ("context_pack", "context_pack_report", "managed_autonomy"):
        if data.get("read_only_paths") and "files inspected" not in lowered and "files gathered" not in lowered:
            missing.append("files_inspected")
        if data.get("required_commands") and "commands used" not in lowered and "commands run" not in lowered:
            missing.append("commands_used")
        if not evidence_coverage_complete:
            missing.append("evidence_coverage")
            missing.append("evidence_coverage_incomplete")

    if _verification_contract_requested(data) and _output_claims_verification(content) and not verification_observed:
        missing.append("verification_observed")
        missing.append("verification_not_observed")

    if missing:
        return "INVALID", sorted(set(missing))
    return "VALID_FINAL_REPORT", []


def validate_report_contract(
    text: str,
    mode: str,
    envelope: dict[str, Any] | None,
    *,
    verification_observed: bool = False,
    evidence_coverage_complete: bool = True,
) -> tuple[bool, list[str]]:
    classification, missing = classify_report_output(
        text,
        mode,
        envelope,
        verification_observed=verification_observed,
        evidence_coverage_complete=evidence_coverage_complete,
    )
    return classification in {"VALID_FINAL_REPORT", "POLICY_REFUSAL"}, missing


def command_aware_context_plan(envelope: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the intended context-pack operations for audit/tests."""
    data = compile_task_envelope_v1(envelope or {})
    plan: list[dict[str, Any]] = []
    required_commands = [str(c).strip() for c in data.get("required_commands", []) if str(c).strip()]
    paths_with_command = set()
    for cmd in required_commands:
        lower = cmd.lower()
        if "grep" in lower or "rg " in lower:
            target = cmd.split()[-1] if cmd.split() else ""
            paths_with_command.add(target.lstrip("./"))
            plan.append({"kind": "grep", "command": cmd, "target": target})
        elif "read" in lower or lower.startswith(("cat ", "sed ")):
            target = cmd.split()[-1] if cmd.split() else ""
            paths_with_command.add(target.lstrip("./"))
            plan.append({"kind": "read", "command": cmd, "target": target})
        else:
            plan.append({"kind": "command_summary", "command": cmd})
    for path in data.get("read_only_paths", []):
        clean = str(path).lstrip("./")
        if clean not in paths_with_command:
            plan.append({"kind": "read", "target": path})
    return plan
