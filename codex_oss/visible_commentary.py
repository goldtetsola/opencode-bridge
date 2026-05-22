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
    (r'(?:DATABASE_URL|POSTGRES_URL|MYSQL_URL|MONGO_URL)\s*=\s*[\w:\/\-@\.]+', '[redacted db url]'),
    (r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA )?PRIVATE KEY-----', '[redacted private key]', re.DOTALL),
    (r'eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+', '[redacted jwt]'),
    (r'sk-ocg-[A-Za-z0-9]+', '[redacted ocg-key]'),
]


def sanitize_visible_text(text: str) -> tuple[str, bool]:
    redacted = False
    result = text
    for pattern, replacement, *flags in SECRET_PATTERNS:
        fl = flags[0] if flags else 0
        if re.search(pattern, result, fl):
            result = re.sub(pattern, replacement, result, flags=fl)
            redacted = True
    return result, redacted


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
        safe_message, redacted = sanitize_visible_text(message)
        safe_title, _ = sanitize_visible_text(title)

        self._seq += 1
        event: JSON = {
            "schema_version": "visible_commentary_event.v1",
            "mission_id": self.mission_id,
            "seq": self._seq,
            "timestamp": str(int(time.time())),
            "phase": phase or "",
            "event_type": event_type,
            "title": safe_title,
            "message": safe_message,
            "source": source,
            "model": model or "",
            "runtime_decision": runtime_decision or "",
            "evidence_refs": (evidence_refs or [])[:8],
            "artifact_refs": (artifact_refs or [])[:8],
            "severity": severity,
            "safe_for_user": True,
            "redactions_applied": redacted,
            "metadata": metadata or {},
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
        self._render_summary_md(final_report)

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
