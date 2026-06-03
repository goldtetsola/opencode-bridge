"""Registered EvidenceShapeV1 vocabulary for MissionV1 source requirements."""

from __future__ import annotations

import re
from typing import Any


REGISTERED_EVIDENCE_SHAPES: dict[str, str] = {
    "function_definition": r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(",
    "class_definition": r"^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\s*[\(:]",
    "mapping_assignment": r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*\{",
    "dictionary_entry": r"[\"'][A-Za-z0-9_.-]+[\"']\s*:\s*[^,\n]+",
    "literal_value": r"[\"'][^\"']+[\"']|\b\d+\b|\btrue\b|\bfalse\b",
    "config_value": r"[\"'][A-Za-z0-9_.-]+[\"']\s*:\s*[^,\n]+",
    "flag_parameter": r"\b(flag|allow|require)_[A-Za-z0-9_]+\b",
    "flag_read": r"\.(get|pop)\(\s*[\"'](?:flag|allow|require)_[A-Za-z0-9_]+[\"']",
    "behavior_derivation": r"\b(derive|derived|observed behavior|behavior provenance)\b",
    "zero_match": r"\b0 matches\b",
    "grep_zero_match": r"\b0 matches\b",
    "test_assertion": r"\bassert\b|\bself\.assert",
    "test_definition": r"^\s*def\s+test_[A-Za-z_][A-Za-z0-9_]*\s*\(",
    "verification_command": r"\b(pytest|unittest|verify|verification)\b",
    "source_change": r"^\+[^+]",
    "desktop_gold_requires_transcript": r"desktop[-_ ]native|desktop[-_ ]gold|desktop.*transcript|transcript.*desktop",
    "claim_tuple": r"claim_type|route_kind|consumer_kind|effective_status|effective_scope|reasons",
    "consumer_kind": r"consumer_kind|direct_bridge_harness|codex_desktop_spawned",
}


MULTILINE_EVIDENCE_SHAPES = frozenset({"function_definition", "class_definition", "test_definition", "source_change"})


def registered_shape_names() -> set[str]:
    return set(REGISTERED_EVIDENCE_SHAPES)


def is_registered_shape(shape: str) -> bool:
    return str(shape or "") in REGISTERED_EVIDENCE_SHAPES


def detect_registered_shapes(text: str) -> list[str]:
    content = str(text or "")
    lowered = content.lower()
    shapes: list[str] = []
    for shape, pattern in REGISTERED_EVIDENCE_SHAPES.items():
        flags = re.MULTILINE if shape in MULTILINE_EVIDENCE_SHAPES else 0
        haystack = content if flags else lowered
        if re.search(pattern, haystack, flags):
            shapes.append(shape)
    return shapes


def custom_shape_patterns(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        shape = str(key or "").strip()
        pattern = str(value or "").strip()
        if shape and pattern:
            result[shape] = pattern
    return result


def shape_is_registered_or_custom(shape: str, patterns: dict[str, str] | None = None) -> bool:
    name = str(shape or "").strip()
    return bool(name and (is_registered_shape(name) or name in (patterns or {})))

