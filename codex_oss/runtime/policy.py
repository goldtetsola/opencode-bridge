"""Spec policy implementations — CriticalPathRegistry, BroadScope, RiskTier, AllowedPaths, EntryPoint, UpstreamModelCall, Deadline, Fallback, ContextCompaction, State, Concurrency, Privacy."""

from __future__ import annotations

import hashlib
import os
import threading
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

JSON = Dict[str, Any]


# ═════════════════════════════════════════════
# CriticalPathRegistryV1
# ═════════════════════════════════════════════

CRITICAL_TERM_PATTERNS = [
    r"\bauth(orization)?\b", r"\bschema\b", r"\bmigration(s)?\b",
    r"\brecovery\b", r"\bfinalizer\b", r"\bcertification\b",
    r"\bci\b", r"\bdeploy(ment)?\b", r"\bpersistence\b",
    r"\bdatabase\b", r"\bdb\b", r"\bpostgres\b", r"\bsqlite\b",
    r"\bsecrets?\b",
]

CRITICAL_PATH_ROOTS = [
    "src/auth/", "src/schema/", "migrations/",
    ".github/workflows/", "scripts/recovery/", "scripts/finalizer/",
]

CRITICAL_FINALITY_TERMS = [
    "safe to merge", "correct", "no risk", "valid", "ready",
    "complete proof", "certified", "100%", "guaranteed",
]

_proj_critical_roots: List[str] = []
_proj_critical_terms: List[str] = []


def load_critical_config(project_root: str = "."):
    """Load project-specific critical path config from .codex/config.toml or env."""
    global _proj_critical_roots, _proj_critical_terms
    raw = os.getenv("PROJECT_CRITICAL_ROOTS", "")
    if raw:
        _proj_critical_roots = [r.strip() for r in raw.split(",") if r.strip()]
    raw_terms = os.getenv("PROJECT_CRITICAL_TERMS", "")
    if raw_terms:
        _proj_critical_terms = [t.strip() for t in raw_terms.split(",") if t.strip()]


def is_critical_path(path: str) -> bool:
    """Check if a path matches critical path roots."""
    p = path.lower().strip("/") + "/"
    all_roots = CRITICAL_PATH_ROOTS + _proj_critical_roots
    for root in all_roots:
        r = root.lower().strip("/") + "/"
        if p.startswith(r):
            return True
    return False


def detect_critical_terms(text: str) -> List[str]:
    """Detect critical path terms in text. Returns list of matched terms."""
    found = []
    all_patterns = CRITICAL_TERM_PATTERNS + _proj_critical_terms
    for pat in all_patterns:
        if re.search(pat, text, re.IGNORECASE):
            found.append(pat.replace("\\b", "").replace("\\", ""))
    return found


def detect_critical_finality(text: str) -> bool:
    """Check if text makes a critical finality claim."""
    t = text.lower()
    return any(term in t for term in CRITICAL_FINALITY_TERMS)


# ═════════════════════════════════════════════
# RiskTierPolicyV1
# ═════════════════════════════════════════════

def risk_tier_allows_autonomy(risk_tier: str, tier: str) -> Tuple[bool, str]:
    """Check if autonomy is allowed at this risk tier. Returns (allowed, reason)."""
    if risk_tier == "critical":
        return False, "critical risk tier forbids autonomous exploration"
    if risk_tier == "medium" and tier == "A3":
        return True, ""  # allowed but confidence cannot be HIGH
    if risk_tier == "low":
        return True, ""
    return True, ""


def risk_tier_confidence_ceiling(risk_tier: str) -> str:
    """Maximum confidence allowed at this risk tier."""
    if risk_tier in ("medium", "critical"):
        return "MEDIUM"
    return "HIGH"


# ═════════════════════════════════════════════
# BroadScopePolicyV1
# ═════════════════════════════════════════════

BROAD_ROOTS = {".", "src", "scripts", "docs", "/"}


def is_broad_root(root: str, max_files: int = 20) -> bool:
    """Check if a root is too broad for safe allocation."""
    r = root.strip().rstrip("/") or "."
    if r in BROAD_ROOTS or r == "." or r == "":
        return True
    try:
        files = len(os.listdir(os.path.join(os.getcwd(), r))) if os.path.isdir(r) else 0
        return files > max_files
    except OSError:
        return True
    return False


def validate_broad_scope(mission) -> Optional[str]:
    """Validate broad scope rules. Returns error or None."""
    for root in mission.allowed_roots:
        if is_broad_root(root, mission.max_files_to_inspect):
            if not mission.allow_broad_read_scope:
                return f"Broad root '{root}' requires allow_broad_read_scope=true"
            if mission.risk_tier == "critical":
                return f"Broad root '{root}' forbidden at critical risk tier"
            if mission.critical_path_read_allowed:
                return f"Broad root '{root}' does not count as explicit critical permission"
    return None


def critical_read_allowed_for_path(path: str, mission) -> Tuple[bool, str]:
    """Critical reads require explicit critical permission plus narrow scope."""
    if not getattr(mission, "critical_path_read_allowed", False):
        return False, "critical_path_read_allowed=false"
    if not getattr(mission, "critical_path_reason", None):
        return False, "critical_path_reason is required"

    normalized = path.strip().rstrip("/")
    for allowed in getattr(mission, "allowed_paths", []) or []:
        if normalized == allowed.strip().rstrip("/"):
            return True, ""

    for root in getattr(mission, "allowed_roots", []) or []:
        root_norm = root.strip().rstrip("/")
        if is_broad_root(root_norm):
            continue
        root_check = root_norm + "/"
        if normalized == root_norm or (normalized + "/").startswith(root_check):
            return True, ""

    return False, "critical path reads require exact allowed_paths or narrow allowed_roots"


# ═════════════════════════════════════════════
# EntryPointExtractionPolicyV1
# ═════════════════════════════════════════════

def extract_single_handoff_block(text: str) -> Optional[str]:
    """Extract exactly one OSS_HANDOFF_JSON block. Raises on multiple."""
    blocks = re.findall(r"<OSS_HANDOFF_JSON>\s*\n?(.*?)\n?</OSS_HANDOFF_JSON>", text, re.DOTALL)
    if not blocks:
        return None
    if len(blocks) > 1:
        raise ValueError("Multiple OSS_HANDOFF_JSON blocks found — exactly one required")
    return blocks[0].strip()


# ═════════════════════════════════════════════
# UpstreamModelCallPolicyV1
# ═════════════════════════════════════════════

def model_call_uses_internal_tools_only(tier: str) -> bool:
    """For A2/A3, model calls must NOT include native tool definitions."""
    return tier in ("A2", "A3")


# ═════════════════════════════════════════════
# ContextCompactionPolicyV1
# ═════════════════════════════════════════════

COMPACT_LEDGER_MAX_CHARS = 6000
CONTEXT_PACK_GLOBAL_MAX = 24000

SMALL_FILE_THRESHOLD = 12000
MEDIUM_FILE_THRESHOLD = 40000


def compact_file_content(raw: str, path: str, max_chars: int = 8000) -> str:
    """Compact file content for model context."""
    n = len(raw)
    if n <= SMALL_FILE_THRESHOLD:
        return raw[:COMPACT_LEDGER_MAX_CHARS]
    if n <= MEDIUM_FILE_THRESHOLD:
        first = min(8000, max_chars)
        last = min(4000, max_chars - first)
        return f"=== {path} (first {first} + last {last} of {n} chars) ===\n{raw[:first]}\n...\n{raw[-last:]}"
    sha = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"=== {path} ({n} chars, sha256:{sha}) ===\n[Large file — metadata only. Use targeted search for specific content.]"


def compact_search_result(stdout: str, max_matches: int = 50) -> str:
    """Compact search results."""
    lines = stdout.strip().split("\n") if stdout else []
    if len(lines) <= max_matches:
        return stdout
    return "\n".join(lines[:max_matches]) + f"\n... ({len(lines) - max_matches} more matches) ..."


def enforce_context_budget(context: list, max_chars: int = COMPACT_LEDGER_MAX_CHARS) -> list:
    """Trim context to stay under budget, keeping system message and recent turns."""
    total = sum(len(str(m.get("content", ""))) for m in context)
    if total <= max_chars:
        return context

    out = []
    budget_used = 0
    # Always keep first system message
    for i, msg in enumerate(context):
        if msg.get("role") == "system" and i == 0:
            out.append(msg)
            budget_used += len(str(msg.get("content", "")))
            break

    # Keep most recent turns
    for msg in reversed(context):
        if msg.get("role") != "system" or out:
            size = len(str(msg.get("content", "")))
            if budget_used + size > max_chars:
                break
            out.insert(1 if len(out) > 0 else 0, msg)
            budget_used += size

    return out


# ═════════════════════════════════════════════
# DeadlinePolicyV1 + FallbackPolicyV1
# ═════════════════════════════════════════════

@dataclass
class DeadlinePolicy:
    request_deadline: float = 90
    first_byte_timeout: float = 20
    semantic_idle_timeout: float = 30
    fallback_budget: float = 20
    deterministic_partial_reserve: float = 5
    max_repair_attempts: int = 1
    max_model_attempts_per_turn: int = 2
    max_total_model_calls: int = 25
    retry_same_model: bool = False

    _elapsed: float = 0
    _model_calls: int = 0

    def start(self):
        self._elapsed = 0
        self._model_calls = 0

    def remaining(self) -> float:
        return max(0, self.request_deadline - self._elapsed - self.deterministic_partial_reserve)

    def can_call_model(self) -> bool:
        return (self.remaining() > self.first_byte_timeout and
                self._model_calls < self.max_total_model_calls)

    def record_call(self):
        self._model_calls += 1

    def must_return_partial(self) -> bool:
        return self.remaining() < self.fallback_budget

    @property
    def effective_deadline(self) -> float:
        return self.request_deadline - self.deterministic_partial_reserve


# ═════════════════════════════════════════════
# StatePolicyV1
# ═════════════════════════════════════════════

STATE_SCHEMA_VERSION = 1
MISSION_TTL_SECONDS = 86400
RESPONSE_TTL_SECONDS = 86400


# ═════════════════════════════════════════════
# ConcurrencyPolicyV1
# ═════════════════════════════════════════════

GLOBAL_MAX_ACTIVE_A3_MISSIONS = 1
A3_QUEUE_MAX = 2
A3_QUEUE_TIMEOUT_SECONDS = 5

_active_missions: Dict[str, float] = {}
_active_missions_lock = threading.Lock()


def acquire_mission_slot(mission_id: str) -> Tuple[bool, str]:
    """Try to acquire a concurrency slot. Returns (acquired, reason)."""
    global _active_missions
    with _active_missions_lock:
        # Clean stale slots
        now = time.time()
        _active_missions = {k: v for k, v in _active_missions.items() if now - v < 300}

        if len(_active_missions) >= GLOBAL_MAX_ACTIVE_A3_MISSIONS:
            return False, f"global A3 concurrency limit reached ({GLOBAL_MAX_ACTIVE_A3_MISSIONS})"

        _active_missions[mission_id] = now
        return True, ""


def release_mission_slot(mission_id: str):
    with _active_missions_lock:
        _active_missions.pop(mission_id, None)


# ═════════════════════════════════════════════
# PrivacyPolicyV1
# ═════════════════════════════════════════════

SECRET_PATTERNS = [
    r'sk-[a-zA-Z0-9]{20,}',
    r'OPENCODE_GO_API_KEY\s*=\s*\S+',
    r'ANTHROPIC_API_KEY\s*=\s*\S+',
    r'OPENAI_API_KEY\s*=\s*\S+',
    r'DATABASE_URL\s*=\s*\S+',
    r'AUTH_TOKEN\s*=\s*\S+',
    r'-----BEGIN\s.*PRIVATE\sKEY-----',
    r'Bearer\s+[a-zA-Z0-9\-_]{20,}',
]


def scan_secrets(text: str) -> Tuple[str, bool]:
    """Scan and redact secrets from text. Returns (redacted, found_secret)."""
    found = False
    result = text
    for pat in SECRET_PATTERNS:
        if re.search(pat, result, re.IGNORECASE):
            result = re.sub(pat, "[REDACTED]", result, flags=re.IGNORECASE)
            found = True
    return result, found


# ═════════════════════════════════════════════
# AllowedPathsPolicyV1
# ═════════════════════════════════════════════

def check_allowed_paths(path: str, allowed_roots: list, allowed_paths: list) -> Tuple[bool, Optional[str]]:
    """Check path against allowed roots/paths. Exact paths win over broad roots."""
    p = path.strip().rstrip("/")

    # Exact paths first
    for allowed in allowed_paths:
        if p == allowed.strip().rstrip("/"):
            return True, None

    # Roots
    for root in allowed_roots:
        r = root.strip().rstrip("/") + "/"
        if (p + "/").startswith(r) or p == root.strip().rstrip("/"):
            return True, None

    return False, f"path not in allowed roots/paths: {path}"


# ═════════════════════════════════════════════
# Report schemas
# ═════════════════════════════════════════════

def build_deterministic_partial_report(mission, ledger: Any, reason: str) -> dict:
    """Build a spec-compliant deterministic partial report."""
    from codex_oss.validation import render_report
    report = {
        "oss_report_version": "1.0",
        "mission_id": mission.mission_id if hasattr(mission, 'mission_id') else "unknown",
        "status": "PARTIAL",
        "confidence": "LOW",
        "files_inspected": [],
        "commands_run": [],
        "findings": [],
        "uncertainties": [],
        "caveats": [f"Mission terminated: {reason}"],
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": [],
    }
    if ledger and hasattr(ledger, 'files_inspected'):
        report["files_inspected"] = [{"path": p, "complete": e.complete} for p, e in getattr(ledger, 'files_inspected', {}).items()]
    if ledger and hasattr(ledger, 'commands_run'):
        report["commands_run"] = [{"tool": c.tool, "args": c.args} for c in getattr(ledger, 'commands_run', [])]
    if ledger and hasattr(ledger, 'risk_flags'):
        for flag in getattr(ledger, 'risk_flags', []):
            report["caveats"].append(flag)
    return report


def response_status_from_mission(status: str) -> str:
    """Map mission outcome to transport status per ResponseStatusMappingV1."""
    return "completed"  # Always prefer response.completed for recoverable outcomes


RESPONSE_STATUS_TABLE = {
    "COMPLETE": ("completed", "response.completed with ValidatedReportV1 status COMPLETE"),
    "PARTIAL": ("completed", "response.completed with ValidatedReportV1 status PARTIAL"),
    "ESCALATE": ("completed", "response.completed with ValidatedReportV1 status ESCALATE"),
    "FAILED": ("completed", "response.completed with ValidatedReportV1 status FAILED"),
}
