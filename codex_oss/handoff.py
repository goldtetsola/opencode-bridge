"""Structured OSS handoff parsing and validation.

The bridge and CLI share this module so a handoff rejected by
``codex-oss validate-handoff`` is rejected the same way at runtime.
"""

from __future__ import annotations

import json
from typing import Optional


HANDOFF_JSON_REQUIRED_FIELDS = (
    "schema_version", "role", "goal", "task_type", "owned_paths",
    "read_only_paths", "forbidden_actions", "verification_steps",
    "deliverable_fields", "completion_rule", "escalation_rule",
)


def empty_task_envelope() -> dict:
    return {
        "role": "", "goal": "", "task_type": "",
        "read_only_paths": [], "owned_paths": [],
        "forbidden_actions": [], "verification_steps": [],
        "deliverable_fields": [], "write_allowed": False,
        "exact_content": "", "no_tools_required": False,
        "proof_critical": False, "schema_error": "",
        "completion_rule": "", "escalation_rule": "",
    }


def extract_structured_handoff_json(handoff_text: str) -> tuple[Optional[dict], str]:
    marker = "OSS_HANDOFF_JSON"
    idx = handoff_text.find(marker)
    if idx < 0:
        return None, ""
    tail = handoff_text[idx + len(marker):].lstrip()
    if tail.startswith(":"):
        tail = tail[1:].lstrip()
    if tail.startswith("```"):
        tail_lines = tail.splitlines()
        if tail_lines:
            tail = "\n".join(tail_lines[1:])
        fence_idx = tail.find("```")
        if fence_idx >= 0:
            tail = tail[:fence_idx]
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail)
    except Exception as exc:
        return None, f"invalid JSON after OSS_HANDOFF_JSON: {exc}"
    if not isinstance(obj, dict):
        return None, "OSS_HANDOFF_JSON must be a JSON object"
    return obj, ""


def validate_string_list(obj: dict, field: str, errors: list[str]) -> list[str]:
    value = obj.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{field} must be a list of strings")
        return []
    return [item.strip() for item in value if item.strip()]


def structured_handoff_to_envelope(handoff_text: str) -> Optional[dict]:
    obj, error = extract_structured_handoff_json(handoff_text)
    if obj is None and not error:
        return None
    envelope = empty_task_envelope()
    if error:
        envelope["schema_error"] = error
        return envelope

    errors: list[str] = []
    missing = [field for field in HANDOFF_JSON_REQUIRED_FIELDS if field not in obj]
    if missing:
        errors.append("missing required fields: " + ", ".join(missing))
    if obj.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    for field in ("role", "goal", "task_type", "completion_rule", "escalation_rule"):
        if field in obj and not isinstance(obj.get(field), str):
            errors.append(f"{field} must be a string")

    envelope["role"] = str(obj.get("role", "")).strip()
    envelope["goal"] = str(obj.get("goal", "")).strip()
    envelope["task_type"] = str(obj.get("task_type", "")).strip()
    envelope["owned_paths"] = validate_string_list(obj, "owned_paths", errors)
    envelope["read_only_paths"] = validate_string_list(obj, "read_only_paths", errors)
    envelope["forbidden_actions"] = validate_string_list(obj, "forbidden_actions", errors)
    envelope["verification_steps"] = validate_string_list(obj, "verification_steps", errors)
    envelope["deliverable_fields"] = validate_string_list(obj, "deliverable_fields", errors)
    envelope["completion_rule"] = str(obj.get("completion_rule", "")).strip()
    envelope["escalation_rule"] = str(obj.get("escalation_rule", "")).strip()
    envelope["write_allowed"] = bool(obj.get("write_allowed", bool(envelope["owned_paths"])))
    envelope["exact_content"] = str(obj.get("exact_content", "")).strip()
    envelope["proof_critical"] = bool(obj.get("proof_critical", False))

    if envelope["write_allowed"] and not envelope["owned_paths"]:
        errors.append("write_allowed requires owned_paths")
    if not envelope["read_only_paths"] and not envelope["owned_paths"] and not envelope["deliverable_fields"]:
        errors.append("handoff must define read_only_paths, owned_paths, or deliverable_fields")

    envelope["schema_error"] = "; ".join(errors)
    return envelope


def validate_handoff_text(handoff_text: str) -> dict:
    envelope = structured_handoff_to_envelope(handoff_text)
    if envelope is None:
        envelope = empty_task_envelope()
        envelope["schema_error"] = "missing OSS_HANDOFF_JSON block"
    return envelope
