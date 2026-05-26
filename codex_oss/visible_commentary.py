"""VisibleCommentaryV1 — user-facing progress narration for runtime-backed OSS subagents.

Writes visible_commentary.jsonl per mission and generates summary.md.
Never exposes raw chain-of-thought, secrets, raw file dumps, or private internals.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

JSON = dict[str, Any]

SECRET_PATTERNS: list[tuple[str, str]] = [
    (r'(?:OPENCODE_GO|OPENCODE|OPENAI|ANTHROPIC|PROXY)_(?:API_)?KEY\s*=\s*(sk-[A-Za-z0-9\-_]+)', '[redacted key]'),
    (r'Bearer\s+(sk-[A-Za-z0-9\-_]+)', 'Bearer [redacted]'),
    (r'sk-[A-Za-z0-9\-_]{12,}', '[redacted key]'),
    (r'(?:DATABASE_URL|POSTGRES_URL|MYSQL_URL|MONGO_URL)\s*=\s*[\w:\/\-@\.]+', '[redacted db url]'),
    (r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA )?PRIVATE KEY-----', '[redacted private key]', re.DOTALL),
    (r'eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+', '[redacted jwt]'),
    (r'sk-ocg-[A-Za-z0-9]+', '[redacted ocg-key]'),
]

SENSITIVE_PATH_PARTS = {".env", "secrets", ".codex-oss/env", ".codex-oss/state", ".codex-oss/logs"}

EVENT_CATEGORIES = {
    "mission_started": "lifecycle",
    "mission_completed": "lifecycle",
    "mission_failed": "lifecycle",
    "mission_partial": "lifecycle",
    "mission_escalated": "lifecycle",
    "model_action_requested": "model_action",
    "tool_action_planned": "model_action",
    "model_action_failed": "model_action",
    "model_action_invalid": "model_action",
    "model_report_received": "model_action",
    "model_narrated_runtime_closed": "model_action",
    "tool_action_started": "tool_execution",
    "tool_result_summary": "tool_execution",
    "coverage_update": "coverage",
    "runtime_redirect": "runtime_decision",
    "runtime_closure_started": "runtime_decision",
    "report_downgraded": "runtime_decision",
    "deterministic_fast_path_used": "runtime_decision",
    "patch_intent_received": "patch_safety",
    "patch_validation_started": "patch_safety",
    "patch_validation_passed": "patch_safety",
    "patch_validation_failed": "patch_safety",
    "verification_passed": "verification",
    "verification_failed": "verification",
}


def sanitize_visible_text(text: str) -> tuple[str, bool]:
    redacted = False
    result = text
    for pattern, replacement, *flags in SECRET_PATTERNS:
        fl = flags[0] if flags else 0
        if re.search(pattern, result, fl):
            result = re.sub(pattern, replacement, result, flags=fl)
            redacted = True
    return result, redacted


def visible_event_category(event_type: str, source: str = "") -> str:
    event_type = str(event_type or "")
    if event_type in EVENT_CATEGORIES:
        return EVENT_CATEGORIES[event_type]
    if event_type.startswith("patch_"):
        return "patch_safety"
    if event_type.startswith("verification_"):
        return "verification"
    if event_type.startswith("model_"):
        return "model_action"
    if event_type.startswith("tool_"):
        return "tool_execution"
    if event_type.startswith("runtime_"):
        return "runtime_decision"
    source = str(source or "")
    return source if source in {"runtime", "tool", "coverage", "verification"} else "general"


def safe_tool_target_text(tool_name: str, arguments: Any) -> str:
    safe_tool, _ = _sanitize_public_string(tool_name or "tool", max_chars=80)
    if not isinstance(arguments, dict):
        return safe_tool or "tool"
    raw_path = str(arguments.get("path", "") or "")
    raw_pattern = str(arguments.get("pattern", "") or "")
    path, _ = _public_path_text(raw_path)
    target = safe_tool or "tool"
    if path:
        preposition = "in" if safe_tool == "rtk_grep" else "on"
        target = f"{target} {preposition} {path}"
    if raw_pattern:
        target = f"{target} with a targeted pattern"
    return target


def model_action_public_message(
    tool_name: str,
    arguments: Any,
    *,
    reason: str = "",
    hypothesis: str = "",
    expected_information_gain: str = "",
    why_not_report_yet: str = "",
) -> str:
    target = safe_tool_target_text(tool_name, arguments)
    fields = []
    if str(reason or "").strip():
        fields.append("a reason")
    if str(hypothesis or "").strip():
        fields.append("a hypothesis")
    if str(expected_information_gain or "").strip():
        fields.append("expected information gain")
    if str(why_not_report_yet or "").strip():
        fields.append("why a report is not ready yet")
    if fields:
        return (
            f"The model chose {target} as the next investigation step and declared "
            f"{_join_public_fields(fields)} before running it."
        )
    return f"The model chose {target} as the next investigation step."


def sanitize_visible_payload(value: Any, *, max_chars: int = 240, _key: str = "", _depth: int = 0) -> tuple[Any, bool]:
    if _depth > 4:
        return "[metadata depth limit]", True
    key = str(_key or "").lower()
    if isinstance(value, str):
        if key in {"path", "file", "filename", "workdir", "cwd"}:
            return _public_path_text(value)
        if key in {"pattern", "query"} and value:
            return "[pattern elided]", True
        return _sanitize_public_string(value, max_chars=max_chars)
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, dict):
        redacted = False
        result: JSON = {}
        for idx, (raw_key, raw_value) in enumerate(value.items()):
            if idx >= 24:
                result["__truncated__"] = True
                redacted = True
                break
            safe_key, key_redacted = _sanitize_public_string(str(raw_key), max_chars=80)
            safe_value, value_redacted = sanitize_visible_payload(
                raw_value,
                max_chars=max_chars,
                _key=str(raw_key),
                _depth=_depth + 1,
            )
            result[safe_key] = safe_value
            redacted = redacted or key_redacted or value_redacted
        return result, redacted
    if isinstance(value, (list, tuple, set)):
        redacted = False
        result = []
        for idx, item in enumerate(list(value)):
            if idx >= 16:
                result.append("[list truncated]")
                redacted = True
                break
            safe_item, item_redacted = sanitize_visible_payload(
                item,
                max_chars=max_chars,
                _key=_key,
                _depth=_depth + 1,
            )
            result.append(safe_item)
            redacted = redacted or item_redacted
        return result, redacted
    return _sanitize_public_string(str(value), max_chars=max_chars)


def _sanitize_public_string(value: str, *, max_chars: int) -> tuple[str, bool]:
    safe, redacted = sanitize_visible_text(str(value or ""))
    home = str(Path.home())
    if home and home in safe:
        safe = safe.replace(home, "~")
        redacted = True
    safe = " ".join(safe.split())
    if len(safe) > max_chars:
        safe = safe[: max(0, max_chars - 3)] + "..."
        redacted = True
    return safe, redacted


def _public_path_text(path: str) -> tuple[str, bool]:
    safe, redacted = _sanitize_public_string(path, max_chars=200)
    if not safe:
        return "", redacted
    normalized = safe.replace("\\", "/")
    cwd = str(Path.cwd()).replace("\\", "/")
    if cwd and normalized.startswith(cwd + "/"):
        normalized = normalized[len(cwd) + 1:]
    home = str(Path.home()).replace("\\", "/")
    cwd_public = cwd.replace(home, "~", 1) if home and cwd.startswith(home + "/") else cwd
    if cwd_public and normalized.startswith(cwd_public + "/"):
        normalized = normalized[len(cwd_public) + 1:]
    if home and normalized.startswith(home + "/"):
        normalized = "~/" + normalized[len(home) + 1:]
        redacted = True
    if _looks_sensitive_path(normalized):
        return "[restricted path]", True
    if normalized.startswith("/"):
        normalized = Path(normalized).name or "[absolute path]"
        redacted = True
    if len(normalized) > 160:
        normalized = normalized[:157] + "..."
        redacted = True
    return normalized, redacted


def _looks_sensitive_path(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    parts = [part for part in lowered.split("/") if part]
    if any(part in SENSITIVE_PATH_PARTS for part in parts):
        return True
    if any(lowered == prefix.rstrip("/") or lowered.startswith(prefix) for prefix in (".codex-oss/env/", ".codex-oss/state/", ".codex-oss/logs/")):
        return True
    return any(marker in lowered for marker in ("/.codex-oss/env/", "/secrets/", "token", "secret"))


def _join_public_fields(fields: list[str]) -> str:
    if len(fields) == 1:
        return fields[0]
    if len(fields) == 2:
        return f"{fields[0]} and {fields[1]}"
    return f"{', '.join(fields[:-1])}, and {fields[-1]}"


class VisibleCommentarySink:
    def __init__(
        self,
        mission_id: str,
        mission_dir: Path | str,
        mode: str = "summary",
        stream_callback: Callable[[JSON], None] | None = None,
        max_events: int = 40,
        max_event_chars: int = 500,
    ):
        self.mission_id = mission_id
        self.mission_dir = Path(mission_dir)
        self.mode = mode
        self._stream = stream_callback
        self._max_events = max_events
        self._max_chars = max_event_chars
        self._seq = 0
        self._events: list[JSON] = []
        self._compacted = False
        self._compacted_count = 0
        os.makedirs(self.mission_dir, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.mission_dir / "visible_commentary.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.mission_dir / "summary.md"

    def emit(
        self,
        event_type: str,
        title: str,
        message: str,
        *,
        phase: str | None = None,
        source: str = "runtime",
        model: str | None = None,
        runtime_decision: str | None = None,
        evidence_refs: list[str] | None = None,
        artifact_refs: list[str] | None = None,
        severity: str = "info",
        metadata: JSON | None = None,
        stream: bool = True,
    ) -> JSON | None:
        if self.mode == "off":
            return None

        if severity == "debug" and self.mode != "detailed":
            return None

        if len(self._events) >= self._max_events and not self._compacted:
            self._compacted = True
            self._compacted_count = len(self._events) - self._max_events

        message = message[:self._max_chars]
        title = title[:120]
        safe_message, message_redacted = sanitize_visible_text(message)
        safe_title, title_redacted = sanitize_visible_text(title)
        safe_metadata, metadata_redacted = sanitize_visible_payload(metadata or {})
        safe_evidence_refs, evidence_redacted = sanitize_visible_payload(
            (evidence_refs or [])[:8],
            max_chars=160,
            _key="evidence_ref",
        )
        safe_artifact_refs, artifact_redacted = sanitize_visible_payload(
            (artifact_refs or [])[:8],
            max_chars=160,
            _key="path",
        )
        safe_phase, phase_redacted = _sanitize_public_string(phase or "", max_chars=40)
        safe_source, source_redacted = _sanitize_public_string(source or "runtime", max_chars=80)
        safe_model, model_redacted = _sanitize_public_string(model or "", max_chars=120)
        safe_runtime_decision, decision_redacted = _sanitize_public_string(runtime_decision or "", max_chars=120)
        safe_severity, severity_redacted = _sanitize_public_string(severity or "info", max_chars=40)
        redacted = bool(
            message_redacted
            or title_redacted
            or metadata_redacted
            or evidence_redacted
            or artifact_redacted
            or phase_redacted
            or source_redacted
            or model_redacted
            or decision_redacted
            or severity_redacted
        )

        self._seq += 1
        event: JSON = {
            "schema_version": "visible_commentary_event.v1",
            "mission_id": self.mission_id,
            "seq": self._seq,
            "timestamp": str(int(time.time())),
            "phase": safe_phase,
            "event_type": event_type,
            "category": visible_event_category(event_type, safe_source),
            "title": safe_title,
            "message": safe_message,
            "source": safe_source,
            "model": safe_model,
            "runtime_decision": safe_runtime_decision,
            "evidence_refs": safe_evidence_refs,
            "artifact_refs": safe_artifact_refs,
            "severity": safe_severity,
            "safe_for_user": True,
            "redactions_applied": redacted,
            "metadata": safe_metadata,
        }
        self._events.append(event)
        self._write_event(event)
        if stream and self._stream:
            try:
                self._stream(event)
            except Exception:
                pass
        return event

    def _write_event(self, event: JSON):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")

    def close(self, final_report: JSON | None = None):
        self._ensure_terminal_lifecycle_event(final_report)
        self._render_summary_md(final_report)

    def _ensure_terminal_lifecycle_event(self, final_report: JSON | None = None):
        if self.mode == "off":
            return
        terminal_events = {"mission_completed", "mission_failed", "mission_partial", "mission_escalated"}
        event_types = {str(event.get("event_type", "") or "") for event in self._events if isinstance(event, dict)}
        if event_types & terminal_events:
            return
        status = str((final_report or {}).get("status", "") or "").upper()
        if status == "COMPLETE":
            event_type = "mission_completed"
            title = "Mission completed"
        elif status == "ESCALATE":
            event_type = "mission_escalated"
            title = "Mission escalated"
        elif status == "FAILED":
            event_type = "mission_failed"
            title = "Mission failed"
        else:
            event_type = "mission_partial"
            title = "Mission partially completed"
        self.emit(
            event_type,
            title,
            f"Final status: {status or 'UNKNOWN'}. Final report and evidence artifacts have been persisted.",
            phase="REPORT",
            source="runtime",
        )

    def _render_summary_md(self, final_report: JSON | None = None):
        events = list(self._events)
        status = (final_report or {}).get("status", "unknown")
        objective = str((final_report or {}).get("mission_id", self.mission_id))
        confidence = str((final_report or {}).get("confidence", "unknown"))
        closure = str((final_report or {}).get("closure_source", "unknown"))
        caveats = (final_report or {}).get("caveats", []) or []
        evidence_refs_all: list[str] = []
        for e in events:
            evidence_refs_all.extend(e.get("evidence_refs", []) or [])

        lines = [
            f"# Mission Summary: {self.mission_id}",
            f"",
            f"**Status:** {status}",
            f"**Confidence:** {confidence}",
            f"**Closure:** {closure}",
            f"",
            f"## Progress",
        ]

        for i, e in enumerate(events[:50]):
            phase = e.get("phase", "")
            title = e.get("title", "")
            msg = e.get("message", "")
            prefix = f"[{phase}]" if phase else ""
            lines.append(f"{i + 1}. {prefix} **{title}**")
            if msg and msg != title:
                lines.append(f"   {msg}")

        runtime_decisions = [e for e in events if e.get("runtime_decision")]
        if runtime_decisions:
            lines.append("")
            lines.append("## Runtime Decisions")
            for e in runtime_decisions[-5:]:
                lines.append(f"- **{e.get('title', '')}**: {e.get('message', '')}")

        if evidence_refs_all:
            lines.append("")
            lines.append("## Evidence Refs")
            for ref in evidence_refs_all[:10]:
                lines.append(f"- `{ref}`")

        if caveats:
            lines.append("")
            lines.append("## Caveats")
            for c in caveats[:5]:
                lines.append(f"- {c}")

        lines.append("")
        lines.append(f"*Visible trace: .codex-oss/missions/{self.mission_id}/visible_commentary.jsonl*")

        with open(self.summary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    @property
    def event_count(self) -> int:
        return len(self._events)
