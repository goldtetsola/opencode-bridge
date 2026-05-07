# Feature Specification: OSS Agent Runtime v1 Managed Investigation

**Created**: 2026-05-07
**Status**: Draft - implementation-ready for foundational contract modules and deterministic fixtures; not approved for live OSS autonomy rollout
**Input**: User brief proposing an OSS Agent Runtime with managed autonomy, then requesting `plan-spec`, `coverage-scan`, and GMFR review.

---

## GMFR Model

```json
{
  "deliverable": {
    "description": "Contract-first planning baseline for promoting opencode-bridge from a passive provider bridge into an OSS Agent Runtime v1 focused on read-only managed investigation.",
    "form": "plan"
  },
  "entities": [
    {
      "name": "GPTOrchestrator",
      "description": "Native GPT-5.5 or GPT-class parent agent that decomposes work, owns risk, and judges final acceptance.",
      "properties": ["native_provider", "final_judge", "critical_path_owner"]
    },
    {
      "name": "OSSAgentRuntime",
      "description": "Runtime layer inside the bridge and CLI that supervises OSS model autonomy through missions, policies, state, validation, and escalation.",
      "properties": ["mission_envelopes", "tool_policy", "evidence_ledger", "report_validator", "deadline_owner"]
    },
    {
      "name": "OSSReasoningEngine",
      "description": "OpenCode Go model used for bounded reasoning, such as Kimi, DeepSeek Pro, or Flash.",
      "properties": ["model_id", "latency_profile", "quality_profile", "rate_limit_profile"]
    },
    {
      "name": "Mission",
      "description": "Structured task envelope that grants a specific autonomy tier and constrains paths, tools, budgets, report schema, and escalation rules.",
      "properties": ["mission_id", "tier", "objective", "allowed_roots", "forbidden_roots", "tool_budget", "time_budget_seconds", "report_schema"]
    },
    {
      "name": "EvidenceLedger",
      "description": "Persistent record of inspected files, commands, duplicate suppressions, open questions, risk flags, and remaining budget.",
      "properties": ["files_inspected", "commands_run", "open_questions", "risk_flags", "duplicate_actions_blocked", "remaining_budget"]
    },
    {
      "name": "RuntimeTool",
      "description": "Runtime-owned tool exposed to OSS models rather than raw Codex tools.",
      "properties": ["name", "class", "schema", "path_policy", "side_effect_policy"]
    },
    {
      "name": "ValidatedReport",
      "description": "Runtime-accepted report that satisfies the mission schema or explicitly returns PARTIAL with missing fields.",
      "properties": ["status", "files_inspected", "commands_run", "findings", "uncertainties", "confidence", "caveats", "escalation_recommendation"]
    }
  ],
  "state_variables": [
    {
      "name": "runtimeTier",
      "type": "categorical",
      "initial": "A2 or A3 for v1",
      "description": "Maximum autonomy tier granted to the OSS model for the current mission."
    },
    {
      "name": "missionStatus",
      "type": "categorical",
      "initial": "active",
      "description": "One of active, complete, partial, escalate, failed."
    },
    {
      "name": "toolBudgetRemaining",
      "type": "quantitative",
      "initial": "mission.tool_budget",
      "description": "Remaining count of model-directed tool actions."
    },
    {
      "name": "writeAllowed",
      "type": "boolean",
      "initial": "false for v1 managed investigation",
      "description": "Whether the mission can mutate files."
    },
    {
      "name": "evidenceCoverage",
      "type": "categorical",
      "initial": "unknown",
      "description": "Whether inspected evidence covers requested paths, search terms, and required report claims."
    }
  ],
  "operations": [
    {
      "name": "classifyMission",
      "description": "Convert a structured handoff into a mission tier and mode.",
      "preconditions": ["handoff is present", "handoff is parseable or fails closed"],
      "effects": ["runtimeTier is assigned", "allowed tools and budgets are derived"],
      "inputs": ["MissionV1 JSON for A2/A3; labeled handoff text only for A0/A1 compatibility"],
      "outputs": ["Mission"]
    },
    {
      "name": "executePlanActObserveLoop",
      "description": "Let the model choose one legal action at a time, execute it, update ledgers, and continue until stop conditions are reached.",
      "preconditions": ["missionStatus is active", "toolBudgetRemaining is greater than zero", "requested action passes policy"],
      "effects": ["toolBudgetRemaining decreases", "EvidenceLedger is updated", "missionStatus may change"],
      "inputs": ["Mission", "compact ledger", "model action"],
      "outputs": ["observation or escalation"]
    },
    {
      "name": "suppressDuplicateAction",
      "description": "Return a synthetic observation instead of spending a tool step on duplicate complete reads.",
      "preconditions": ["action targets a file already inspected completely"],
      "effects": ["duplicate_actions_blocked increases", "model receives reminder to progress"],
      "inputs": ["RuntimeTool call", "EvidenceLedger"],
      "outputs": ["duplicate suppression observation"]
    },
    {
      "name": "validateFinalReport",
      "description": "Accept only reports that satisfy the required schema or explicit PARTIAL contract.",
      "preconditions": ["model returned a candidate final report"],
      "effects": ["missionStatus becomes complete, partial, escalate, or failed"],
      "inputs": ["candidate report", "Mission", "EvidenceLedger"],
      "outputs": ["ValidatedReport or repair feedback"]
    },
    {
      "name": "escalateCriticalPath",
      "description": "Stop or downgrade autonomy when the task or discovered evidence enters critical path areas.",
      "preconditions": ["critical path term or forbidden path is detected"],
      "effects": ["missionStatus becomes escalate", "writes remain denied"],
      "inputs": ["Mission", "path", "action", "evidence"],
      "outputs": ["escalation report"]
    }
  ],
  "constraints": [
    {
      "id": "C1",
      "statement": "OSS v1 managed investigation MUST be read-only.",
      "type": "invariant"
    },
    {
      "id": "C2",
      "statement": "Autonomy MUST be granted by mission tier, not by raw prompt or tool budget alone.",
      "type": "invariant"
    },
    {
      "id": "C3",
      "statement": "Status or intent text MUST NOT be accepted as a final report.",
      "type": "postcondition"
    },
    {
      "id": "C4",
      "statement": "Every stream=true response path MUST emit exactly one terminal Responses event plus [DONE].",
      "type": "postcondition"
    },
    {
      "id": "C5",
      "statement": "Every accepted report MUST include files inspected, commands run, findings, uncertainties, confidence, caveats, and escalation recommendation, or explicitly mark missing fields in PARTIAL.",
      "type": "postcondition"
    },
    {
      "id": "C6",
      "statement": "The runtime MUST block writes and final decisions on critical paths including auth, authorization, schema authority, persistence, recovery/finalizer, CI gates, deployments, migrations, secrets, and production data.",
      "type": "boundary"
    },
    {
      "id": "C7",
      "statement": "Doctor and health MUST make running process identity and supervisor durability inspectable.",
      "type": "postcondition"
    },
    {
      "id": "C8",
      "statement": "Fallback work MUST complete before the request deadline or return deterministic PARTIAL before the client times out.",
      "type": "boundary"
    }
  ],
  "initial_state": [
    "The repo already implements Responses-to-Chat translation in bridge.py.",
    "Existing code includes TaskSession, task-class budgets, context-pack reports, managed_autonomy mode, evidence ledger injection, duplicate read suppression, report validation, protocol conformance tests, doctor checks, and supervisor CLI surfaces.",
    "Existing implementation is not yet specified as a gated OSS Agent Runtime with falsifiable v1 acceptance gates."
  ],
  "goal": [
    "Define a v1 managed-investigation plan that is shippable before patch/implementation autonomy.",
    "Convert runtime strategy into traceable requirements, BDD scenarios, TDD slices, datasets, and coverage review.",
    "Separate v1 managed investigation from vFuture patch proposal, bounded implementation, and parallel worker pool capabilities."
  ],
  "assumptions": [
    "v1 scope is read-only A2/A3 managed investigation.",
    "Existing files bridge.py, codex_oss/cli.py, codex_oss/doctor.py, tests/test_protocol_conformance.py, tests/test_v9_transport.py, and tests/test_v10_autonomy.py are the main implementation and verification surfaces.",
    "GPT-5.5 remains the orchestrator and final judge.",
    "This spec is a planning artifact only; implementation changes are not included in this document.",
    "A3 managed investigation requires `OSS_HANDOFF_JSON` carrying MissionV1; prose fallback is not allowed for tool autonomy."
  ],
  "unknowns": [
    "Accepted report rate for live Kimi and DeepSeek A3 missions",
    "Optimal model routing for A2 versus A3",
    "Provider latency and rate-limit behavior under repeated tool turns",
    "Best context compaction thresholds for real repo investigations",
    "GPT cleanup burden after OSS reports",
    "False-confidence incidence in real repo investigations",
    "Whether live-model doctor smoke should ever be default"
  ],
  "placeholders": [],
  "requirement_trace": [
    {
      "requirement": "Build an OSS Agent Runtime, not just a bridge.",
      "represented_as": "goal",
      "ref": "goal[0]",
      "primary": true
    },
    {
      "requirement": "Use mission envelopes, tool budgets, evidence ledger, duplicate suppression, validation, and escalation.",
      "represented_as": "constraint",
      "ref": "C2",
      "primary": true
    },
    {
      "requirement": "Target managed autonomy A2/A3 before A4/A5/A6.",
      "represented_as": "constraint",
      "ref": "C1",
      "primary": true
    },
    {
      "requirement": "Use plan-spec, coverage-scan, and GMFR to find gaps.",
      "represented_as": "operation",
      "ref": "validateFinalReport",
      "primary": true
    }
  ],
  "validation_oracles": [
    {
      "id": "V1",
      "maps_to": ["C1", "C6"],
      "method": "measurement",
      "evidence_required": "Tests prove write tools and critical path writes are blocked in managed investigation.",
      "fail_condition": "A managed investigation can modify a file or claim write authority."
    },
    {
      "id": "V2",
      "maps_to": ["C2"],
      "method": "inspection",
      "evidence_required": "Mission/tier classification drives tools, budgets, and report schema.",
      "fail_condition": "A raw prompt can request broader tools without a mission tier."
    },
    {
      "id": "V3",
      "maps_to": ["C3", "C5"],
      "method": "measurement",
      "evidence_required": "Report validator rejects intent/status text and reports missing required fields.",
      "fail_condition": "Text such as 'I will inspect the files' is returned as completed."
    },
    {
      "id": "V4",
      "maps_to": ["C4", "C8"],
      "method": "measurement",
      "evidence_required": "Transport tests cover success, timeout, malformed upstream, early close, fallback, and deterministic partial.",
      "fail_condition": "stream=true closes without terminal event or request exceeds deadline."
    },
    {
      "id": "V5",
      "maps_to": ["C7"],
      "method": "measurement",
      "evidence_required": "Doctor detects stale source hash, unsupervised process, and unhealthy bridge.",
      "fail_condition": "Running process identity cannot be compared to source on disk."
    }
  ]
}
```

### GMFR Audit

```json
{
  "planning_audit_pass": true,
  "implementation_audit_pass": "conditional",
  "live_rollout_audit_pass": false,
  "status": "implementation_ready_after_loophole_fixes_v1_1_are_incorporated",
  "condition": "LoopholeFixesV1.1 sections and associated tests are incorporated before implementation agents modify runtime behavior.",
  "implementation_scope": [
    "MissionV1 parser and validator",
    "RuntimeContextV1 binding",
    "RuntimeToolV1 registry",
    "CommandExecutionPolicyV1",
    "PathPolicyV1",
    "CriticalPathRegistryV1 and CriticalPathReadPermissionV1",
    "EvidenceLedgerV1 and EvidenceRefResolutionPolicyV1",
    "RawObservationStoreV1",
    "ActionProtocolEnforcementV1",
    "ValidatedReportV1 validator",
    "DeadlinePolicyV1 and DeadlineInteractionRule",
    "FallbackPolicyV1",
    "StatePolicyV1 and ConcurrencyPolicyV1",
    "DoctorPolicyV1",
    "PrivacyPolicyV1",
    "LoopholeFixesV1.1",
    "deterministic A3 fixture tests"
  ],
  "live_rollout_blockers": [
    "Deterministic fixture suite must pass at 100%",
    "No unauthorized writes, secret reads, or critical-path write executions in fixtures",
    "Doctor/source/supervisor confidence gates must pass",
    "25-task live read-only burn-in must reach >=80% valid COMPLETE/PARTIAL reports",
    "Live burn-in must show 0 false-confidence incidents"
  ]
}
```

---

## Scope

### In Scope for v1

- A2 guided exploration and A3 managed investigation.
- Mission/tier classification from structured `OSS_HANDOFF_JSON` containing MissionV1.
- Read-only runtime-owned tools for search, read, list, and safe git inspection.
- Plan-act-observe loop with one model-directed action per turn.
- Persistent evidence ledger with duplicate suppression.
- Context compaction into compact ledger summaries.
- Strict final report schema validation.
- Critical path detection with read-only escalation.
- Doctor/health proof that the runtime is supervised, current, and safe to use.
- Protocol tests for terminal streaming and deterministic partials.
- Privacy, redaction, state retention, and baseline concurrent-mission isolation.

### Out of Scope for v1

- A4 patch proposal mode, except as a roadmap item.
- A5 bounded implementation mode, except as a roadmap item.
- A6 critical autonomous engineer mode.
- Network-capable OSS tools.
- Secrets, production DB, deployment, migrations, or live customer workspace access.
- Replacing GPT-5.5 as orchestrator or final judge.
- Prose-only A3 managed investigation. Labeled prose remains compatibility input for A0/A1 only.

### Scope Guardrails

- A3 managed investigation MUST require MissionV1 JSON.
- Missing tier plus any tool autonomy request MUST fail closed as `invalid_handoff`.
- Missing tier plus write intent MUST escalate.
- Missing tier plus no-tool exact output MAY be treated as A0.
- Missing tier plus explicit read-only paths and no model-directed exploration MAY be treated as A1 context-pack compatibility.
- v1 burn-in MUST NOT require A4 patch proposal, A5 bounded implementation, A6 critical engineering, or parallel worker pool behavior.

---

## Contract Schemas

These schemas are the implementation contract. Any implementation that cannot satisfy them should remain behind an explicit experimental flag.

### MissionV1

```json
{
  "schema_version": "oss_agent_mission.v1",
  "mission_id": "mission_20260507_001",
  "tier": "A3",
  "mode": "managed_investigation",
  "objective": "Investigate where portfolio certification state is recorded.",
  "risk_tier": "low|medium|critical",
  "write_allowed": false,
  "allowed_roots": ["docs/", "src/portfolio/"],
  "allowed_paths": [],
  "forbidden_roots": [".env", "secrets/", ".git/", "node_modules/", ".codex-oss/env/"],
  "forbidden_topics": ["production_data", "secrets", "deployments"],
  "critical_path_read_allowed": false,
  "critical_path_reason": null,
  "allow_broad_read_scope": false,
  "max_files_to_inspect": 20,
  "max_total_observation_bytes": 500000,
  "tool_budget": 20,
  "time_budget_seconds": 180,
  "allowed_tool_classes": ["read", "search", "list", "safe_git"],
  "stop_conditions": ["valid_report", "budget_exhausted", "critical_path_detected", "deadline_reached", "tool_failure"],
  "report_schema": "managed_investigation_report.v1",
  "required_outputs": ["files_inspected", "commands_run", "findings", "uncertainties", "confidence", "caveats", "escalation_recommendation"],
  "fallback_policy": "fallback_policy.v1",
  "deadline_policy": "deadline_policy.v1"
}
```

MissionV1 validation rules:

- `schema_version`, `mission_id`, `tier`, `mode`, `objective`, `write_allowed`, `allowed_roots`, `allowed_paths`, `forbidden_roots`, `tool_budget`, `time_budget_seconds`, `allowed_tool_classes`, `stop_conditions`, `report_schema`, and `required_outputs` are required.
- For v1, `tier` MUST be `A2` or `A3` for any model-directed tool autonomy.
- For v1, `mode` MUST be `guided_exploration` for A2 or `managed_investigation` for A3.
- For v1, `write_allowed` MUST be `false`.
- For v1, `allowed_tool_classes` MUST be a subset of `read`, `search`, `list`, and `safe_git`.
- Missing tier with tool autonomy MUST fail closed; the runtime MUST NOT infer A2/A3 from prose.
- `allowed_roots` grants directory-level permission. `allowed_paths` grants exact file-level permission.
- Critical reads require `critical_path_read_allowed=true`, `critical_path_reason`, and either exact `allowed_paths` or narrow critical `allowed_roots`.

### LoopholeFixesV1.1

This section closes the final contract precision gaps before runtime implementation begins. Implementation agents MUST treat these policies as part of v1, not later polish.

### EntryPointExtractionPolicyV1

A3 managed investigation is granted only by exactly one marked JSON block:

```text
<OSS_HANDOFF_JSON>
{
  "schema_version": "oss_agent_mission.v1",
  "...": "..."
}
</OSS_HANDOFF_JSON>
```

Rules:

- Exactly one `OSS_HANDOFF_JSON` block is allowed.
- Malformed JSON fails closed.
- Multiple blocks fail closed.
- JSON outside the marker is ignored for A3 authorization.
- A3 cannot be granted by markdown prose, XML-ish prose, tool budget language, or role instructions.

### A1ContextPackCompatibilityV1

A1 exists only for compatibility with current context-pack behavior.

- A1 accepts labeled prose with explicit `READ-ONLY PATHS`.
- A1 does not allow model-directed tools.
- The runtime gathers sources deterministically.
- The model, if used, receives no tools and only synthesizes a report.
- A1 cannot inspect critical paths unless explicit critical permission is present.
- A1 uses a smaller context budget than A3 and cannot return HIGH confidence unless evidence coverage is complete.

### UpstreamModelCallPolicyV1

For A2/A3 v1, upstream OSS model calls MUST use `tools=[]` or omit the tool field entirely.

- Provider-native function/tool calling is disabled for v1 managed investigation.
- The model expresses actions only through ManagedInvestigationActionV1 JSON.
- Runtime-owned tools are executed only by the bridge runtime after JSON action validation.

### AllowedPathsPolicyV1

- `allowed_roots` is for directory scopes.
- `allowed_paths` is for exact file scopes.
- Exact paths always win over broad roots for critical-read authorization.
- Critical reads SHOULD use exact `allowed_paths` whenever possible.
- Broad roots do not count as explicit critical permission.

### BroadScopePolicyV1

Broad roots include `.`, project root, `src/`, `scripts/`, `docs/`, and any root that would include more than `max_files_to_inspect` candidate files.

Broad roots are allowed only when:

- `allow_broad_read_scope=true`.
- `risk_tier` is `low` or `medium`.
- `critical_path_read_allowed=false` unless critical paths are explicitly listed.
- `max_files_to_inspect` and `max_total_observation_bytes` are enforced.
- Final report confidence cannot be HIGH unless evidence coverage is demonstrated.

If broad scope is requested without `allow_broad_read_scope=true`, the runtime fails closed or returns ESCALATE asking GPT-5.5 to narrow the mission.

### DefaultDenyPolicyV1

The runtime always applies default forbidden roots and secret patterns regardless of MissionV1.

Mission `forbidden_roots` may add restrictions but may not remove runtime defaults.

Default deny roots and patterns:

- `.env`, `*.env`
- `secrets/`
- `.git/`
- `node_modules/`
- `.codex-oss/env/`
- `.codex-oss/state/`
- `.codex-oss/logs/`
- private keys and credential files
- database dumps

This policy is runtime-enforced before tool execution and before model exposure.

### RiskTierPolicyV1

- `low`: A2/A3 read-only allowed within path policy.
- `medium`: A3 read-only allowed; confidence cannot be HIGH without strong evidence coverage.
- `critical`: no autonomous exploration unless `critical_path_read_allowed=true` and explicit critical `allowed_paths` or narrow critical `allowed_roots` are present; `write_allowed` must be false; final report must be ESCALATE or include GPT review required.

### EffectiveBudgetPolicyV1

Hard caps:

- `A2_MAX_TOOL_BUDGET=10`
- `A3_MAX_TOOL_BUDGET=20`
- `A2_MAX_TIME_SECONDS=90`
- `A3_MAX_TIME_SECONDS=180`
- `ESTIMATED_A3_TURN_SECONDS=8`

Runtime computes:

```text
budget_allowed_by_request_deadline =
  floor((effective_deadline_seconds - DETERMINISTIC_PARTIAL_RESERVE_SECONDS) / ESTIMATED_A3_TURN_SECONDS)

effective_tool_budget = min(
  mission.tool_budget,
  tier_max_tool_budget,
  budget_allowed_by_request_deadline
)
```

If mission budget exceeds cap, the runtime clamps reasonable values and records the clamp in the ledger. Clearly abusive values, such as `tool_budget=1000` or `time_budget_seconds=3600`, fail closed.

ValidatedReportV1 MUST include budget provenance when clamping occurs:

```json
{
  "tool_budget_requested": 20,
  "tool_budget_effective": 10,
  "budget_reduction_reason": "request_deadline"
}
```

### RuntimeContextV1

Mission execution is bound to runtime context that is not supplied by the OSS model:

```json
{
  "project_root": "/Users/example/project",
  "project_id": "opencode-bridge",
  "worktree_id": "sha256:...",
  "git_head_at_start": "abc123",
  "git_dirty_status_at_start": "clean|dirty",
  "request_deadline_seconds": 90
}
```

The runtime MUST attach this context to the mission ledger before the first model call. This prevents long investigations from losing track of the worktree and request boundary they were authorized to inspect.

### A2/A3 Execution Boundary

For v1, A2/A3 managed investigation runs inside the OSS Agent Runtime, not through Codex-exposed tool continuation.

- Codex sends a mission request to the bridge.
- The runtime performs the plan-act-observe loop internally using RuntimeToolV1.
- The runtime executes tools, updates EvidenceLedgerV1, validates reports, and applies policy gates itself.
- The runtime returns one terminal Responses result to Codex: `COMPLETE`, `PARTIAL`, `ESCALATE`, or `FAILED`.
- Raw Codex tools are not exposed to OSS models in A2/A3.
- Provider-native tool calling MAY be explored later, but v1 correctness MUST NOT depend on Codex continuation tool loops.

### RuntimeToolV1

```json
{
  "name": "rtk_read",
  "class": "read",
  "description": "Read a project-relative file path.",
  "schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "Project-relative path to read."}
    },
    "required": ["path"],
    "additionalProperties": false
  },
  "side_effect_policy": "none",
  "path_policy": "path_policy.v1"
}
```

Allowed RuntimeToolV1 tools for v1:

| Tool | Class | Required args | Notes |
|---|---|---|---|
| `rtk_read` | `read` | `path` | Reads one allowed project file. |
| `rtk_grep` | `search` | `pattern`, `path` | Searches within allowed roots; zero matches are valid evidence. |
| `rtk_ls` | `list` | `path` | Lists allowed project paths. |
| `rtk_git_status` | `safe_git` | none | Equivalent to `rtk git status --short`. |
| `rtk_git_log` | `safe_git` | `limit` | Equivalent to bounded `rtk git log --oneline -n N`; `N` max 20. |
| `rtk_git_show_stat` | `safe_git` | `rev` | Stat-only; no blob or patch content. |
| `rtk_git_diff_stat` | `safe_git` | optional `path` | Stat-only; no patch content. |

Forbidden tools and actions for v1:

- `apply_patch`, write-file tools, edit tools, raw shell, network calls, deployments, database commands, secret/env reads, `git add`, `git commit`, `git checkout`, `git reset`, `git clean`, `git stash`, `git push`, `git pull`, `git fetch`, `git merge`, and `git rebase`.
- The runtime MUST NOT expose raw Codex shell tools to A2/A3 missions.

### CommandExecutionPolicyV1

Runtime-owned RTK tools are implemented as subprocesses under a strict execution policy:

```json
{
  "tool": "rtk_read",
  "argv": ["rtk", "read", "docs/CONTINUITY.md"],
  "cwd": "project_root",
  "timeout_seconds": 20,
  "stdout_max_bytes": 200000,
  "stderr_max_bytes": 20000,
  "shell": false
}
```

Rules:

- `shell=false` only.
- Executable paths are resolved at startup.
- Commands are argv arrays, never interpolated command strings.
- `cwd` must be `project_root`.
- Every tool has a timeout and stdout/stderr byte caps.
- Environment is minimal and redacted; no secret-bearing environment is inherited unless required for bridge/model access.
- Pipes, redirection, command chaining, shell metacharacters, and raw shell snippets are not supported.
- Command results are scanned/redacted before any observation is sent to an OSS model.

### PathPolicyV1

Path resolution rules:

- All OSS paths must normalize to project-relative paths.
- Absolute paths are accepted only when their resolved realpath remains under `project_root`; they are then converted to project-relative paths.
- `..` escapes, URL-encoded escapes, symlinks resolving outside allowed roots, and direct reads of `.git` internals are blocked.
- Secret paths are blocked even if they appear under allowed roots.
- Runtime state/log/env directories are blocked unless explicitly allowed for a read-only diagnostic mission.
- `.git` access is allowed only through safe runtime-owned git tools.
- Case-insensitive filesystem comparisons must compare resolved canonical paths, not raw strings.

Path examples:

| Input | Expected |
|---|---|
| `docs/CONTINUITY.md` | Allow if under allowed roots. |
| `/Users/.../opencode-bridge/docs/CONTINUITY.md` | Normalize to `docs/CONTINUITY.md` if under project root and allowed. |
| `../secrets.env` | Block. |
| `.env` | Block. |
| `docs/link_to_env` | Block if symlink resolves to forbidden path. |
| `.git/config` | Block direct read. |
| `.codex-oss/env/opencode-go.env` | Block. |

### EvidenceLedgerV1

```json
{
  "mission_id": "mission_20260507_001",
  "runtime_context": {
    "project_id": "opencode-bridge",
    "worktree_id": "sha256:...",
    "git_head_at_start": "abc123",
    "git_dirty_status_at_start": "dirty"
  },
  "files_inspected": {
    "docs/CONTINUITY.md": {
      "path": "docs/CONTINUITY.md",
      "complete": true,
      "chars_total": 12642,
      "chars_returned": 12642,
      "sha256": "sha256:...",
      "tool": "rtk_read",
      "turn": 3,
      "risk_flags": [],
      "extracts": [
        {"id": "extract:1", "kind": "line_range", "summary": "Mentions stale cursor warning."}
      ]
    }
  },
  "commands_run": [
    {
      "tool": "rtk_grep",
      "args": {"pattern": "finalizer", "path": "docs/"},
      "exit_code": 0,
      "stdout_sha256": "sha256:...",
      "matches_count": 8,
      "turn": 2
    }
  ],
  "claims": [
    {
      "claim": "Finalizer docs mention stale cursor.",
      "evidence_refs": ["file:docs/CONTINUITY.md#extract:1"],
      "confidence": "MEDIUM"
    }
  ],
  "open_questions": [],
  "risk_flags": [],
  "duplicate_actions_blocked": 0,
  "tool_budget_remaining": 12
}
```

Ledger rules:

- Duplicate suppression may fire only when `complete=true` for the target path.
- Incomplete/truncated reads may be reread with a narrower range or search action.
- EvidenceLedger metadata is persisted by default.
- Raw observations may be stored only in RawObservationStoreV1.
- Claims in final reports must reference ledger evidence.

### RawObservationStoreV1

Raw observations MAY be stored only in local state, never in logs, health, or doctor output.

- Raw observation storage is local-only, gitignored, TTL-limited, purgeable, and treated as sensitive.
- `RAW_OBSERVATION_TTL_SECONDS=86400`.
- `MAX_RAW_OBSERVATION_BYTES=5000000` per mission unless configured lower.
- `STORE_RAW_OBSERVATIONS=false` by default.
- `STORE_RAW_OBSERVATIONS=true` is allowed for local developer workflows, but the runtime must print/document that state may contain repo content.
- `codex-oss purge-state` and `codex-oss purge-logs` must remove state/log artifacts.
- Model turns receive redacted observations and compact ledger summaries, not raw state dumps.

### ManagedInvestigationActionV1

The model must return exactly one structured action per turn:

```json
{
  "action_type": "tool_call",
  "tool_name": "rtk_grep",
  "arguments": {"pattern": "finalizer", "path": "docs/"},
  "reason": "Find documentation references to finalizer state."
}
```

### ActionProtocolEnforcementV1

For v1, the model-facing loop MUST request exactly one JSON object matching ManagedInvestigationActionV1.

- The runtime parses assistant text as JSON.
- Markdown fences, prose, status text, multiple JSON objects, arrays of actions, or provider-native multi-tool messages are invalid.
- On invalid output, the runtime sends exactly one repair prompt:

```text
Your previous response was invalid because: <reason>.
Return exactly one ManagedInvestigationActionV1 JSON object.
Do not include markdown.
Do not include status text.
Do not include more than one action.
```

- If repair fails, the runtime returns PARTIAL or FAILED with the invalid-output reason.
- Provider-native function/tool calling MAY be tested later, but v1 correctness MUST use JSON-only action parsing.

Terminal action:

```json
{
  "action_type": "final_report",
  "report": {
    "oss_report_version": "1.0",
    "mission_id": "mission_20260507_001",
    "status": "COMPLETE",
    "confidence": "MEDIUM",
    "files_inspected": [{"path": "docs/CONTINUITY.md", "complete": true}],
    "commands_run": [{"tool": "rtk_grep", "args": {"pattern": "finalizer", "path": "docs/"}}],
    "findings": [
      {
        "claim": "Recovery docs mention stale finalizer state.",
        "evidence_refs": ["file:docs/CONTINUITY.md#extract:1"],
        "confidence": "MEDIUM"
      }
    ],
    "uncertainties": [],
    "caveats": [],
    "escalation_recommendation": "GPT-5.5 review required because recovery/finalizer is a critical path.",
    "missing_fields": []
  }
}
```

Repair rules:

- Plain status text is invalid.
- Multiple tool calls in one response are invalid; the runtime executes none, sends one repair prompt asking for exactly one action, then returns PARTIAL or FAILED on repeat violation.
- Unstructured markdown mid-loop is invalid.
- Tool calls outside RuntimeToolV1 are blocked or escalated before execution.

### ValidatedReportV1

Required fields:

- `oss_report_version`
- `mission_id`
- `status`: `COMPLETE`, `PARTIAL`, `ESCALATE`, or `FAILED`
- `confidence`: `HIGH`, `MEDIUM`, or `LOW`
- `files_inspected`
- `commands_run`
- `findings`
- `uncertainties`
- `caveats`
- `escalation_recommendation`
- `missing_fields`

Validation rules:

- Every finding MUST include at least one `evidence_refs` entry.
- Every `evidence_refs` entry MUST resolve to an EvidenceLedgerV1 file extract, command result, or explicit zero-match search record.
- `files_inspected` and `commands_run` in the report MUST match ledger entries for the same mission.
- `HIGH` confidence is forbidden when evidence coverage is partial, any required field is missing, or any critical path risk flag is present.
- Critical-path reports MUST include an escalation recommendation.
- Critical finality claims such as "safe to merge", "correct", "no risk", "valid", "ready", "complete proof", and "certified" are rejected around critical paths unless framed as evidence requiring GPT review.
- Markdown may be rendered from ValidatedReportV1, but validation operates on structured JSON.

### EvidenceRefResolutionPolicyV1

Evidence references are canonical strings:

- `file:<path>#extract:<id>`
- `command:<turn>`
- `command:<turn>#match:<n>`
- `command:<turn>#zero_match`

Resolution rules:

- File refs must point to `files_inspected[path].extracts[id]`.
- Command refs must point to an entry in `commands_run` with matching `turn`.
- Zero-match findings are valid only when the referenced grep command has `exit_code=1` and `matches_count=0`.
- Reports referencing nonexistent evidence ids are rejected.
- A finding may cite redacted evidence; the report should note `redactions_applied=true` when relevant.

### CriticalPathRegistryV1

```json
{
  "critical_term_patterns": ["\\bauth(orization)?\\b", "\\bschema\\b", "\\bmigration(s)?\\b", "\\brecovery\\b", "\\bfinalizer\\b", "\\bcertification\\b", "\\bci\\b", "\\bdeploy(ment)?\\b", "\\bpersistence\\b", "\\bdatabase\\b", "\\bdb\\b", "\\bpostgres\\b", "\\bsqlite\\b", "\\bsecrets?\\b"],
  "critical_path_roots": ["src/auth/", "src/schema/", "migrations/", ".github/workflows/", "scripts/recovery/", "scripts/finalizer/"],
  "project_critical_roots": [],
  "project_critical_terms": [],
  "policy": {
    "read_explicitly_allowed": true,
    "write_allowed": false,
    "final_decision_allowed": false,
    "must_mark_gpt_review_required": true
  }
}
```

Critical matching rules:

- Term matching is case-insensitive unless a project override says otherwise.
- Project critical roots and terms are loaded from config and merged with defaults.
- Raw substring matching such as `ci` inside ordinary words is forbidden; use word-boundary regexes.

### CriticalPathReadPermissionV1

Critical-path reads require explicit permission beyond broad allowed roots:

- MissionV1 must include `critical_path_read_allowed=true`.
- The critical path must be listed directly in `allowed_roots` or `allowed_paths`, not merely covered by a broad parent such as `src/`.
- MissionV1 must include `critical_path_reason`.
- For critical reads, `allowed_roots` must be a narrow critical root, not `.`, project root, `src/`, `scripts/`, or another broad parent.
- The ledger must add a GPT-review-required risk flag.
- The final report cannot make final safety, merge, schema, recovery, auth, persistence, CI, deployment, or certification decisions.

### ContextCompactionPolicyV1

- Small file `<= 12000` chars: include full content if allowed.
- Medium file `<= 40000` chars: include first 8000 chars, last 4000 chars, and keyword extracts.
- Large file `> 40000` chars: include metadata and keyword extracts only unless explicitly requested.
- Search result with more than 50 matches: include first 50, total count, and suggest narrower search.
- Zero-match search: record as valid evidence, not failure.
- Compact ledger sent to model: max 6000 chars per turn.
- Global context-pack budget: max 24000 chars unless configured lower.

### DeadlinePolicyV1

Defaults:

- `REQUEST_DEADLINE_SECONDS=90`
- `MODEL_FIRST_BYTE_TIMEOUT_SECONDS=20`
- `MODEL_SEMANTIC_IDLE_TIMEOUT_SECONDS=30`
- `FALLBACK_BUDGET_SECONDS=20`
- `DETERMINISTIC_PARTIAL_RESERVE_SECONDS=5`
- `MAX_REPAIR_ATTEMPTS=1`

Invariant: the runtime must reserve enough time to emit deterministic PARTIAL before client timeout.

### DeadlineInteractionRule

Mission time and request time are separate:

- `mission_time_budget_seconds` is the logical cap on mission work.
- `request_deadline_seconds` is the client-visible transport deadline.
- `effective_deadline_seconds = min(mission.time_budget_seconds, request_deadline_seconds)`.
- `deterministic_partial_deadline = effective_deadline_seconds - DETERMINISTIC_PARTIAL_RESERVE_SECONDS`.
- The runtime MUST stop model/tool/fallback work no later than `deterministic_partial_deadline`.
- The runtime MUST never continue model or tool work past the point where it can still emit a deterministic terminal response before the client-visible deadline.

### FallbackPolicyV1

```json
{
  "max_model_attempts_per_turn": 2,
  "max_total_model_calls_per_mission": 25,
  "max_repair_attempts_per_turn": 1,
  "max_fallback_attempts_per_turn": 1,
  "retry_same_model": false,
  "fallback_models": ["ocg-deepseek-v4-flash"],
  "deterministic_partial_on_timeout": true,
  "cancel_on_client_disconnect": true,
  "report_fallback_provenance": true
}
```

The model-attempt limits are mission-wide cost controls. They do not authorize extra tool actions beyond the effective tool budget.

### ResponseStatusMappingV1

Prefer `response.completed` with a structured ValidatedReportV1 for recoverable mission outcomes.

| Mission outcome | Responses transport status |
|---|---|
| `COMPLETE` | `response.completed` with ValidatedReportV1 status `COMPLETE` |
| `PARTIAL` | `response.completed` with ValidatedReportV1 status `PARTIAL` |
| `ESCALATE` | `response.completed` with ValidatedReportV1 status `ESCALATE` |
| Recoverable runtime/tool/model policy failure | `response.completed` with ValidatedReportV1 status `FAILED` |
| Queue timeout | `response.completed` with ValidatedReportV1 status `FAILED`, reason `capacity_timeout` |
| Deadline reached with deterministic report | `response.completed` with ValidatedReportV1 status `PARTIAL` |
| Upstream malformed but runtime can report | `response.completed` with ValidatedReportV1 status `PARTIAL` or `FAILED` |
| Protocol/config/auth/routing violation | `response.failed` |

Use `response.failed` only for true provider, protocol, auth, config, or routing failures where the runtime cannot produce a trustworthy mission report.

### StatePolicyV1

- `STATE_SCHEMA_VERSION=1`
- `MISSION_TTL_SECONDS=86400`
- `RESPONSE_TTL_SECONDS=86400`
- Startup migrations must be idempotent.
- Doctor verifies state DB is writable and schema-current.
- If state is recoverable after restart, continue.
- If state is missing or corrupt, return a completed recovery report explaining state loss rather than a broken stream.
- On client disconnect, cancel the current model call if possible, stop the internal action loop, mark the mission interrupted, preserve diagnostic state, and do not continue spending provider budget.
- v1 does not support background missions.
- v1 must isolate concurrent missions by `mission_id`.

### ConcurrencyPolicyV1

v1 serializes A3 missions by default:

- `GLOBAL_MAX_ACTIVE_A3_MISSIONS=1`
- `A3_QUEUE_MAX=2`
- `A3_QUEUE_TIMEOUT_SECONDS=5`
- If a mission cannot start within the queue timeout, the runtime returns terminal `FAILED` with capacity reason.
- Queued missions do not share ledgers, model state, request deadlines, or raw observations.
- Future parallel worker-pool behavior must pass vFuture Gate 7 before normal use.

### DoctorPolicyV1

Doctor modes:

| Mode | Checks |
|---|---|
| `--offline` | Config, files, TOMLs, source hash, supervisor metadata, rules, state DB path. |
| default | Offline checks plus bridge health. |
| `--network` | Default checks plus OpenCode model catalog/API key validation. |
| `--live-model` | Network checks plus one tiny inference smoke test. |

Default doctor MUST NOT burn model usage. Live inference is opt-in.

Supervisor classification:

- `foreground`: accepted for development; doctor status WARN for normal use if not otherwise durable.
- `service`, `container`, `external_verified`: durable; doctor status PASS when health/source checks pass.
- `unknown` or `started_by_codex_shell=true`: doctor status FAIL for normal OSS agent use.

### PrivacyPolicyV1

- No API keys or bearer tokens in logs, health, doctor output, reports, or deterministic partials.
- Logs store metadata and hashes by default, not raw file contents.
- Tool observations MUST pass through secret scanning/redaction before being sent to OSS models.
- Reports redact secret-like strings before returning to GPT.
- State DB and logs must be gitignored.
- User must have a purge path for state/logs, such as `codex-oss purge-state`.
- Secret patterns include bearer tokens, `sk-` style keys, database URLs, `AUTH_TOKEN`, `OPENCODE_GO_API_KEY`, `ANTHROPIC_API_KEY`, and `OPENAI_API_KEY`.

### SecretDetectionSeverityPolicyV1

- Low-risk secret-like pattern in an allowed file: send redacted observation, set `redactions_applied=true`, and add a caveat.
- High-confidence credential material: stop mission and return ESCALATE with no raw value exposed.
- Secret material in a path that should have been blocked: return FAILED or ESCALATE and mark a path-policy defect.

### HumanReadableReportRenderingV1

ValidatedReportV1 JSON is canonical. Markdown rendering is optional and must be generated from validated JSON:

```text
OSS_REPORT_BEGIN
Status: <status>
Confidence: <confidence>
Findings:
- <claim> [evidence_refs: ...]
Evidence:
- <files and commands>
Uncertainties:
- <uncertainty>
Caveats:
- <caveat>
Escalation:
<escalation_recommendation>
OSS_REPORT_END
```

---

## User Stories & Acceptance Criteria

### User Story 1 - Supervised Runtime Identity (Priority: P0)

A Codex user wants the bridge to run under a known supervisor with truthful process identity so they can trust that OSS missions are using the intended source, config, and durable process rather than a stale shell child.

**Why this priority**: If the runtime is not durable or identifiable, failures are misdiagnosed as model behavior and every other gate becomes unreliable.

**Independent Test**: Start the bridge through a supported supervisor backend, run `codex-oss doctor`, alter the source on disk, and confirm doctor detects the running-source mismatch before restart.

**Acceptance Scenarios**:

1. **Given** the bridge is started with a supported supervisor backend, **When** health is requested, **Then** health reports pid, ppid, started_at, uptime, source path/hash, config fingerprint, and supervisor durability.
2. **Given** the running bridge source hash differs from the file on disk, **When** doctor runs, **Then** doctor reports a stale running source mismatch with restart guidance.
3. **Given** the bridge is started by a Codex shell child or unknown supervisor, **When** doctor runs in normal mode, **Then** doctor fails and explains how to start a supported supervisor.
4. **Given** the foreground supervisor is running, **When** doctor runs in development mode, **Then** doctor warns but allows development use.

### User Story 2 - Mission Envelopes and Autonomy Tiers (Priority: P0)

A GPT orchestrator wants to delegate OSS work through structured missions so autonomy is granted by tier, tool class, path scope, and report contract rather than by ambiguous prose.

**Why this priority**: Mission envelopes are the boundary between managed autonomy and raw model improvisation.

**Independent Test**: Feed structured MissionV1 handoffs for A2/A3 and invalid handoffs for missing tier, A4, A5, and A6; verify v1 accepts only explicit read-only A2/A3 missions and fails closed or escalates everything else.

**Acceptance Scenarios**:

1. **Given** a valid A3 read-only mission, **When** the runtime classifies it, **Then** it grants only investigation tools, a finite budget, and the investigation report schema.
2. **Given** malformed `OSS_HANDOFF_JSON`, **When** the runtime classifies it, **Then** it fails closed as `invalid_handoff`.
3. **Given** a tool-using handoff has no tier, **When** the runtime classifies it, **Then** it fails closed as `invalid_handoff`.
4. **Given** an A4/A5/A6 mission in v1, **When** the runtime classifies it, **Then** it returns `escalate` or an explicitly unsupported mode.
5. **Given** a request contains multiple handoff blocks or unmarked JSON, **When** the runtime extracts the entrypoint, **Then** A3 authorization fails closed.
6. **Given** a mission requests a broad root without `allow_broad_read_scope=true`, **When** the runtime validates scope, **Then** it fails closed or escalates for narrowing.

### User Story 3 - Read-Only Plan-Act-Observe Loop (Priority: P0)

An OSS reasoning engine wants to inspect relevant files and follow evidence within a budget so it can perform useful investigation while the runtime owns legality, progress, and stopping.

**Why this priority**: This is the core capability that turns the bridge into an autonomy layer.

**Independent Test**: Run a fixture mission where the model searches for a symbol, reads one result, attempts a duplicate read, and returns a report; verify ledger updates, duplicate suppression, and budget accounting.

**Acceptance Scenarios**:

1. **Given** an active A3 mission with budget remaining, **When** the model requests an allowed search, **Then** the runtime executes it internally with RuntimeToolV1, records the command, and returns an observation inside the runtime loop.
2. **Given** the model requests a duplicate complete read, **When** the runtime evaluates the action, **Then** it suppresses the duplicate and returns a progress reminder.
3. **Given** the model requests a write during read-only investigation, **When** policy checks the action, **Then** the runtime blocks it and returns escalation.
4. **Given** the tool budget is exhausted, **When** the mission has no valid final report, **Then** the runtime returns PARTIAL with evidence gathered and missing fields.
5. **Given** the model emits multiple actions in one turn, **When** the runtime evaluates the response, **Then** it executes none of them and issues one repair attempt before returning PARTIAL or FAILED.
6. **Given** a runtime tool observation contains a secret-like string, **When** the runtime prepares the observation for the OSS model, **Then** it redacts the secret before model exposure and records that redaction occurred.
7. **Given** requested tool budget exceeds the effective deadline budget, **When** the runtime starts the mission, **Then** it clamps the tool budget and records the reduction reason.

### User Story 4 - Strict Report Validation (Priority: P0)

A GPT orchestrator wants OSS reports to be schema-validated and uncertainty-explicit so it can treat them as evidence instead of unsupported truth.

**Why this priority**: The biggest risk is false confidence from fluent but incomplete reports.

**Independent Test**: Validate reports that are complete, missing fields, status text, partial with missing fields, and critical-path escalation.

**Acceptance Scenarios**:

1. **Given** a report containing all required fields, evidence coverage, and evidence refs that resolve to the mission ledger, **When** validation runs, **Then** the report is accepted as COMPLETE.
2. **Given** a report saying only "I will inspect the files now", **When** validation runs, **Then** the runtime rejects it as status/intent text.
3. **Given** a PARTIAL report that names missing fields and inspected evidence, **When** validation runs, **Then** the runtime accepts it as PARTIAL.
4. **Given** a confident report without evidence coverage, **When** validation runs, **Then** the runtime rejects confident PASS.
5. **Given** a finding has no evidence references, **When** validation runs, **Then** the runtime rejects the report.
6. **Given** a finding references a nonexistent ledger evidence id, **When** validation runs, **Then** the runtime rejects the report.

### User Story 5 - Terminal-Safe Transport and Deadlines (Priority: P0)

A Codex client wants every Responses request to end deterministically so OSS runtime failures do not appear as hangs, broken streams, or false successes.

**Why this priority**: Transport correctness is a prerequisite for reliable agent behavior.

**Independent Test**: Use fake upstream cases for success, stall, early close, malformed JSON, timeout, status text, and deterministic partial; assert terminal event or JSON status in every case.

**Acceptance Scenarios**:

1. **Given** `stream=true`, **When** upstream succeeds, **Then** the bridge emits `response.created`, output events, exactly one terminal Responses event, and `[DONE]`.
2. **Given** `stream=true`, **When** upstream stalls or closes early, **Then** the bridge emits `response.incomplete` or `response.failed` and `[DONE]`.
3. **Given** fallback cannot finish before deadline, **When** the request deadline approaches, **Then** the runtime returns deterministic PARTIAL before client timeout.
4. **Given** mission time budget exceeds request deadline, **When** the runtime calculates deadlines, **Then** it uses the smaller effective deadline and reserves deterministic partial time.
5. **Given** a recoverable mission policy failure occurs, **When** the runtime returns to Codex, **Then** it uses `response.completed` with ValidatedReportV1 status `FAILED` instead of transport failure.

### User Story 6 - Critical Path Escalation (Priority: P0)

A GPT orchestrator wants OSS workers to help inspect high-risk areas without becoming final authority or mutating critical paths.

**Why this priority**: Recovery, schema, auth, persistence, and deployment mistakes can cause data loss, security failures, or broken release gates.

**Independent Test**: Ask an A3 mission to inspect recovery docs read-only and then ask it to edit recovery code; verify inspection can continue but writes are blocked and final decision escalates.

**Acceptance Scenarios**:

1. **Given** a read-only mission explicitly permits recovery docs with `critical_path_read_allowed=true` and a critical path reason, **When** the model reads those docs, **Then** the runtime allows the read and marks GPT review required.
2. **Given** any OSS mission requests a critical-path write, **When** policy checks the action, **Then** the runtime blocks the write and escalates.
3. **Given** a mission discovers critical path evidence, **When** the final report is produced, **Then** it includes an escalation recommendation.

### User Story 7 - Doctor as Confidence Gate (Priority: P1)

A user wants one command to determine whether OSS agents are safe to use so setup mistakes do not masquerade as model or runtime failures.

**Why this priority**: The runtime will be used across projects; setup failures need precise, actionable diagnosis.

**Independent Test**: Intentionally break each setup dimension and verify `codex-oss doctor` reports the exact failing invariant and remediation.

**Acceptance Scenarios**:

1. **Given** the parent provider is accidentally set to `opencode_bridge`, **When** doctor runs, **Then** it fails with instructions to keep GPT native.
2. **Given** agent TOMLs are missing provider or model config, **When** doctor runs, **Then** it reports the exact agent and field.
3. **Given** the health endpoint lacks source fingerprint or state DB persistence, **When** doctor runs, **Then** it warns that runtime confidence is incomplete.
4. **Given** the user runs default doctor, **When** doctor completes, **Then** it has not performed live model inference unless `--live-model` was explicitly requested.

### User Story 8 - vFuture Roadmap Gates (Priority: P2)

A maintainer wants A4/A5/parallel autonomy represented as future gates so v1 does not sprawl while later work remains traceable.

**Why this priority**: The larger strategy matters, but v1 should not silently absorb patching, implementation, and concurrency blast radius.

**Independent Test**: Confirm roadmap gates exist with clear promotion criteria and no v1 acceptance scenario requires write autonomy.

**Acceptance Scenarios**:

1. **Given** v1 is complete, **When** A4 is considered, **Then** promotion requires patch proposal validation, path validation, and GPT review.
2. **Given** v1 is complete, **When** A5 is considered, **Then** promotion requires bounded patch application plus one narrow verification.
3. **Given** multiple OSS agents are considered, **When** parallel runtime is enabled, **Then** queueing and circuit breaker tests must pass first.

---

## Edge Cases

- Malformed `OSS_HANDOFF_JSON`: expected `invalid_handoff`, no model call trusted.
- Missing mission tier: expected lowest safe read-only mode or fail-closed if tools/write are requested.
- Tool argument aliases such as `path`, `file_path`, `file`, `filename`, and nested `args`: expected normalized path or validation error.
- Unsupported hosted, MCP, app, image, network, or broad shell tools: expected stripped, converted, blocked, or escalated.
- Duplicate full file read: expected synthetic duplicate suppression observation.
- Search with zero matches: expected evidence coverage PASS for the search action, not a false failure.
- Large files or directories: expected command-aware extraction, truncation, or deterministic PARTIAL within budget.
- Upstream timeout, malformed JSON, early close, or rate limit: expected terminal event and deadline-safe partial/failure report.
- Bridge source changed while old process is running: expected doctor stale-source failure or warning.
- Critical path term appears mid-investigation: expected read-only evidence allowed only if scoped, write blocked, GPT review required.
- Model returns status text, policy refusal, markdown without fields, or overconfident summary: expected report validation repair or PARTIAL/FAILED.

---

## BDD Scenarios

### Feature: OSS Agent Runtime v1 Managed Investigation

#### Scenario: Health exposes supervised running identity

**Traces to**: User Story 1, Acceptance Scenario 1
**Category**: Happy Path

- **Given** the bridge is running through a supported foreground, service, container, or externally verified supervisor backend
- **When** the user requests `/health`
- **Then** the response includes pid, ppid, started_at, uptime_seconds, source_path, source_sha256, config_fingerprint, and supervisor durability
- **And** no secret-bearing environment values are included

#### Scenario: Doctor detects stale running source

**Traces to**: User Story 1, Acceptance Scenario 2
**Category**: Error Path

- **Given** the running bridge source hash differs from `bridge.py` on disk
- **When** the user runs `codex-oss doctor`
- **Then** doctor reports running-source mismatch
- **And** doctor suggests restarting the supervised bridge

#### Scenario: Runtime classifies valid A3 mission

**Traces to**: User Story 2, Acceptance Scenario 1
**Category**: Happy Path

- **Given** a structured mission with tier `A3`, `write_allowed=false`, allowed read roots, forbidden roots, budget `20`, and investigation report fields
- **When** the runtime classifies the mission
- **Then** it selects managed investigation mode
- **And** it exposes only read/search/list/safe-git tools

#### Scenario: Runtime fails closed on malformed handoff

**Traces to**: User Story 2, Acceptance Scenario 2
**Category**: Error Path

- **Given** an `OSS_HANDOFF_JSON` block missing required mission fields
- **When** the runtime parses the handoff
- **Then** it selects `invalid_handoff`
- **And** no OSS model output is trusted as task completion

#### Scenario: Runtime fails closed on missing tier with tool autonomy

**Traces to**: User Story 2, Acceptance Scenario 3
**Category**: Error Path

- **Given** a handoff requests read/search tool autonomy without a MissionV1 `tier`
- **When** the runtime parses the handoff
- **Then** it selects `invalid_handoff`
- **And** it does not infer A2 or A3 from prose

#### Scenario: Runtime rejects unsupported autonomy tier

**Traces to**: User Story 2, Acceptance Scenario 4
**Category**: Alternate Path

- **Given** a structured mission requesting A6 critical autonomous engineer mode
- **When** the runtime classifies the mission
- **Then** it returns escalation
- **And** the report says GPT-5.5 must own the task

#### Scenario: Entrypoint extraction rejects multiple handoff blocks

**Traces to**: User Story 2, Acceptance Scenario 5
**Category**: Error Path

- **Given** a request contains two `<OSS_HANDOFF_JSON>` blocks
- **When** the runtime extracts MissionV1
- **Then** it fails closed as `invalid_handoff`
- **And** neither block grants A3 autonomy

#### Scenario: Broad read scope requires explicit opt-in

**Traces to**: User Story 2, Acceptance Scenario 6
**Category**: Error Path

- **Given** a mission uses `allowed_roots=["."]` without `allow_broad_read_scope=true`
- **When** the runtime validates MissionV1
- **Then** it fails closed or returns ESCALATE asking GPT-5.5 to narrow the scope

#### Scenario: Allowed search updates evidence ledger

**Traces to**: User Story 3, Acceptance Scenario 1
**Category**: Happy Path

- **Given** an active A3 mission with budget remaining
- **When** the model calls `rtk_grep` for an allowed symbol in an allowed root
- **Then** the runtime executes the search internally through RuntimeToolV1 using CommandExecutionPolicyV1
- **And** the evidence ledger records the command, exit code, and remaining budget

#### Scenario: A3 loop stays inside runtime boundary

**Traces to**: User Story 3, Acceptance Scenario 1
**Category**: Happy Path

- **Given** Codex sends a valid A3 MissionV1 request
- **When** the runtime performs the investigation
- **Then** model/tool iterations happen inside the bridge runtime
- **And** Codex receives one terminal Responses result, not intermediate raw tool continuation

#### Scenario: Duplicate read is suppressed

**Traces to**: User Story 3, Acceptance Scenario 2
**Category**: Edge Case

- **Given** the evidence ledger marks `README.md` as completely inspected
- **When** the model calls `rtk_read` for `README.md` again
- **Then** the runtime returns an `ALREADY READ` observation
- **And** duplicate_actions_blocked increases by one

#### Scenario: Read-only mission blocks write action

**Traces to**: User Story 3, Acceptance Scenario 3
**Category**: Error Path

- **Given** an active read-only A3 mission
- **When** the model requests `apply_patch`
- **Then** the runtime blocks the action
- **And** the mission returns escalation with no file changes

#### Scenario: Budget exhaustion returns partial report

**Traces to**: User Story 3, Acceptance Scenario 4
**Category**: Edge Case

- **Given** an active A3 mission has spent its final tool step
- **When** no valid final report has been produced
- **Then** the runtime returns PARTIAL
- **And** the PARTIAL report lists inspected files, commands, missing fields, and caveats

#### Scenario: Multiple actions are repaired then failed

**Traces to**: User Story 3, Acceptance Scenario 5
**Category**: Error Path

- **Given** an active A3 mission expects exactly one ManagedInvestigationActionV1
- **When** the model emits two tool calls in one response
- **Then** the runtime executes none of them
- **And** the runtime sends one repair instruction or returns PARTIAL/FAILED after a repeated multi-action violation

#### Scenario: Tool observations are redacted before model exposure

**Traces to**: User Story 3, Acceptance Scenario 6
**Category**: Error Path

- **Given** an allowed file contains `OPENAI_API_KEY=sk-test-secret`
- **When** the runtime reads the file for an A3 mission
- **Then** the observation sent to the OSS model contains a redacted value
- **And** the ledger records `redactions_applied=true`

#### Scenario: Effective tool budget is clamped by request deadline

**Traces to**: User Story 3, Acceptance Scenario 7
**Category**: Edge Case

- **Given** MissionV1 requests `tool_budget=20`
- **And** the request deadline allows only 10 estimated A3 turns after terminal reserve
- **When** the runtime starts the mission
- **Then** the effective tool budget is 10
- **And** the ledger records `budget_reduction_reason=request_deadline`

#### Scenario: Complete report passes schema validation

**Traces to**: User Story 4, Acceptance Scenario 1
**Category**: Happy Path

- **Given** a report includes status, files inspected, commands run, findings, uncertainties, confidence, caveats, escalation recommendation, and evidence refs that resolve to the mission ledger
- **When** the runtime validates the report
- **Then** the report is accepted as COMPLETE

#### Scenario: Intent text is rejected

**Traces to**: User Story 4, Acceptance Scenario 2
**Category**: Error Path

- **Given** the model output is `I will inspect the files now`
- **When** the runtime validates the output
- **Then** validation fails with `status_or_intent`
- **And** the output is not returned as completed task success

#### Scenario: Partial report with missing fields is accepted as partial

**Traces to**: User Story 4, Acceptance Scenario 3
**Category**: Alternate Path

- **Given** a report marks status PARTIAL and names missing fields
- **When** the runtime validates the report
- **Then** validation accepts it as PARTIAL
- **And** confidence cannot be HIGH

#### Scenario: Confident report without evidence coverage is rejected

**Traces to**: User Story 4, Acceptance Scenario 4
**Category**: Error Path

- **Given** a report claims PASS and HIGH confidence without covering requested paths or search terms
- **When** the runtime validates the report
- **Then** validation fails with `evidence_coverage`

#### Scenario: Finding without evidence reference is rejected

**Traces to**: User Story 4, Acceptance Scenario 5
**Category**: Error Path

- **Given** a report includes a finding without any `evidence_refs`
- **When** the runtime validates the report
- **Then** validation fails with `missing_evidence_refs`

#### Scenario: Finding with nonexistent evidence reference is rejected

**Traces to**: User Story 4, Acceptance Scenario 6
**Category**: Error Path

- **Given** a report includes `file:docs/CONTINUITY.md#extract:missing`
- **When** the runtime validates the report against EvidenceLedgerV1
- **Then** validation fails with `unresolved_evidence_ref`

#### Scenario: Streaming success emits terminal event

**Traces to**: User Story 5, Acceptance Scenario 1
**Category**: Happy Path

- **Given** a `stream=true` Responses request
- **When** upstream returns a valid streamed completion
- **Then** the bridge emits `response.created`, output events, one terminal event, and `[DONE]`

#### Scenario: Streaming failure emits terminal event

**Traces to**: User Story 5, Acceptance Scenario 2
**Category**: Error Path

- **Given** a `stream=true` Responses request
- **When** upstream stalls, closes early, or returns malformed JSON
- **Then** the bridge emits `response.incomplete` or `response.failed`
- **And** the stream ends with `[DONE]`

#### Scenario: Deadline returns deterministic partial

**Traces to**: User Story 5, Acceptance Scenario 3
**Category**: Edge Case

- **Given** primary and fallback model calls cannot finish before the request deadline
- **When** the deadline guard triggers
- **Then** the runtime returns deterministic PARTIAL
- **And** no background fallback is reported as user-visible success

#### Scenario: Recoverable mission failure maps to completed response

**Traces to**: User Story 5, Acceptance Scenario 5
**Category**: Alternate Path

- **Given** a recoverable policy failure such as queue timeout or invalid model action occurs
- **When** the runtime can produce a ValidatedReportV1
- **Then** the Responses transport status is `response.completed`
- **And** the report status is `FAILED` with a machine-readable reason

#### Scenario: Effective deadline uses smaller budget

**Traces to**: User Story 5, Acceptance Scenario 4
**Category**: Edge Case

- **Given** MissionV1 has `time_budget_seconds=180` and RuntimeContextV1 has `request_deadline_seconds=90`
- **When** the runtime calculates the effective deadline
- **Then** it uses 90 seconds minus deterministic partial reserve
- **And** model/tool/fallback work stops before that reserve is consumed

#### Scenario: Critical read is allowed with escalation marker

**Traces to**: User Story 6, Acceptance Scenario 1
**Category**: Alternate Path

- **Given** a mission explicitly permits read-only inspection of recovery docs with `critical_path_read_allowed=true` and a critical path reason
- **When** the model reads an allowed recovery doc
- **Then** the runtime records the read
- **And** the ledger records GPT review required

#### Scenario: Broad parent root does not permit critical read

**Traces to**: User Story 6, Acceptance Scenario 1
**Category**: Error Path

- **Given** a mission allows `src/` but does not explicitly allow critical-path reads
- **When** the model attempts to read `src/auth/token.ts`
- **Then** the runtime blocks or escalates the read
- **And** no file content is exposed to the OSS model

#### Scenario: Critical write is blocked

**Traces to**: User Story 6, Acceptance Scenario 2
**Category**: Error Path

- **Given** any OSS mission targets recovery, auth, schema, persistence, CI, migration, secret, or production data paths for writing
- **When** policy checks the action
- **Then** the runtime blocks the write
- **And** no file is touched

#### Scenario: Discovered critical evidence escalates final report

**Traces to**: User Story 6, Acceptance Scenario 3
**Category**: Edge Case

- **Given** an investigation finds critical-path evidence
- **When** the final report is validated
- **Then** the report must include an escalation recommendation for GPT review

#### Scenario: Doctor catches parent provider misuse

**Traces to**: User Story 7, Acceptance Scenario 1
**Category**: Error Path

- **Given** `.codex/config.toml` sets top-level `model_provider = "opencode_bridge"`
- **When** doctor runs
- **Then** doctor fails `config.parent_provider_safe`
- **And** it tells the user to keep GPT native and use the bridge only in agent TOMLs

#### Scenario: Doctor catches incomplete agent TOML

**Traces to**: User Story 7, Acceptance Scenario 2
**Category**: Error Path

- **Given** an OSS agent TOML lacks `model_provider` or `model`
- **When** doctor runs
- **Then** doctor reports the exact agent and missing field

#### Scenario: Doctor catches incomplete health identity

**Traces to**: User Story 7, Acceptance Scenario 3
**Category**: Edge Case

- **Given** `/health` omits source fingerprint or persistent state DB path
- **When** doctor runs
- **Then** doctor warns that runtime confidence is incomplete

#### Scenario: Default doctor does not burn model usage

**Traces to**: User Story 7, Acceptance Scenario 4
**Category**: Happy Path

- **Given** the user runs default `codex-oss doctor`
- **When** doctor completes
- **Then** it performs offline and bridge-health checks only
- **And** it does not perform live model inference unless `--live-model` is set

#### Scenario: Concurrent missions do not share ledgers

**Traces to**: User Story 3, Acceptance Scenario 1
**Category**: Edge Case

- **Given** two A3 missions with different `mission_id` values arrive concurrently
- **When** the runtime handles both requests
- **Then** each mission has an isolated EvidenceLedgerV1
- **And** any queueing or rejection result is terminal and does not corrupt either mission state

#### Scenario: A4 promotion requires patch proposal gate

**Traces to**: User Story 8, Acceptance Scenario 1
**Category**: Alternate Path

- **Given** v1 managed investigation has passed
- **When** maintainers enable A4 patch proposal mode
- **Then** tests must prove unified diff parsing, path validation, malformed patch rejection, and GPT review requirement

#### Scenario: A5 promotion requires narrow verification gate

**Traces to**: User Story 8, Acceptance Scenario 2
**Category**: Alternate Path

- **Given** A4 patch proposal mode has passed
- **When** maintainers enable A5 bounded implementation
- **Then** tests must prove allowed patch application, read-back, and one narrow verification command

#### Scenario: Parallel promotion requires queue and circuit breaker gate

**Traces to**: User Story 8, Acceptance Scenario 3
**Category**: Alternate Path

- **Given** multiple OSS missions are submitted concurrently
- **When** maintainers enable parallel runtime
- **Then** queue limits, per-model limits, cooldown, and deterministic terminal results must pass before release

---

## Test-Driven Development Plan

### Test Hierarchy

| Level | Scope | Purpose |
|---|---|---|
| Unit | Mission parsing, tier selection, tool schema normalization, report validation, critical path classification | Prove runtime decisions in isolation. |
| Integration | Bridge request handling, SQLite state, context pack, managed autonomy loop, doctor/health, fake upstream transport | Prove bridge components work together without relying on live provider behavior. |
| E2E | Supported supervisor backend, `/health`, `codex-oss doctor`, stream contract, representative A3 mission | Prove user-facing confidence gates. |

### Test Implementation Order

| Order | Test Name | Level | Traces to BDD Scenario | Description |
|---:|---|---|---|---|
| 1 | `test_parse_a3_mission_selects_managed_investigation` | Unit | Runtime classifies valid A3 mission | Valid A3 mission maps to read-only tools and budget. |
| 2 | `test_invalid_handoff_fails_closed` | Unit | Runtime fails closed on malformed handoff | Missing fields produce invalid handoff and no trusted OSS output. |
| 3 | `test_unsupported_tiers_escalate_in_v1` | Unit | Runtime rejects unsupported autonomy tier | A4/A5/A6 are not silently accepted in v1. |
| 4 | `test_missing_tier_tool_autonomy_fails_closed` | Unit | Runtime fails closed on missing tier with tool autonomy | Tool-using missions cannot infer A2/A3 from prose. |
| 5 | `test_tool_arg_aliases_normalize_to_path` | Unit | Allowed search updates evidence ledger | `path`, `file_path`, `file`, `filename`, nested `args` normalize consistently. |
| 6 | `test_path_policy_blocks_escape_symlink_and_secret_paths` | Unit | Critical write is blocked | PathPolicyV1 blocks escapes, symlink escapes, `.git`, env, and runtime secret paths. |
| 7 | `test_duplicate_read_returns_synthetic_observation` | Unit | Duplicate read is suppressed | Complete prior read blocks repeated file read. |
| 8 | `test_read_only_policy_blocks_write_tools` | Unit | Read-only mission blocks write action | Patch/write tools denied in A3. |
| 9 | `test_critical_path_write_escalates` | Unit | Critical write is blocked | Critical path writes denied regardless of prompt. |
| 10 | `test_report_schema_accepts_complete_report` | Unit | Complete report passes schema validation | Required fields accepted. |
| 11 | `test_report_schema_rejects_intent_text` | Unit | Intent text is rejected | "I will..." outputs fail. |
| 12 | `test_report_schema_requires_evidence_refs` | Unit | Finding without evidence reference is rejected | Every finding must cite ledger evidence. |
| 13 | `test_partial_report_requires_missing_fields_and_non_high_confidence` | Unit | Partial report with missing fields is accepted as partial | PARTIAL report contract enforced. |
| 14 | `test_confident_report_requires_evidence_coverage` | Unit | Confident report without evidence coverage is rejected | PASS/HIGH blocked without coverage. |
| 15 | `test_multi_action_response_executes_none_and_repairs_once` | Unit | Multiple actions are repaired then failed | Multiple actions are structurally rejected. |
| 16 | `test_unresolved_evidence_ref_rejects_report` | Unit | Finding with nonexistent evidence reference is rejected | Evidence refs must resolve to ledger entries. |
| 17 | `test_command_execution_policy_uses_argv_shell_false` | Unit | Allowed search updates evidence ledger | Runtime tools are argv-only with `shell=false`. |
| 18 | `test_observation_redacted_before_model_exposure` | Unit | Tool observations are redacted before model exposure | Secret-like values never reach OSS model observations. |
| 19 | `test_critical_read_requires_explicit_critical_permission` | Unit | Broad parent root does not permit critical read | Broad roots do not grant critical reads. |
| 20 | `test_effective_deadline_uses_min_budget_and_reserve` | Unit | Effective deadline uses smaller budget | Mission and request deadlines are reconciled. |
| 21 | `test_effective_tool_budget_clamps_by_deadline` | Unit | Effective tool budget is clamped by request deadline | Requested 20 turns becomes deadline-safe effective budget. |
| 22 | `test_entrypoint_extraction_rejects_multiple_blocks` | Unit | Entrypoint extraction rejects multiple handoff blocks | Exactly one marked JSON block allowed. |
| 23 | `test_broad_scope_requires_explicit_opt_in` | Unit | Broad read scope requires explicit opt-in | Broad roots fail without `allow_broad_read_scope=true`. |
| 24 | `test_default_deny_policy_is_non_overridable` | Unit | Broad read scope requires explicit opt-in | Runtime deny roots cannot be removed by mission. |
| 25 | `test_fallback_policy_separates_per_turn_and_mission_limits` | Unit | Effective tool budget is clamped by request deadline | Model call budget is unambiguous. |
| 26 | `test_response_status_mapping_prefers_completed_reports` | Unit | Recoverable mission failure maps to completed response | Recoverable failures return structured mission report. |
| 27 | `test_a1_context_pack_has_no_model_directed_tools` | Unit | Runtime fails closed on missing tier with tool autonomy | A1 compatibility cannot grant A3 behavior. |
| 28 | `test_secret_severity_policy_escalates_high_confidence_credentials` | Unit | Tool observations are redacted before model exposure | High-confidence credentials stop mission. |
| 29 | `test_a3_loop_runs_inside_runtime_boundary` | Integration | A3 loop stays inside runtime boundary | Codex gets one terminal response, not raw tool continuation. |
| 30 | `test_managed_loop_records_search_and_budget` | Integration | Allowed search updates evidence ledger | Fake model calls search; ledger records command and budget. |
| 31 | `test_managed_loop_blocks_duplicate_read` | Integration | Duplicate read is suppressed | Fake model repeats read; runtime suppresses. |
| 32 | `test_budget_exhaustion_returns_partial` | Integration | Budget exhaustion returns partial report | Mission ends with deterministic PARTIAL. |
| 33 | `test_concurrent_missions_serialize_with_queue_or_timeout` | Integration | Concurrent missions do not share ledgers | Active limit 1, queue max 2, queue timeout 5s. |
| 34 | `test_stream_success_terminal_contract` | Integration | Streaming success emits terminal event | Fake upstream success emits terminal stream. |
| 35 | `test_stream_failure_terminal_contract` | Integration | Streaming failure emits terminal event | Fake upstream stall/early close/malformed JSON ends deterministically. |
| 36 | `test_deadline_returns_partial_before_client_timeout` | Integration | Deadline returns deterministic partial | Deadline guard wins before fallback overrun. |
| 37 | `test_client_disconnect_cancels_internal_mission` | Integration | Deadline returns deterministic partial | Disconnect stops loop and does not spend background provider budget. |
| 38 | `test_health_includes_process_fingerprint` | Integration | Health exposes supervised running identity | `/health` includes identity fields with no secret values. |
| 39 | `test_doctor_detects_stale_source_hash` | Integration | Doctor detects stale running source | Doctor compares running and disk source hash. |
| 40 | `test_doctor_default_skips_live_model_inference` | Integration | Default doctor does not burn model usage | Live inference requires `--live-model`. |
| 41 | `test_doctor_detects_parent_provider_misuse` | Integration | Doctor catches parent provider misuse | Existing config check remains protected. |
| 42 | `test_doctor_detects_incomplete_agent_toml` | Integration | Doctor catches incomplete agent TOML | Existing agent config check remains protected. |
| 43 | `test_supervised_runtime_doctor_green_path` | E2E | Health exposes supervised running identity | Supported supervisor, health, and doctor work together. |
| 44 | `test_a3_fixture_mission_returns_validated_report` | E2E | Complete report passes schema validation | Representative A3 mission returns accepted COMPLETE or valid PARTIAL. |
| 45 | `test_vfuture_modes_remain_gated` | Unit | A4/A5/parallel promotion scenarios | Future modes remain unsupported or gated until tests exist. |

### Test Datasets

#### Dataset: Mission Classification

| # | Input | Boundary Type | Expected Output | Traces to | Notes |
|---:|---|---|---|---|
| 1 | Valid A2 mission with read/search tools | Happy path | Guided exploration selected | BDD Scenario: Runtime classifies valid A3 mission | A2 and A3 share read-only policy. |
| 2 | Valid A3 mission with budget 20 | Happy path | Managed investigation selected | BDD Scenario: Runtime classifies valid A3 mission | Main v1 target. |
| 3 | A3 mission with `write_allowed=true` | Error | Escalate or reject | BDD Scenario: Read-only mission blocks write action | v1 is read-only. |
| 4 | A4 mission | Boundary max+1 for v1 tier | Escalate unsupported | BDD Scenario: Runtime rejects unsupported autonomy tier | Future patch proposal. |
| 5 | A6 mission | Error | Escalate GPT-only | BDD Scenario: Runtime rejects unsupported autonomy tier | Critical engineer disallowed. |
| 6 | Malformed JSON | Error | `invalid_handoff` | BDD Scenario: Runtime fails closed on malformed handoff | Existing parser path. |
| 7 | Missing tier with read/search tools | Null/absence | `invalid_handoff` | BDD Scenario: Runtime fails closed on missing tier with tool autonomy | Autonomy cannot be inferred. |
| 8 | Missing tier with no-tool exact output | Null/absence | A0 allowed | BDD Scenario: Runtime fails closed on malformed handoff | Compatibility only. |
| 9 | Missing tier with explicit read-only paths and no exploration | Null/absence | A1 context-pack allowed | BDD Scenario: Runtime fails closed on malformed handoff | Compatibility only. |
| 10 | Two marked handoff blocks | Error | `invalid_handoff` | BDD Scenario: Entrypoint extraction rejects multiple handoff blocks | Exact entrypoint. |
| 11 | Unmarked JSON with `tier=A3` | Error | ignored for A3 authorization | BDD Scenario: Entrypoint extraction rejects multiple handoff blocks | Marker required. |
| 12 | `allowed_roots=["."]` without broad opt-in | Broad scope | fail closed or ESCALATE | BDD Scenario: Broad read scope requires explicit opt-in | Privacy/cost guard. |
| 13 | `tool_budget=1000` | Abusive budget | invalid_handoff | BDD Scenario: Effective tool budget is clamped by request deadline | Hard cap. |

#### Dataset: Tool Policy and Path Handling

| # | Input | Boundary Type | Expected Output | Traces to | Notes |
|---:|---|---|---|---|
| 1 | `{"path":"README.md"}` | Happy path | Normalize `README.md` | BDD Scenario: Allowed search updates evidence ledger | Current `normalize_tool_args` supports this. |
| 2 | `{"file_path":"README.md"}` | Alias | Normalize `README.md` | BDD Scenario: Allowed search updates evidence ledger | Provider drift guard. |
| 3 | `{"filename":"README.md"}` | Alias | Normalize `README.md` | BDD Scenario: Allowed search updates evidence ledger | Provider drift guard. |
| 4 | `{"args":{"file":"README.md"}}` | Nested | Normalize `README.md` | BDD Scenario: Allowed search updates evidence ledger | Nested alias. |
| 5 | `../secrets.env` | Path escape | Block | BDD Scenario: Critical write is blocked | Root boundary. |
| 6 | `.env` | Secret path | Block | BDD Scenario: Critical write is blocked | Secret-bearing config. |
| 7 | `apply_patch` in A3 | Forbidden tool | Escalate, no change | BDD Scenario: Read-only mission blocks write action | v1 invariant. |
| 8 | Duplicate `README.md` read | Duplicate | Synthetic `ALREADY READ` | BDD Scenario: Duplicate read is suppressed | Budget preservation. |
| 9 | Absolute project path | Path normalization | Convert to project-relative if under root | BDD Scenario: Allowed search updates evidence ledger | Local path compatibility. |
| 10 | Symlink to `.env` | Symlink escape | Block | BDD Scenario: Critical write is blocked | Realpath policy. |
| 11 | `.git/config` | Forbidden root | Block direct read | BDD Scenario: Critical write is blocked | Git only through safe tools. |
| 12 | `rtk git commit` | Forbidden git action | Block | BDD Scenario: Read-only mission blocks write action | Safe git boundary. |
| 13 | `rtk_grep` argv | Command execution | `["rtk","grep",pattern,path]`, `shell=false` | BDD Scenario: Allowed search updates evidence ledger | No command strings. |
| 14 | File containing `sk-test-secret` | Secret redaction | Redacted before model exposure | BDD Scenario: Tool observations are redacted before model exposure | Pre-model redaction. |
| 15 | Broad `src/` with `src/auth/token.ts` | Critical read | Block without explicit critical permission | BDD Scenario: Broad parent root does not permit critical read | Critical scope. |
| 16 | Mission omits `.codex-oss/state/` from forbidden roots | Default deny | Block anyway | BDD Scenario: Broad read scope requires explicit opt-in | Non-overridable denylist. |
| 17 | High-confidence credential in allowed file | Secret severity | ESCALATE before model exposure | BDD Scenario: Tool observations are redacted before model exposure | Severe redaction path. |

#### Dataset: Report Validation

| # | Input | Boundary Type | Expected Output | Traces to | Notes |
|---:|---|---|---|---|
| 1 | Complete report with all fields | Happy path | COMPLETE accepted | BDD Scenario: Complete report passes schema validation | Main success. |
| 2 | `I will inspect the files now` | Error | Reject `status_or_intent` | BDD Scenario: Intent text is rejected | Weak completion. |
| 3 | Empty string | Empty | Reject `report_too_short` | BDD Scenario: Intent text is rejected | Minimum report length. |
| 4 | Report missing confidence | Missing field | Reject or PARTIAL with missing fields | BDD Scenario: Partial report with missing fields is accepted as partial | Schema discipline. |
| 5 | PARTIAL with missing fields and MEDIUM confidence | Alternate path | PARTIAL accepted | BDD Scenario: Partial report with missing fields is accepted as partial | Deadline-safe. |
| 6 | PASS/HIGH without evidence coverage | Error | Reject `evidence_coverage` | BDD Scenario: Confident report without evidence coverage is rejected | False confidence prevention. |
| 7 | Critical-path report without escalation | Error | Reject `escalation_recommendation` | BDD Scenario: Discovered critical evidence escalates final report | Critical path safety. |
| 8 | Finding without `evidence_refs` | Error | Reject `missing_evidence_refs` | BDD Scenario: Finding without evidence reference is rejected | Claim support required. |
| 9 | Critical report says "safe to merge" | Forbidden finality claim | Reject or force escalation | BDD Scenario: Discovered critical evidence escalates final report | GPT owns final decisions. |
| 10 | Finding refs nonexistent extract | Error | Reject `unresolved_evidence_ref` | BDD Scenario: Finding with nonexistent evidence reference is rejected | Evidence resolution. |
| 11 | Zero-match grep evidence | Edge case | Allow claim with `command:<turn>#zero_match` | BDD Scenario: Complete report passes schema validation | Absence can be evidence. |

#### Dataset: Transport and Deadline

| # | Input | Boundary Type | Expected Output | Traces to | Notes |
|---:|---|---|---|---|
| 1 | Upstream success stream | Happy path | `response.completed` and `[DONE]` | BDD Scenario: Streaming success emits terminal event | Existing v9 surface. |
| 2 | Upstream stall | Timeout | `response.incomplete` or `response.failed` and `[DONE]` | BDD Scenario: Streaming failure emits terminal event | No hangs. |
| 3 | Upstream early close | Partial dependency failure | Terminal event and `[DONE]` | BDD Scenario: Streaming failure emits terminal event | No broken SSE. |
| 4 | Malformed JSON | Error | Terminal failure event | BDD Scenario: Streaming failure emits terminal event | Protocol robustness. |
| 5 | Fallback exceeds deadline | Timeout | Deterministic PARTIAL before client timeout | BDD Scenario: Deadline returns deterministic partial | Client-visible truth. |
| 6 | Client disconnect | Cancellation | Cancel fallback; no success claim | BDD Scenario: Deadline returns deterministic partial | Avoid hidden success logs. |
| 7 | Mission 180s, request 90s | Conflicting budget | Effective deadline 90s minus reserve | BDD Scenario: Effective deadline uses smaller budget | Budget reconciliation. |
| 8 | Three concurrent A3 missions | Concurrency | One active, two queued or timed out terminally | BDD Scenario: Concurrent missions do not share ledgers | v1 serialization. |
| 9 | Queue timeout | Capacity | `response.completed` with report status FAILED | BDD Scenario: Recoverable mission failure maps to completed response | Transport mapping. |
| 10 | Recoverable invalid model action | Policy failure | `response.completed` with report status FAILED or PARTIAL | BDD Scenario: Recoverable mission failure maps to completed response | Prefer structured report. |

### Regression Test Requirements

| Existing Behaviour | Existing Test | New Regression Test Needed | Notes |
|---|---|---|---|
| Malformed structured handoffs fail closed | `tests/test_protocol_conformance.py::assert_malformed_handoff_fails_closed` | No | Keep unchanged. |
| Exact writes require exact content | `tests/test_protocol_conformance.py::assert_exact_write_requires_exact_content` | No for v1 | v1 read-only should not weaken exact-write semantics. |
| Evidence coverage blocks confident pass | `tests/test_protocol_conformance.py::assert_incomplete_evidence_blocks_confident_pass` | Yes: add A3 report variant | Existing context-pack coverage should extend to managed investigation. |
| Bounded patch requires observed verification | `tests/test_protocol_conformance.py::assert_verification_claims_need_observed_results` | No for v1 | Future A4/A5 gate. |
| Transport stream has terminal SSE | `tests/test_v9_transport.py` | Yes: add managed-investigation status-text/partial case | Runtime finalization must share transport guarantees. |
| Evidence ledger injection exists | `tests/test_v10_autonomy.py::test_multi_turn_with_evidence_ledger` | Yes: add persistent ledger/duplicate suppression proof | Existing test is live-style and should become deterministic fixture coverage. |
| Doctor catches parent-provider misuse | `codex_oss/doctor.py` checks | Yes: add source hash mismatch proof | Health identity hardening is central to v1 confidence. |

---

## Functional Requirements

- **FR-001**: System MUST represent every v1 autonomous OSS task as a Mission with tier, mode, allowed roots, forbidden roots, tool budget, time budget, write permission, stop conditions, and report schema.
- **FR-002**: System MUST fail closed on malformed structured handoffs.
- **FR-003**: System MUST fail closed when a tool-using mission omits an explicit A2/A3 tier.
- **FR-004**: System MUST normalize common tool argument aliases before policy checks.
- **FR-005**: System MUST restrict v1 A2/A3 missions to read-only RuntimeToolV1 tool classes.
- **FR-006**: System MUST enforce PathPolicyV1 before every file or git inspection action.
- **FR-007**: System MUST block unsupported, network, secret, production, broad shell, git write, deploy, and patch/write tools in v1 managed investigation.
- **FR-008**: System MUST persist or reconstruct an EvidenceLedgerV1 across multi-turn Responses continuation.
- **FR-009**: System MUST suppress duplicate complete reads and return a progress-oriented synthetic observation.
- **FR-010**: System MUST compact mission state for the model while retaining raw observations for validation.
- **FR-011**: System MUST reject status/intent text as final task completion.
- **FR-012**: System MUST reject multi-action model responses without executing any action and allow at most one repair attempt.
- **FR-013**: System MUST validate final reports against ValidatedReportV1.
- **FR-014**: System MUST require every finding to include evidence references.
- **FR-015**: System MUST reject findings whose evidence references do not resolve to EvidenceLedgerV1 entries.
- **FR-016**: System MUST accept incomplete work only as explicit PARTIAL with missing fields, evidence gathered, confidence, and caveats.
- **FR-017**: System MUST classify critical paths and block OSS writes/final decisions there.
- **FR-018**: System MUST require explicit critical-path read permission for critical reads; broad parent roots are insufficient.
- **FR-019**: System MUST mark critical-path investigations as GPT-review-required.
- **FR-020**: System MUST execute runtime tools internally using CommandExecutionPolicyV1.
- **FR-021**: System MUST redact tool observations before sending them to OSS models.
- **FR-022**: System MUST execute A2/A3 loops inside the runtime and return a single terminal Responses result to Codex.
- **FR-023**: System MUST reconcile mission time and request deadline using the smaller effective deadline with deterministic partial reserve.
- **FR-024**: System MUST guarantee terminal Responses output for every `stream=true` request path.
- **FR-025**: System MUST return deterministic PARTIAL before client timeout when model/fallback cannot finish within the request deadline.
- **FR-026**: System MUST expose running process identity, source hash, config fingerprint, state DB path, and supervisor durability through health or doctor.
- **FR-027**: System MUST make `codex-oss doctor` detect stale running source, unsupervised bridge, parent provider misuse, agent TOML defects, missing state persistence, and failed health checks.
- **FR-028**: System MUST keep live model inference out of default doctor unless `--live-model` is explicitly requested.
- **FR-029**: System MUST serialize concurrent A3 missions with the v1 queue policy and prevent ledger/state sharing.
- **FR-030**: System MUST cancel internal mission work on client disconnect and avoid background provider spend.
- **FR-031**: System MUST redact secret-like strings from logs, health, doctor output, reports, deterministic partials, and model observations.
- **FR-032**: System MUST extract A3 MissionV1 only from exactly one marked `OSS_HANDOFF_JSON` block.
- **FR-033**: System MUST apply DefaultDenyPolicyV1 regardless of mission-provided forbidden roots.
- **FR-034**: System MUST require explicit broad-scope opt-in and enforce broad-scope caps.
- **FR-035**: System MUST disable upstream provider-native tools for A2/A3 and use JSON-only action protocol.
- **FR-036**: System MUST enforce EffectiveBudgetPolicyV1, including hard caps, deadline-based budget reduction, and budget provenance.
- **FR-037**: System MUST enforce RiskTierPolicyV1.
- **FR-038**: System MUST enforce ResponseStatusMappingV1 for mission outcomes versus Responses transport outcomes.
- **FR-039**: System MUST define A1 context-pack compatibility as deterministic source gathering with no model-directed tools.
- **FR-040**: System MUST apply SecretDetectionSeverityPolicyV1 before OSS model exposure.
- **FR-041**: System SHOULD provide deterministic fixture tests for managed investigation without requiring live OpenCode Go.
- **FR-042**: System SHOULD preserve existing context-pack, exact-write, bounded-patch, and transport contract behavior while adding v1 managed investigation hardening.
- **FR-043**: System MAY document A4/A5/parallel modes as future gates, but MUST NOT require them for v1 success.

---

## Success Criteria

- **SC-001**: `python3 tests/test_protocol_conformance.py` passes after v1 changes.
- **SC-002**: `python3 tests/test_v9_transport.py` passes or its successor proves all stream=true cases end with one terminal event and `[DONE]`.
- **SC-003**: A deterministic A3 fixture test proves search -> read -> duplicate suppression -> valid report or valid PARTIAL without live provider dependency.
- **SC-004**: A report consisting only of status/intent text is rejected in an executable test.
- **SC-005**: An A3 mission attempting any write action produces escalation and leaves `rtk git status --short` unchanged except for test fixtures created by the test harness.
- **SC-006**: Doctor detects a stale running source hash before bridge restart in an executable or faithful fixture test.
- **SC-007**: Every v1 FR row in this document is mapped to at least one BDD scenario and one planned test.
- **SC-008**: No v1 test requires A4 patch proposal, A5 bounded implementation, or A6 critical autonomous engineer capability.
- **SC-009**: Fixture tests produce 0 unauthorized writes, 0 secret reads, and 0 critical-path write executions.
- **SC-010a**: Deterministic fixture suite pass rate is 100% for infrastructure, policy, validation, transport, and A3 runtime-boundary tests.
- **SC-010b**: Live beta burn-in reaches at least 80% valid COMPLETE/PARTIAL report rate on 25 read-only missions with 0 false-confidence incidents.
- **SC-011**: Default doctor performs 0 live model inference calls.
- **SC-012**: Three simultaneous A3 fixture missions obey one-active/two-queued behavior or return terminal capacity failures without ledger sharing.
- **SC-013**: Tool observations containing secret-like strings are redacted before OSS model exposure in executable tests.
- **SC-014**: A3 entrypoint extraction rejects malformed, missing, duplicate, and unmarked mission JSON in executable tests.
- **SC-015**: EffectiveBudgetPolicyV1 clamps or rejects oversized budgets and records budget provenance.
- **SC-016**: Recoverable mission failures return `response.completed` with structured FAILED/PARTIAL/ESCALATE reports, while protocol/config/auth failures use `response.failed`.

---

## Traceability Matrix

| Requirement | User Story | BDD Scenario(s) | Test Name(s) |
|---|---|---|---|
| FR-001 | US-2 | Runtime classifies valid A3 mission | `test_parse_a3_mission_selects_managed_investigation` |
| FR-002 | US-2 | Runtime fails closed on malformed handoff | `test_invalid_handoff_fails_closed` |
| FR-003 | US-2 | Runtime fails closed on missing tier with tool autonomy | `test_missing_tier_tool_autonomy_fails_closed` |
| FR-004 | US-3 | Allowed search updates evidence ledger | `test_tool_arg_aliases_normalize_to_path` |
| FR-005 | US-2, US-3 | Runtime classifies valid A3 mission; Read-only mission blocks write action | `test_parse_a3_mission_selects_managed_investigation`, `test_read_only_policy_blocks_write_tools` |
| FR-006 | US-3, US-6 | Critical write is blocked | `test_path_policy_blocks_escape_symlink_and_secret_paths`, `test_critical_path_write_escalates` |
| FR-007 | US-3, US-6 | Read-only mission blocks write action; Critical write is blocked | `test_read_only_policy_blocks_write_tools`, `test_critical_path_write_escalates` |
| FR-008 | US-3 | Allowed search updates evidence ledger | `test_managed_loop_records_search_and_budget` |
| FR-009 | US-3 | Duplicate read is suppressed | `test_duplicate_read_returns_synthetic_observation`, `test_managed_loop_blocks_duplicate_read` |
| FR-010 | US-3 | Allowed search updates evidence ledger; Budget exhaustion returns partial report | `test_managed_loop_records_search_and_budget`, `test_budget_exhaustion_returns_partial` |
| FR-011 | US-4 | Intent text is rejected | `test_report_schema_rejects_intent_text` |
| FR-012 | US-3 | Multiple actions are repaired then failed | `test_multi_action_response_executes_none_and_repairs_once` |
| FR-013 | US-4 | Complete report passes schema validation | `test_report_schema_accepts_complete_report` |
| FR-014 | US-4 | Finding without evidence reference is rejected | `test_report_schema_requires_evidence_refs` |
| FR-015 | US-4 | Finding with nonexistent evidence reference is rejected | `test_unresolved_evidence_ref_rejects_report` |
| FR-016 | US-4 | Partial report with missing fields is accepted as partial | `test_partial_report_requires_missing_fields_and_non_high_confidence` |
| FR-017 | US-6 | Critical write is blocked | `test_critical_path_write_escalates` |
| FR-018 | US-6 | Critical read is allowed with escalation marker; Broad parent root does not permit critical read | `test_critical_read_requires_explicit_critical_permission` |
| FR-019 | US-6 | Critical read is allowed with escalation marker; Discovered critical evidence escalates final report | `test_critical_path_write_escalates`, `test_a3_fixture_mission_returns_validated_report` |
| FR-020 | US-3 | Allowed search updates evidence ledger | `test_command_execution_policy_uses_argv_shell_false` |
| FR-021 | US-3 | Tool observations are redacted before model exposure | `test_observation_redacted_before_model_exposure` |
| FR-022 | US-3 | A3 loop stays inside runtime boundary | `test_a3_loop_runs_inside_runtime_boundary` |
| FR-023 | US-5 | Effective deadline uses smaller budget | `test_effective_deadline_uses_min_budget_and_reserve` |
| FR-024 | US-5 | Streaming success emits terminal event; Streaming failure emits terminal event | `test_stream_success_terminal_contract`, `test_stream_failure_terminal_contract` |
| FR-025 | US-5 | Deadline returns deterministic partial | `test_deadline_returns_partial_before_client_timeout` |
| FR-026 | US-1, US-7 | Health exposes supervised running identity; Doctor catches incomplete health identity | `test_health_includes_process_fingerprint`, `test_doctor_detects_stale_source_hash` |
| FR-027 | US-1, US-7 | Doctor detects stale running source; Doctor catches parent provider misuse; Doctor catches incomplete agent TOML | `test_doctor_detects_stale_source_hash`, `test_doctor_detects_parent_provider_misuse`, `test_doctor_detects_incomplete_agent_toml` |
| FR-028 | US-7 | Default doctor does not burn model usage | `test_doctor_default_skips_live_model_inference` |
| FR-029 | US-3 | Concurrent missions do not share ledgers | `test_concurrent_missions_serialize_with_queue_or_timeout` |
| FR-030 | US-5 | Deadline returns deterministic partial | `test_client_disconnect_cancels_internal_mission` |
| FR-031 | US-1, US-3, US-7 | Health exposes supervised running identity; Tool observations are redacted before model exposure; Default doctor does not burn model usage | `test_health_includes_process_fingerprint`, `test_observation_redacted_before_model_exposure`, `test_doctor_default_skips_live_model_inference` |
| FR-032 | US-2 | Entrypoint extraction rejects multiple handoff blocks | `test_entrypoint_extraction_rejects_multiple_blocks` |
| FR-033 | US-2, US-3 | Broad read scope requires explicit opt-in | `test_default_deny_policy_is_non_overridable` |
| FR-034 | US-2 | Broad read scope requires explicit opt-in | `test_broad_scope_requires_explicit_opt_in` |
| FR-035 | US-3 | A3 loop stays inside runtime boundary | `test_a3_loop_runs_inside_runtime_boundary` |
| FR-036 | US-3, US-5 | Effective tool budget is clamped by request deadline; Effective deadline uses smaller budget | `test_effective_tool_budget_clamps_by_deadline`, `test_effective_deadline_uses_min_budget_and_reserve` |
| FR-037 | US-2, US-6 | Runtime classifies valid A3 mission; Critical read is allowed with escalation marker | `test_parse_a3_mission_selects_managed_investigation`, `test_critical_read_requires_explicit_critical_permission` |
| FR-038 | US-5 | Recoverable mission failure maps to completed response | `test_response_status_mapping_prefers_completed_reports` |
| FR-039 | US-2 | Runtime fails closed on missing tier with tool autonomy | `test_a1_context_pack_has_no_model_directed_tools` |
| FR-040 | US-3 | Tool observations are redacted before model exposure | `test_secret_severity_policy_escalates_high_confidence_credentials` |
| FR-041 | US-3, US-4 | Allowed search updates evidence ledger; Complete report passes schema validation | `test_a3_fixture_mission_returns_validated_report` |
| FR-042 | US-5, US-7 | Streaming success emits terminal event; Doctor catches parent provider misuse | `test_protocol_conformance.py`, `test_v9_transport.py` |
| FR-043 | US-8 | A4 promotion requires patch proposal gate; A5 promotion requires narrow verification gate; Parallel promotion requires queue and circuit breaker gate | `test_vfuture_modes_remain_gated` |

---

## Coverage-Scan Review

### SUMMARY

Verdict: **incomplete by design**. The repo already contains several v1 building blocks, but the spec identifies pending hardening around mission-tier explicitness, strict report schema for managed investigation, source-hash doctor checks, deterministic A3 fixtures, and critical path policy enforcement.

### COVERAGE TABLES

#### Symbol Group: `managed_autonomy`, `TaskSession`, `EvidenceLedger`, `duplicate`

| occurrence | file:line | context | expected status | status | notes |
|---|---|---|---|---|---|
| `TaskSession` | `bridge.py:579` | Runtime session dataclass already tracks mode, budgets, reads, commands, required outputs. | updated | pending | Good base, but must become or wrap MissionV1 with explicit tier and policies. |
| `ReadLedger` | `bridge.py:383` | Tracks paths and commands as strings. | updated | pending | Must be replaced or augmented by EvidenceLedgerV1. |
| `inject_evidence_ledger` | `bridge.py:490` | Injects compact progress context. | updated | pending | Must emit ContextCompactionPolicyV1 summaries from EvidenceLedgerV1. |
| `suppress_duplicate_read` | `bridge.py:1080` | Suppresses repeat reads. | updated | updated | Needs deterministic tests around complete/incomplete read distinction. |
| `managed_autonomy` | `bridge.py:1100` | Execution mode exists. | updated | pending | Needs internal A2/A3 runtime loop using MissionV1, RuntimeToolV1, ManagedInvestigationActionV1, and ValidatedReportV1. |
| `test_multi_turn_with_evidence_ledger` | `tests/test_v10_autonomy.py:82` | Live-style proof for ledger injection. | updated | pending | Needs deterministic fixture test independent of live OSS behavior. |

#### Symbol Group: `context_pack_report`, `evidence_coverage`, `validate_report_output`

| occurrence | file:line | context | expected status | status | notes |
|---|---|---|---|---|---|
| `build_context_pack` | `bridge.py:724` | Command-aware source gathering and budget caps exist. | updated | updated | Existing context-pack behavior should remain regression-protected. |
| `evaluate_evidence_coverage` | `bridge.py:924` | Checks requested paths and search terms. | updated | updated | Extend same principle to A3 reports. |
| `build_context_pack_deterministic_report` | `bridge.py:969` | Deterministic PARTIAL fallback exists. | updated | updated | Good model for A3 budget/deadline partial. |
| `assert_incomplete_evidence_blocks_confident_pass` | `tests/test_protocol_conformance.py:66` | Prevents confident pass with missing evidence. | updated | updated | Add managed-investigation variant. |
| `validate_report_output` | `bridge.py` | Referenced in protocol tests for report validation. | updated | pending | Needs strict A3 schema fields and status/intent classification. |

#### Symbol Group: `health`, `doctor`, `supervisor`, `source_sha`

| occurrence | file:line | context | expected status | status | notes |
|---|---|---|---|---|---|
| `codex-oss up --daemon` | `codex_oss/cli.py:47` | Existing detached supervisor CLI exists. | updated | updated | v1 should define supported supervisor backends and make durability a hard confidence gate. |
| supervisor file | `codex_oss/cli.py:351` | Supervisor metadata written. | updated | pending | Need health/doctor source mismatch proof. |
| `_check_bridge` | `codex_oss/doctor.py:261` | Doctor queries health and tests bridge. | updated | pending | Needs source hash comparison, config fingerprint checks, and DoctorPolicyV1 modes. |
| `bridge.supervisor` | `codex_oss/doctor.py:279` | Warns when the current bridge is not under the expected supervisor mode. | updated | updated | Good existing invariant, but should align with DoctorPolicyV1 supervisor backend names. |
| `test_health_identity` | `tests/test_v9_transport.py:155` | Health identity test exists. | updated | pending | Confirm it includes source hash/config fingerprint/stale detection. |

#### Symbol Group: `stream`, `response.completed`, `response.failed`, `response.incomplete`, `[DONE]`

| occurrence | file:line | context | expected status | status | notes |
|---|---|---|---|---|---|
| Streaming contract doc | `README.md:84` | Describes SSE streaming with heartbeat. | updated | updated | Keep as user-facing contract. |
| v9 transport tests | `tests/test_v9_transport.py:2` | Stream terminal gauntlet exists. | updated | updated | Add status-text/fallback A3 terminal cases. |
| v8 integration tests | `tests/test_v8_integration.py:142` | Stalled upstream should return within deadline. | updated | updated | Align with request-level deadline invariant. |

#### Symbol Group: `bounded_write`, `patch`, `verification`

| occurrence | file:line | context | expected status | status | notes |
|---|---|---|---|---|---|
| `bounded_write_exact` | `README.md:161` | Exact write mode documented. | not-applicable | not-applicable | v1 must preserve but not expand writes. |
| `bounded_write_patch` | `README.md:162` | Patch/docs write mode documented. | not-applicable | not-applicable | vFuture A4/A5 gates. |
| `assert_patch_acceptance_requires_scope_change_and_verification` | `tests/test_protocol_conformance.py:87` | Existing patch guard. | updated | updated | Keep regression. |
| `assert_verification_claims_need_observed_results` | `tests/test_protocol_conformance.py:118` | Blocks prose-only verification claims. | updated | updated | Useful pattern for A3 report evidence validation. |

### TOUCHPOINT CHECK

| Touchpoint | Status | Evidence / Notes |
|---|---|---|
| Prompts / handoff contract | pending | README and installer mention structured handoff. A2/A3 must require MissionV1 JSON; prose fallback only for A0/A1 compatibility. |
| Schemas / contracts | pending | `parse_task_envelope` exists; MissionV1, RuntimeToolV1, EvidenceLedgerV1, ValidatedReportV1, and policy schemas now exist in this spec but not code. |
| Persistence / writes | pending | SQLite `StoredResponse` and string ledgers exist; EvidenceLedgerV1, StatePolicyV1, and mission isolation are pending in code. |
| Runtime loop | pending | Managed autonomy mode exists; policy-mediated plan-act-observe loop needs deterministic tests and stricter stop conditions. |
| Transport | updated | v8/v9 tests cover terminal behavior; add A3/status-text/fallback cases. |
| Doctor / health | pending | Existing doctor checks bridge/supervisor/config; source mismatch and fingerprint checks are pending. |
| Tests | pending | Protocol, transport, handoff tests exist; A3 deterministic mission tests are needed. |
| Docs | updated | This spec defines v1 and roadmap split. README may later need a concise v1 gate summary. |

### FALSIFICATION NOTES

1. Plausible miss: managed investigation may still accept fluent status text through a path not covered by context-pack validation.
   Evidence: intent patterns exist in `bridge.py`, but this spec marks A3-specific report validation tests as pending.

2. Plausible miss: doctor may report a healthy bridge while a stale process runs old source.
   Evidence: doctor checks health and supervisor today, but the source hash comparison proof is marked pending.

3. Plausible miss: A3 read-only mode may accidentally inherit broad Codex tools because the bridge strips unsupported provider tools but does not yet expose a strict runtime-owned investigation schema.
   Evidence: tool filtering exists, tool arg normalization exists, but Mission-owned tool classes are marked pending.

### PENDING GAPS

- `bridge.py:383`: Replace or augment string-based `ReadLedger` with EvidenceLedgerV1, including claim-to-evidence refs.
- `bridge.py:579`: Add explicit MissionV1 tier, policy, deadline, fallback, and report schema fields to `TaskSession` or introduce a separate `Mission` object.
- `bridge.py:871`: Harden report validation for A3 with ValidatedReportV1, PARTIAL semantics, confidence rules, status/intent rejection, multi-action rejection, and resolvable evidence refs.
- `bridge.py:1080`: Add deterministic duplicate suppression tests, including incomplete-read handling.
- `codex_oss/doctor.py:261`: Add running-source hash, on-disk hash, config fingerprint, DoctorPolicyV1 modes, and stale process detection.
- `tests/test_v10_autonomy.py:82`: Add deterministic fixture-based managed investigation tests independent of live OpenCode Go.
- `tests/test_v9_transport.py:155`: Extend health/transport tests for source fingerprint and A3 terminal partial cases.
- `bridge.py`: Add A2/A3 internal execution boundary, LoopholeFixesV1.1, EntryPointExtractionPolicyV1, A1ContextPackCompatibilityV1, UpstreamModelCallPolicyV1, AllowedPathsPolicyV1, BroadScopePolicyV1, DefaultDenyPolicyV1, RiskTierPolicyV1, EffectiveBudgetPolicyV1, ActionProtocolEnforcementV1, CommandExecutionPolicyV1, PathPolicyV1, CriticalPathRegistryV1, CriticalPathReadPermissionV1, DeadlineInteractionRule, ResponseStatusMappingV1, ConcurrencyPolicyV1, FallbackPolicyV1, StatePolicyV1, EvidenceRefResolutionPolicyV1, SecretDetectionSeverityPolicyV1, and PrivacyPolicyV1 enforcement points.

---

## Roadmap Gates

### Gate 1 - Infrastructure Confidence

- Supervised sidecar runs under a supported foreground, service, container, or externally verified supervisor backend.
- Health exposes running identity and source/config fingerprints.
- Doctor detects stale process, unsupervised process, parent-provider misuse, agent TOML defects, and state DB persistence issues.
- `stream=true` always terminates.

### Gate 2 - Mission Contract

- Mission schema supports A0-A6 but v1 enables only A2/A3 managed read-only work.
- A3 requires MissionV1 JSON; missing tier fails closed for tool autonomy.
- Malformed handoffs fail closed.
- Tool classes are runtime-owned and explicit.
- Critical paths trigger read-only escalation and write denial.
- A2/A3 execution is internal to the runtime; Codex receives only one terminal Responses result.
- Runtime tools execute through argv-only `shell=false` commands.
- Tool observations are redacted before OSS model exposure.

### Gate 3 - Managed Investigation

- Plan-act-observe loop supports one legal action per turn.
- EvidenceLedger persists across continuation turns.
- Duplicate suppression prevents rereading complete files.
- Budget exhaustion returns valid PARTIAL.
- Final report schema rejects status text and false confidence.
- Final report findings require evidence refs that resolve to EvidenceLedgerV1 entries.
- Mission deadlines and request deadlines use the smaller effective deadline with terminal reserve.
- A3 missions serialize through the v1 queue policy and never share ledgers.

### Gate 4A - v1 Read-Only Burn-In

- Run 25 delegated tasks before normalizing use:
  - 10 read-only investigations
  - 5 context-pack reports
  - 5 docs/test inventories
  - 5 critical-path read-only escalation tests
- Required thresholds: 0 unauthorized writes, 0 secret reads, 0 stream breaks, at least 80% valid COMPLETE/PARTIAL reports, 0 false-confidence incidents, and GPT cleanup mostly minor for accepted reports.

### Gate 4B - vFuture Exploratory Non-Shipping Tests

- A4 patch proposals may be tested in dry-run only.
- A5 bounded implementations may be tested in isolated fixtures only.
- These tests do not count toward v1 readiness.

### vFuture Gate 5 - Patch Proposal

- Unified diff proposal only.
- Runtime validates paths and malformed patches.
- Runtime does not apply forbidden paths.
- GPT review remains required.

### vFuture Gate 6 - Bounded Implementation

- Runtime applies only validated allowed patches.
- Runtime reads back changed files.
- Runtime runs one narrow verification.
- Critical paths remain GPT-only for writes and final decisions.

### vFuture Gate 7 - Parallel Worker Pool

- Global and per-model concurrency limits.
- Queueing rather than provider stampede.
- Circuit breaker cooldown.
- All agents return COMPLETE, PARTIAL, ESCALATE, or FAILED.

---

## Assumptions

- The bridge remains a local development tool and binds only to localhost.
- The parent Codex session remains GPT-native; `opencode_bridge` stays subagent-only.
- v1 does not require live provider tests for core correctness; fake upstream and deterministic fixtures are mandatory.
- Existing untracked `.codex-oss/` and `tmp/` directories are user/runtime artifacts and are outside this spec change.

## Clarifications

### 2026-05-07

- Q: Should the full OSS Agent Runtime be implemented in one pass? -> A: No. v1 should target A2/A3 managed investigation; A4/A5/parallel modes remain roadmap gates.
- Q: Should OSS agents become GPT-5.5 replacements? -> A: No. GPT-5.5 remains orchestrator, risk owner, and final judge.
- Q: Should exact writes depend on model self-reporting? -> A: No. Exact writes remain deterministic runtime operations and are out of v1 managed-investigation scope.
