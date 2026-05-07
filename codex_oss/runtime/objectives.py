"""ObjectiveCoveragePolicyV1 and EvidenceFinalizerV1 primitives.

This module keeps mission-specific "what counts as done?" logic out of the
runtime loop. It operates only on MissionV1 objective text and ledger evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ObjectiveSpec:
    objective_type: str
    target: str = ""
    required_outputs: list[str] = field(default_factory=list)
    evidence_shape: str = ""
    required_evidence_shapes: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    explicit: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceHit:
    ref: str
    text: str
    path: str = ""


def classify_objective(mission: Any) -> ObjectiveSpec:
    explicit = _explicit_objective_spec(mission)
    if explicit:
        return explicit
    if not bool(getattr(mission, "allow_heuristic_objective", True)):
        return ObjectiveSpec(objective_type="invalid_missing_objective_spec")
    objective = str(getattr(mission, "objective", "") or "")
    lower = objective.lower()

    alias = _first(r"\bmission-a[23]-(?:kimi|deepseek|flash)\b", objective)
    if alias and any(term in lower for term in ("alias", "map", "mapped", "reasoning model", "underlying model")):
        return ObjectiveSpec(
            objective_type="mapping_lookup",
            target=alias,
            required_outputs=["mapped_value"],
            evidence_shape="dictionary_mapping",
            required_evidence_shapes=["mapping_assignment"],
        )

    if "validatedreportv1" in lower or "validator module" in lower or re.search(r"\bfunction\s+[A-Za-z_][A-Za-z0-9_]*", objective):
        target = _first(r"\b[A-Za-z_][A-Za-z0-9_]*\b", objective) or "validate_report"
        function_name = _first(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)", objective)
        if function_name:
            target = function_name
        if "validate_report" in objective:
            target = "validate_report"
        return ObjectiveSpec(
            objective_type="function_location",
            target=target,
            required_outputs=["file_path", "function_definition"],
            evidence_shape="function_definition",
            required_evidence_shapes=["function_definition"],
        )

    key = _first(r"\bmission-a[23]-(?:kimi|deepseek|flash)\b", objective)
    if not key:
        key = _natural_model_alias(objective)
    symbol = _first(r"\b[A-Z][A-Z0-9_]{4,}\b", objective)
    fields = _fields_from_objective(objective)
    if (symbol or key) and ("profile" in lower or "config" in lower or "value" in lower or fields or "limits" in lower):
        return ObjectiveSpec(
            objective_type="config_value_extraction",
            target=key or symbol,
            required_outputs=["file_path"] + fields,
            evidence_shape="dictionary_entry",
            required_evidence_shapes=["dictionary_entry"],
            fields=fields,
        )

    quoted = _first(r'"([^"]+)"', objective) or _first(r"'([^']+)'", objective)
    if any(term in lower for term in ("zero match", "zero-match", "no occurrence", "not found")) and quoted:
        return ObjectiveSpec(
            objective_type="zero_match_evidence",
            target=quoted,
            required_outputs=["searched_paths", "zero_match_result"],
            evidence_shape="grep_zero_match",
            required_evidence_shapes=["grep_zero_match"],
        )

    return ObjectiveSpec(objective_type="generic_evidence")


def synthesize_objective_finding(mission: Any, ledger: Any) -> Optional[dict]:
    spec = classify_objective(mission)
    if spec.objective_type == "mapping_lookup":
        return _synthesize_mapping_lookup(spec, ledger)
    if spec.objective_type == "config_value_extraction":
        return _synthesize_config_value(spec, ledger)
    if spec.objective_type == "function_location":
        return _synthesize_function_location(spec, ledger)
    if spec.objective_type == "zero_match_evidence":
        return _synthesize_zero_match(spec, ledger)
    return None


def objective_coverage_errors(mission: Any, report: dict, ledger: Any) -> list[str]:
    if str(report.get("status", "")).upper() != "COMPLETE":
        return []
    spec = classify_objective(mission)
    if spec.objective_type == "invalid_missing_objective_spec":
        return ["objective_spec is required when heuristic objective inference is disabled"]
    if spec.objective_type == "generic_evidence":
        return []

    expected = synthesize_objective_finding(mission, ledger)
    if not expected:
        if spec.explicit:
            return [f"explicit objective_spec was not satisfied: {spec.objective_type}"]
        return []
    text = _report_text(report)
    errors = []
    for token in _required_tokens_from_finding(expected):
        if token and token not in text:
            errors.append(f"objective requires {token!r}, but COMPLETE report does not include it")
    if spec.explicit:
        for output in spec.required_outputs:
            if output in ("evidence_ref", "evidence_refs"):
                continue
            if _required_output_token(output, spec) and _required_output_token(output, spec) not in text:
                errors.append(f"objective_spec requires output {output!r}, but COMPLETE report does not include it")
    return errors


def _explicit_objective_spec(mission: Any) -> Optional[ObjectiveSpec]:
    raw = getattr(mission, "objective_spec", None)
    if not isinstance(raw, dict):
        return None
    objective_type = str(raw.get("objective_type", ""))
    target_obj = raw.get("target", {}) if isinstance(raw.get("target", {}), dict) else {}
    target = ""
    fields: list[str] = []
    if objective_type == "mapping_lookup":
        target = str(target_obj.get("key") or target_obj.get("symbol") or target_obj.get("name") or "")
    elif objective_type == "config_value_extraction":
        target = str(target_obj.get("key") or target_obj.get("symbol") or target_obj.get("name") or "")
        fields = [str(v) for v in raw.get("required_values", []) if str(v)]
    elif objective_type == "function_location":
        target = str(target_obj.get("symbol") or target_obj.get("function") or target_obj.get("name") or "")
    elif objective_type == "zero_match_evidence":
        target = str(target_obj.get("pattern") or target_obj.get("query") or "")
    else:
        target = str(target_obj.get("symbol") or target_obj.get("name") or target_obj.get("target") or "")
    required_outputs = [str(v) for v in raw.get("required_outputs", []) if str(v)]
    required_shapes = [str(v) for v in raw.get("required_evidence_shapes", []) if str(v)]
    return ObjectiveSpec(
        objective_type=objective_type,
        target=target,
        required_outputs=required_outputs,
        evidence_shape=required_shapes[0] if required_shapes else "",
        required_evidence_shapes=required_shapes,
        fields=fields,
        explicit=True,
        raw=dict(raw),
    )


def _synthesize_mapping_lookup(spec: ObjectiveSpec, ledger: Any) -> Optional[dict]:
    target = spec.target
    hit = _find_mapping_value(target, ledger)
    if not hit:
        return None
    model, evidence = hit
    path_hint = f" in {evidence.path}" if evidence.path else ""
    return {
        "claim": f"Runtime model alias {target} maps to underlying reasoning model {model}{path_hint}.",
        "evidence_refs": [evidence.ref],
        "confidence": "LOW",
    }


def _synthesize_config_value(spec: ObjectiveSpec, ledger: Any) -> Optional[dict]:
    target = spec.target
    definition_path = ""
    definition_ref = ""
    for evidence in _all_evidence(ledger):
        path = _definition_path_from_text(evidence.text)
        if path and ("PROFILES" in evidence.text or "CONFIG" in evidence.text or "SETTINGS" in evidence.text):
            definition_path = path
            definition_ref = evidence.ref
            break
    for evidence in _all_evidence(ledger):
        text = evidence.text
        if target and target not in text:
            continue
        if target and re.fullmatch(r"[A-Z][A-Z0-9_]{4,}", target):
            values = _extract_field_values(text, spec.fields)
        else:
            values = _extract_target_field_values(text, target, spec.fields) if target else _extract_field_values(text, spec.fields)
        if spec.fields and len(values) < len(spec.fields):
            continue
        if evidence.ref.startswith("command:") and definition_path:
            path = definition_path
        else:
            path = evidence.path or definition_path or _definition_path_from_text(text)
        field_text = ", ".join(f"{field}={values[field]}" for field in spec.fields if field in values)
        if not field_text:
            field_text = f"{target} appears in configuration evidence"
        if path:
            claim = f"Configuration values for {target} are defined in {path}; {field_text}."
        else:
            claim = f"Configuration values for {target} were found; {field_text}."
        refs = [definition_ref] if definition_ref else []
        if evidence.ref not in refs:
            refs.append(evidence.ref)
        return {"claim": claim, "evidence_refs": refs or [evidence.ref], "confidence": "LOW"}
    return None


def _synthesize_function_location(spec: ObjectiveSpec, ledger: Any) -> Optional[dict]:
    target = spec.target or "validate_report"
    candidates = [target]
    if target.lower() == "validatedreportv1":
        candidates.append("validate_report")
    for evidence in _all_evidence(ledger):
        for candidate in candidates:
            if _function_defined(candidate, evidence.text):
                path = evidence.path or "cited evidence"
                return {
                    "claim": f"{candidate} is defined in {path}, providing the requested validator/function location.",
                    "evidence_refs": [evidence.ref],
                    "confidence": "LOW",
                }
        if "ValidatedReportV1" in evidence.text and "validate_report" in evidence.text and evidence.path:
            return {
                "claim": f"The runtime has a ValidatedReportV1 validator module in {evidence.path}; it defines validate_report for strict report schema validation.",
                "evidence_refs": [evidence.ref],
                "confidence": "LOW",
            }
    return None


def _synthesize_zero_match(spec: ObjectiveSpec, ledger: Any) -> Optional[dict]:
    target = spec.target
    for idx, command in enumerate(getattr(ledger, "commands_run", []) or []):
        args = getattr(command, "args", {}) or {}
        if target and target != str(args.get("pattern", "")):
            continue
        if getattr(command, "exit_code", None) == 1 and getattr(command, "matches_count", -1) == 0:
            path = args.get("path", "(unknown path)")
            return {
                "claim": f"No matches for {target!r} were found in {path}.",
                "evidence_refs": [f"command:{idx}#zero_match"],
                "confidence": "LOW",
            }
    return None


def _find_mapping_value(target: str, ledger: Any) -> Optional[tuple[str, EvidenceHit]]:
    for evidence in _all_evidence(ledger):
        model = _extract_mapping_value(target, evidence.text)
        if model:
            return model, evidence
    return None


def _extract_mapping_value(target: str, text: str) -> Optional[str]:
    direct = re.search(rf'"{re.escape(target)}"\s*:\s*"([^"]+)"', text)
    if direct:
        return direct.group(1)
    single = re.search(rf"'{re.escape(target)}'\s*:\s*'([^']+)'", text)
    if single:
        return single.group(1)
    return None


def _extract_field_values(text: str, fields: list[str]) -> dict[str, str]:
    values = {}
    for field in fields:
        match = re.search(rf'["\']?{re.escape(field)}["\']?\s*:\s*([0-9]+|"[^"]+"|\'[^\']+\')', text)
        if match:
            values[field] = match.group(1).strip("\"'")
    return values


def _extract_target_field_values(text: str, target: str, fields: list[str]) -> dict[str, str]:
    values = {}
    if not target:
        return values
    double = re.search(rf'"{re.escape(target)}"\s*:\s*\{{(?P<body>.*?)\}}', text, re.DOTALL)
    single = re.search(rf"'{re.escape(target)}'\s*:\s*\{{(?P<body>.*?)\}}", text, re.DOTALL)
    match = double or single
    if not match:
        return values
    return _extract_field_values(match.group("body"), fields)


def _function_defined(name: str, text: str) -> bool:
    direct_definition = rf"(?m)^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\("
    grep_prefixed_definition = rf"(?m)^[^:\n]+:\d+:\s*(?:async\s+)?def\s+{re.escape(name)}\s*\("
    rtk_grep_definition = rf"(?m)^\s*\d+:\s*(?:async\s+)?def\s+{re.escape(name)}\s*\("
    return bool(
        re.search(direct_definition, text)
        or re.search(grep_prefixed_definition, text)
        or re.search(rtk_grep_definition, text)
    )


def _all_evidence(ledger: Any) -> list[EvidenceHit]:
    hits: list[EvidenceHit] = []
    for path, entry in getattr(ledger, "files_inspected", {}).items():
        extracts = getattr(entry, "extracts", []) or []
        if extracts:
            for extract in extracts:
                extract_id = str(extract.get("id", "extract:1")).replace("extract:", "")
                hits.append(EvidenceHit(ref=f"file:{path}#extract:{extract_id}", text=str(extract.get("text", "") or ""), path=path))
        cached = str(getattr(entry, "cached_text", "") or "")
        if cached:
            hits.append(EvidenceHit(ref=f"file:{path}#extract:1", text=cached, path=path))
    for idx, command in enumerate(getattr(ledger, "commands_run", []) or []):
        cached = str(getattr(command, "cached_text", "") or "")
        if cached:
            hits.append(EvidenceHit(ref=f"command:{idx}", text=cached, path=_path_from_command(command)))
    return hits


def _path_from_command(command: Any) -> str:
    args = getattr(command, "args", {}) or {}
    if "path" in args:
        return str(args["path"])
    return _definition_path_from_text(str(getattr(command, "cached_text", "") or ""))


def _definition_path_from_text(text: str) -> str:
    match = re.search(r"(?m)^([^:\n]+):\d+:", text)
    if match:
        return match.group(1)
    rtk_match = re.search(r"(?m)^\[file\]\s+(.+?)\s+\(\d+\):", text)
    return rtk_match.group(1) if rtk_match else ""


def _fields_from_objective(objective: str) -> list[str]:
    known = []
    for field in ("max_tool_budget", "max_time_seconds", "timeout_seconds", "max_jobs"):
        if field in objective:
            known.append(field)
    if re.search(r"\blimits\b", objective.lower()) and "max_tool_budget" not in known and "max_time_seconds" not in known:
        known.extend(["max_tool_budget", "max_time_seconds"])
    return known


def _natural_model_alias(objective: str) -> str:
    lower = objective.lower()
    tier = ""
    if re.search(r"\ba2\b", lower):
        tier = "a2"
    elif re.search(r"\ba3\b", lower):
        tier = "a3"
    model = ""
    for candidate in ("kimi", "deepseek", "flash"):
        if candidate in lower:
            model = candidate
            break
    if tier and model:
        return f"mission-{tier}-{model}"
    return ""


def _required_tokens_from_finding(finding: dict) -> list[str]:
    claim = str(finding.get("claim", "") or "")
    tokens = []
    tokens.extend(re.findall(r"\bmission-a[23]-(?:kimi|deepseek|flash)\b", claim))
    tokens.extend(re.findall(r"\bocg-[A-Za-z0-9_.-]+", claim))
    tokens.extend(re.findall(r"\b(?:max_tool_budget|max_time_seconds|timeout_seconds|max_jobs)=\S+", claim))
    if "validate_report" in claim:
        tokens.append("validate_report")
    return tokens


def _required_output_token(output: str, spec: ObjectiveSpec) -> str:
    if output in ("mapped_value", "mapped_model"):
        hit = _first(r"\bmission-a[23]-(?:kimi|deepseek|flash)\b", spec.target)
        return hit or spec.target
    if output in ("symbol_name", "function_definition"):
        return spec.target
    if output in ("file_path", "mapping_file", "searched_paths"):
        return ""
    if output in spec.fields:
        return output
    return ""


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


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    if not match:
        return ""
    if match.lastindex:
        return match.group(1)
    return match.group(0)
