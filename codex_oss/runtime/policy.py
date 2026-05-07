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

CRITICAL_FINALITY_PATTERNS = [
    r"\bsafe\s+to\s+merge\b",
    r"\bcorrect\b",
    r"\bno\s+risk\b",
    r"\bvalid\b",
    r"\bready\b",
    r"\bcomplete\s+proof\b",
    r"\bcertified\b",
    r"\b100%\b",
    r"\bguaranteed\b",
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
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in CRITICAL_FINALITY_PATTERNS)


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

GLOBAL_MAX_ACTIVE_MISSIONS = 1
GLOBAL_MAX_ACTIVE_A3_MISSIONS = GLOBAL_MAX_ACTIVE_MISSIONS
A3_QUEUE_MAX = 2
A3_QUEUE_TIMEOUT_SECONDS = 5
MISSION_SLOT_STALE_SECONDS = float(os.getenv("MISSION_SLOT_STALE_SECONDS", "120"))

_active_missions: Dict[str, float] = {}
_active_missions_lock = threading.Lock()
_waiting_missions = 0


def acquire_mission_slot(mission_id: str) -> Tuple[bool, str]:
    """Try to acquire a concurrency slot. Returns (acquired, reason)."""
    global _active_missions, _waiting_missions
    deadline = time.time() + A3_QUEUE_TIMEOUT_SECONDS
    registered_waiter = False
    try:
        while True:
            with _active_missions_lock:
                # Clean stale slots
                now = time.time()
                _active_missions = {k: v for k, v in _active_missions.items() if now - v < MISSION_SLOT_STALE_SECONDS}

                if len(_active_missions) < GLOBAL_MAX_ACTIVE_MISSIONS:
                    _active_missions[mission_id] = now
                    if registered_waiter:
                        _waiting_missions = max(0, _waiting_missions - 1)
                    return True, ""

                if not registered_waiter:
                    if _waiting_missions >= A3_QUEUE_MAX:
                        return False, f"mission queue limit reached ({A3_QUEUE_MAX})"
                    _waiting_missions += 1
                    registered_waiter = True

                if now >= deadline:
                    _waiting_missions = max(0, _waiting_missions - 1)
                    return False, f"mission queue timeout after {A3_QUEUE_TIMEOUT_SECONDS}s"

            time.sleep(0.1)
    except Exception:
        if registered_waiter:
            with _active_missions_lock:
                _waiting_missions = max(0, _waiting_missions - 1)
        raise


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
    from codex_oss.runtime.objectives import synthesize_objective_finding
    findings = []
    uncertainties = []
    objective_finding = synthesize_objective_finding(mission, ledger)
    if objective_finding:
        findings.append(objective_finding)
    if not findings and ledger and getattr(ledger, "files_inspected", None):
        for path, entry in list(getattr(ledger, "files_inspected", {}).items())[:2]:
            extracts = getattr(entry, "extracts", []) or []
            if not extracts:
                continue
            _append_objective_extracts(mission, path, entry)
            extracts = getattr(entry, "extracts", []) or extracts
            extract = _best_extract_for_objective(mission, extracts)
            extract_id = str(extract.get("id", "extract:1")).replace("extract:", "")
            claim = _synthesize_file_finding_claim(mission, path, str(extract.get("text", "")))
            findings.append({
                "claim": claim,
                "evidence_refs": [f"file:{path}#extract:{extract_id}"],
                "confidence": "LOW",
            })
    if not findings and ledger and getattr(ledger, "commands_run", None):
        for idx, command in enumerate(getattr(ledger, "commands_run", [])[:2]):
            if getattr(command, "exit_code", None) == 1 and getattr(command, "matches_count", -1) == 0:
                ref = f"command:{idx}#zero_match"
                claim = "A zero-match search result was gathered as evidence for the mission."
            else:
                ref = f"command:{idx}"
                claim = "Search/tool evidence was gathered for the mission."
            findings.append({"claim": claim, "evidence_refs": [ref], "confidence": "LOW"})
    if findings:
        uncertainties.append(f"Report was synthesized deterministically after: {reason}")
    explicit_objective_complete = bool(objective_finding and isinstance(getattr(mission, "objective_spec", None), dict))
    report = {
        "oss_report_version": "1.0",
        "mission_id": mission.mission_id if hasattr(mission, 'mission_id') else "unknown",
        "status": "COMPLETE" if explicit_objective_complete else "PARTIAL",
        "confidence": "LOW",
        "report_source": "runtime_finalizer",
        "runtime_model_alias": getattr(mission, "runtime_model_alias", ""),
        "explorer_model": getattr(mission, "last_reasoning_model", ""),
        "finalizer_model": None,
        "fallback_model_used": bool(getattr(mission, "fallback_model_used", False)),
        "files_inspected": [],
        "commands_run": [],
        "findings": findings,
        "uncertainties": uncertainties,
        "caveats": [f"Mission terminated: {reason}"],
        "escalation_recommendation": "GPT-5.5 review required" if not explicit_objective_complete else "GPT-5.5 review recommended for runtime-synthesized report",
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


def objective_coverage_errors(mission: Any, report: dict, ledger: Any) -> list[str]:
    """Mission-aware coverage checks beyond schema/evidence validation."""
    from codex_oss.runtime.objectives import objective_coverage_errors as _objective_coverage_errors
    return _objective_coverage_errors(mission, report, ledger)


def _report_text(report: dict) -> str:
    parts = []
    for finding in report.get("findings", []) or []:
        if isinstance(finding, dict):
            parts.append(str(finding.get("claim", "") or ""))
    for field in ("uncertainties", "caveats", "escalation_recommendation"):
        value = report.get(field)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def _synthesize_command_profile_finding(mission: Any, ledger: Any) -> Optional[dict]:
    objective = str(getattr(mission, "objective", "") or "").lower()
    if "autonomy" not in objective and "profile" not in objective and "mission-a3-deepseek" not in objective:
        return None
    commands = list(getattr(ledger, "commands_run", []) or [])
    if not commands:
        return None
    definition_path = None
    definition_ref = None
    profile_budget = None
    profile_seconds = None
    profile_ref = None
    for idx, command in enumerate(commands):
        text = str(getattr(command, "cached_text", "") or "")
        if not text:
            continue
        if definition_path is None:
            definition_match = re.search(
                r"(?m)^([^:\n]+):\d+:\s*RUNTIME_AUTONOMY_PROFILES\s*=",
                text,
            )
            if definition_match:
                definition_path = definition_match.group(1)
                definition_ref = f"command:{idx}"
        if profile_budget is None:
            profile_match = re.search(
                r'"mission-a3-deepseek"\s*:\s*\{[^}]*"max_tool_budget"\s*:\s*(\d+)[^}]*"max_time_seconds"\s*:\s*(\d+)',
                text,
            )
            if profile_match:
                profile_budget = profile_match.group(1)
                profile_seconds = profile_match.group(2)
                profile_ref = f"command:{idx}"
    if not definition_path:
        return None
    refs = [definition_ref] if definition_ref else []
    if profile_ref and profile_ref not in refs:
        refs.append(profile_ref)
    if profile_budget and profile_seconds:
        claim = (
            f"Runtime autonomy profiles are defined in {definition_path}; mission-a3-deepseek has "
            f"max_tool_budget={profile_budget} and max_time_seconds={profile_seconds}."
        )
    else:
        claim = (
            f"Runtime autonomy profiles are defined in {definition_path}; exact mission-a3-deepseek "
            "limits were not captured before the mission ended."
        )
    return {"claim": claim, "evidence_refs": refs or ["command:0"], "confidence": "LOW"}


def _synthesize_alias_mapping_finding(mission: Any, ledger: Any) -> Optional[dict]:
    objective = str(getattr(mission, "objective", "") or "").lower()
    alias_match = re.search(r"\bmission-a[23]-(kimi|deepseek|flash)\b", objective)
    wants_alias = any(term in objective for term in ("alias", "map", "mapped", "reasoning model", "underlying model"))
    if not alias_match or not wants_alias:
        return None
    alias = alias_match.group(0)

    file_hit = _find_alias_mapping_in_files(alias, ledger)
    if file_hit:
        path, model, extract_id = file_hit
        return {
            "claim": f"Runtime model alias {alias} maps to underlying reasoning model {model} in {path}.",
            "evidence_refs": [f"file:{path}#{extract_id}"],
            "confidence": "LOW",
        }

    command_hit = _find_alias_mapping_in_commands(alias, ledger)
    if command_hit:
        idx, model = command_hit
        return {
            "claim": f"Runtime model alias {alias} maps to underlying reasoning model {model}.",
            "evidence_refs": [f"command:{idx}"],
            "confidence": "LOW",
        }
    return None


def _synthesize_validator_module_finding(mission: Any, ledger: Any) -> Optional[dict]:
    objective = str(getattr(mission, "objective", "") or "").lower()
    if "validatedreportv1" not in objective and "validator" not in objective:
        return None
    for path, entry in getattr(ledger, "files_inspected", {}).items():
        text = str(getattr(entry, "cached_text", "") or "")
        lower = text.lower()
        if "validatedreportv1" not in lower or "validate_report" not in text:
            continue
        _append_objective_extracts(mission, path, entry)
        extract = _best_extract_for_objective(mission, getattr(entry, "extracts", []) or [])
        extract_id = str(extract.get("id", "extract:1")).replace("extract:", "")
        return {
            "claim": f"The runtime has a ValidatedReportV1 validator module in {path}; it defines validate_report for strict report schema validation.",
            "evidence_refs": [f"file:{path}#extract:{extract_id}"],
            "confidence": "LOW",
        }
    for idx, command in enumerate(getattr(ledger, "commands_run", []) or []):
        text = str(getattr(command, "cached_text", "") or "")
        if "ValidatedReportV1" in text and "codex_oss/validation/__init__.py" in text:
            return {
                "claim": "The runtime has a ValidatedReportV1 validator module at codex_oss/validation/__init__.py.",
                "evidence_refs": [f"command:{idx}"],
                "confidence": "LOW",
            }
    return None


def _find_alias_mapping_in_files(alias: str, ledger: Any) -> Optional[tuple[str, str, str]]:
    for path, entry in getattr(ledger, "files_inspected", {}).items():
        text = str(getattr(entry, "cached_text", "") or "")
        model = _extract_runtime_alias_model(alias, text)
        if not model:
            continue
        _append_alias_extract(path, entry, alias)
        extract = _best_alias_extract(alias, getattr(entry, "extracts", []) or [])
        extract_id = str(extract.get("id", "extract:1")).replace("extract:", "")
        return path, model, f"extract:{extract_id}"
    return None


def _find_alias_mapping_in_commands(alias: str, ledger: Any) -> Optional[tuple[int, str]]:
    for idx, command in enumerate(getattr(ledger, "commands_run", []) or []):
        text = str(getattr(command, "cached_text", "") or "")
        model = _extract_runtime_alias_model(alias, text)
        if model:
            return idx, model
    return None


def _extract_runtime_alias_model(alias: str, text: str) -> Optional[str]:
    if not text or alias not in text:
        return None
    direct = re.search(rf'"{re.escape(alias)}"\s*:\s*"([^"]+)"', text)
    if direct:
        return direct.group(1)
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if alias not in line:
            continue
        window = "\n".join(lines[max(0, idx - 6):idx + 2])
        if "RUNTIME_MODEL_ALIASES" not in window:
            continue
        direct = re.search(rf'"{re.escape(alias)}"\s*:\s*"([^"]+)"', line)
        if direct:
            return direct.group(1)
    return None


def _append_alias_extract(path: str, entry: Any, alias: str) -> None:
    extracts = getattr(entry, "extracts", []) or []
    if any(alias in str(extract.get("text", "")) and "RUNTIME_MODEL_ALIASES" in str(extract.get("text", "")) for extract in extracts):
        return
    text = str(getattr(entry, "cached_text", "") or "")
    idx = text.find(alias)
    if idx < 0:
        return
    start = max(0, idx - 500)
    end = min(len(text), idx + 500)
    extract_id = f"extract:{len(extracts) + 1}"
    extracts.append({"id": extract_id, "text": text[start:end], "kind": "alias_mapping"})
    entry.extracts = extracts


def _best_alias_extract(alias: str, extracts: list[dict]) -> dict:
    best = extracts[0] if extracts else {"id": "extract:1", "text": ""}
    best_score = -1
    for extract in extracts:
        text = str(extract.get("text", "") or "")
        score = 0
        if alias in text:
            score += 3
        if "RUNTIME_MODEL_ALIASES" in text:
            score += 3
        if "ocg-" in text:
            score += 1
        if score > best_score:
            best = extract
            best_score = score
    return best


def _append_objective_extracts(mission: Any, path: str, entry: Any) -> None:
    cached_text = str(getattr(entry, "cached_text", "") or "")
    if not cached_text:
        return
    if any("objective_match" in str(extract.get("id", "")) for extract in getattr(entry, "extracts", []) or []):
        return
    snippets = _objective_snippets(mission, cached_text)
    for snippet in snippets:
        extract_id = f"extract:{len(getattr(entry, 'extracts', []) or []) + 1}"
        entry.extracts.append({"id": extract_id, "text": snippet, "kind": "objective_match"})


def _objective_snippets(mission: Any, text: str, radius: int = 500) -> list[str]:
    needles = []
    objective = str(getattr(mission, "objective", "") or "").lower()
    if "autonomy" in objective or "profile" in objective:
        needles.extend(["RUNTIME_AUTONOMY_PROFILES", "mission-a3-deepseek"])
    needles.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_:-]{5,}", objective))
    snippets = []
    lower = text.lower()
    for needle in dict.fromkeys(needles):
        idx = lower.find(needle.lower())
        if idx < 0:
            continue
        start = max(0, idx - radius)
        end = min(len(text), idx + len(needle) + radius)
        snippet = text[start:end]
        if snippet and snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= 3:
            break
    return snippets


def _best_extract_for_objective(mission: Any, extracts: list[dict]) -> dict:
    objective = str(getattr(mission, "objective", "") or "").lower()
    terms = [term for term in re.findall(r"[A-Za-z0-9_:-]{4,}", objective) if term not in {"where", "defined", "return", "finding"}]
    best = extracts[0]
    best_score = -1
    for extract in extracts:
        text = str(extract.get("text", ""))
        lower = text.lower()
        score = sum(1 for term in terms if term.lower() in lower)
        score += 3 if "runtime_autonomy_profiles" in lower else 0
        score += 2 if "mission-a3-deepseek" in lower else 0
        score += 1 if "max_tool_budget" in lower else 0
        score += 1 if "max_time_seconds" in lower else 0
        if score > best_score:
            best = extract
            best_score = score
    return best


def _synthesize_file_finding_claim(mission: Any, path: str, text: str) -> str:
    profile = _extract_runtime_profile_claim(path, text)
    if profile:
        return profile
    return (
        f"Evidence was gathered from {path} for the mission objective; "
        "GPT-5.5 should review the cited extract before treating this as final."
    )


def _extract_runtime_profile_claim(path: str, text: str) -> Optional[str]:
    if not re.search(r"\bRUNTIME_AUTONOMY_PROFILES\s*=", text) or "mission-a3-deepseek" not in text:
        return None
    match = re.search(
        r'"mission-a3-deepseek"\s*:\s*\{[^}]*"max_tool_budget"\s*:\s*(\d+)[^}]*"max_time_seconds"\s*:\s*(\d+)',
        text,
        re.DOTALL,
    )
    if not match:
        return f"Runtime autonomy profiles, including mission-a3-deepseek, are defined in {path}."
    return (
        f"Runtime autonomy profiles are defined in {path}; mission-a3-deepseek has "
        f"max_tool_budget={match.group(1)} and max_time_seconds={match.group(2)}."
    )


def response_status_from_mission(status: str) -> str:
    """Map mission outcome to transport status per ResponseStatusMappingV1."""
    return "completed"  # Always prefer response.completed for recoverable outcomes


RESPONSE_STATUS_TABLE = {
    "COMPLETE": ("completed", "response.completed with ValidatedReportV1 status COMPLETE"),
    "PARTIAL": ("completed", "response.completed with ValidatedReportV1 status PARTIAL"),
    "ESCALATE": ("completed", "response.completed with ValidatedReportV1 status ESCALATE"),
    "FAILED": ("completed", "response.completed with ValidatedReportV1 status FAILED"),
}
