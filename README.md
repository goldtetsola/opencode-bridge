# OpenCode Bridge

Runtime-backed OSS subagents for Codex. Spawn DeepSeek, Kimi, and Flash workers that inspect files, gather evidence, propose patches, apply changes in isolation, verify, and report — governed by a MissionV1 runtime rather than prompt-only discipline.

## Architecture

```
GPT-5.5 Orchestrator (Codex)
  |
  +- Custom subagent -> opencode_bridge provider -> /v1/responses
  |
  +- MissionV1 Runtime
  |    +- Path / secret / critical-path policy
  |    +- Evidence ledger + claim/answer/coverage graphs
  |    +- Semantic review with structured reason codes
  |    +- PatchRecipe / PatchIntent builder
  |    +- Isolated worktree apply + verification + rollback
  |    +- Decision trace + audit
  |
  +- OSS model (Kimi / DeepSeek / Flash)
  |
  +- GPT-5.5 final review
```

**Core principle:** runtime owns execution; model owns reasoning; GPT owns judgment. The model is a reasoning engine inside a governed control plane, not the entire agent.

## Quick start

```bash
git clone <repo-url>
cd opencode-bridge

# Set your OpenCode Go API key
echo 'OPENCODE_GO_API_KEY=sk-...' > .codex-oss/env/opencode-go.env

# Install into a Codex project
python3 bin/codex-oss install --project /path/to/your-codex-project

# Start the bridge
bin/codex-oss up --daemon

# Verify
bin/codex-oss doctor
bin/codex-oss claim-status --project . --json
```

After install, your Codex project has agent TOMLs and an `opencode_bridge` provider block. Codex can spawn `oss_kimi_investigator`, `oss_deepseek_investigator`, and `oss_flash_context`.

## Mission tiers

| Tier | Name | What it does | Status |
|------|------|-------------|--------|
| A2 | Deterministic lookup | Function location, config extraction, mapping lookup, zero-match evidence — no model call needed | Green |
| A3 | Managed investigation | Read files, search, form hypotheses, return evidence-backed reports | Green |
| A3-open | Open investigation | Ambiguous questions, multi-file exploration, evidence obligations, agenda-guided | Green |
| A4 | Patch proposal | Propose bounded patches via raw diff, PatchIntent, or PatchRecipe | Viable |
| A5 | Bounded implementation | Apply validated patches in isolated worktree, verify, report | Viable |
| A6 | Critical implementation | Critical-path simulation, certified apply with multi-review | Proof |

## Capability matrix

| Lane | Status | Use today? |
|------|--------|------------|
| A2/A3 deterministic lookup | Green | Yes |
| A3-open investigation | Green — 25 live burn-in cases, 0 false COMPLETE | Yes, monitored |
| A4 PatchRecipe proposal | Green in scoped cases | Yes, low-risk |
| A4 PatchIntent proposal | Green in scoped cases | Yes, low-risk |
| A5 isolated PatchRecipe apply | Green — VERIFIED, no workspace mutation | Yes, low-risk |
| A5 isolated PatchIntent apply | Green — VERIFIED, no workspace mutation | Yes, low-risk |
| A5 workspace apply | Pilot only | Not default |
| A6 critical-path simulation | Isolated only | Read/propose/simulate |
| Critical-path workspace apply | Not supported | No |
| Raw OSS free editing | Research | Not for serious work |

## What the runtime guarantees

### Investigations (A2/A3)

- Files inspected are scope-checked against allowed paths
- Secret content is detected and redacted
- Critical paths are blocked unless explicitly allowed
- Required evidence sources are tracked; missing evidence blocks COMPLETE
- Contradictory or blocked evidence escalates to GPT review
- Evidence shapes (function definitions, config values, test assertions, etc.) must match requirements
- Reports list answered/unanswered obligations, missing sources, blocked sources, contradictions

### Implementations (A4/A5)

- Patches validated: path policy, secret scan, git apply --check, base hash match
- Semantic review with structured reason codes (blocking, repairable, non-blocking)
- File roles classify changes as production source, test code, test fixture, docs, or config
- Coverage graph assesses target knowledge, source evidence, change intent, verification, risk, semantic quality
- A5 applies in isolated worktree — main workspace never mutated
- Rollback artifacts generated for every apply
- Verification commands must be mission-allowlisted

## Runtime model aliases

Runtime-backed agents use these aliases (maps to upstream model with autonomy profile):

| Alias | Upstream | Typical use |
|-------|----------|-------------|
| `mission-a2-flash` | DeepSeek V4 Flash | Fast deterministic lookups |
| `mission-a3-kimi` | Kimi K2.6 | Primary investigation |
| `mission-a3-deepseek` | DeepSeek V4 Pro | Deeper investigation reasoning |
| `mission-a3-flash` | DeepSeek V4 Flash | Support/fallback investigation |
| `mission-a4-kimi` | Kimi K2.6 | Patch proposal |
| `mission-a4-deepseek` | DeepSeek V4 Pro | Complex patch reasoning |
| `mission-a5-kimi` | Kimi K2.6 | Isolated implementation |
| `mission-a5-deepseek` | DeepSeek V4 Pro | Complex implementation |
| `mission-a6-kimi` | Kimi K2.6 | Critical-path simulation |

Raw (non-runtime) aliases: `ocg-kimi-k2.6`, `ocg-deepseek-v4-pro`, `ocg-deepseek-v4-flash`.

## CLI reference

```bash
# Setup
bin/codex-oss install [--project PATH] [--force]
bin/codex-oss doctor [--json] [--runtime-models] [--offline]
bin/codex-oss up --daemon
bin/codex-oss start [--port PORT]
bin/codex-oss stop
bin/codex-oss status

# Missions
bin/codex-oss mission template --tier A3
bin/codex-oss mission compile [20+ flags]
bin/codex-oss mission run [--project PATH]

# Audit and explain
bin/codex-oss audit-mission MISSION_ID [--project PATH] [--json]
bin/codex-oss explain MISSION_ID [--project PATH] [--json]
bin/codex-oss claim-status [--project PATH] [--json]

# Proof and certification
bin/codex-oss refresh-proofs --suite all [--project PATH] [--json]
bin/codex-oss burnin --suite operational [--project PATH] [--json]
bin/codex-oss certify --target open_investigation [--project PATH] [--json]

# Handoff validation
bin/codex-oss validate-handoff path/to/handoff.md
```

## Investigation flow (A3)

A MissionV1 contract compiled from a handoff:

```json
{
  "tier": "A3",
  "objective_style": "open_investigation",
  "answer_obligations": [{
    "question": "Where is run_loop defined?",
    "source_requirements": [{
      "path": "codex_oss/runtime/loop.py",
      "evidence_kind": "function_definition",
      "required_shapes": ["function_definition"]
    }]
  }],
  "must_inspect": ["codex_oss/runtime/loop.py"],
  "evidence_collection_mode": "agenda_guided",
  "exploration_policy": {
    "after_required_floor": "allow_model_exploration",
    "min_optional_actions_after_floor": 1,
    "require_contradiction_search": true
  }
}
```

The runtime:
1. Schedules through the read pool
2. Refreshes claim graph, answer graph, evidence agenda
3. Runs plan-act-observe loop with the OSS model
4. Enforces scope, redirects to pending sources, tracks evidence shapes
5. Closes from runtime answer graph or accepts model final report
6. Persists: report.json, claim_graph.json, answer_graph.json, coverage_graph.json, evidence_agenda.json, decision_trace.json, trace.jsonl

## Implementation flow (A4/A5)

```json
{
  "tier": "A5",
  "mode": "bounded_implementation",
  "apply_mode": "isolated_worktree",
  "objective_spec": {
    "objective_type": "documentation_patch",
    "target": {"required_changed_files": ["tests/fixtures/target.py"]}
  }
}
```

The runtime:
1. Validates the patch (path policy, secret scan, semantic review, coverage)
2. PatchRecipe: extracts anchor candidates, model selects anchor + content, runtime builds diff
3. PatchIntent: model returns structured intent, runtime builds diff
4. Applies in isolated worktree (never main workspace)
5. Runs mission-allowlisted verification
6. Generates rollback artifact
7. Produces: implementation_report.json, semantic_review.json, implementation_coverage_graph.json

## Evidence shape detection

The runtime detects code patterns in inspected files:

| Shape | Detects |
|-------|---------|
| `function_definition` | `def name(...):` |
| `class_definition` | `class Name...:` |
| `mapping_assignment` | `name = {...}` |
| `config_value` | `"key": value` entries |
| `flag_parameter` | `flag_*`, `allow_*`, `require_*` variables |
| `flag_read` | `.get("flag_*")` access patterns |
| `behavior_derivation` | `derive`, `observed behavior` keywords |
| `zero_match` | grep returning "0 matches" |
| `test_assertion` | `assert` statements |
| `verification_command` | pytest, unittest, verify references |

Required shapes can be specified per source requirement; missing shapes block COMPLETE.

## Exploration policy modes

| Mode | Behavior |
|------|----------|
| `prefetch_floor` | Runtime reads required sources upfront, closes from answer graph |
| `agenda_guided` | Runtime tells model pending sources, redirects if ignored, prefetches after threshold |
| `model_led` | No prefetch, minimal steering — experimental |

After the evidence floor is covered, `exploration_policy` controls what happens next:

| Policy | Effect |
|--------|--------|
| `close_immediately` | Force closure; no further exploration |
| `allow_model_exploration` | Bounded optional exploration with min/max actions and contradiction search |

## Explanation and audit

Every mission produces auditable artifacts. The decision explainer surfaces the full reasoning:

```bash
bin/codex-oss explain mission_open_blocked_source --project . --json
```

Output includes: status, closure source, phase path, evidence coverage, contradictions, blocked obligations, semantic review decisions, coverage gaps, and a human-readable summary of why the mission ended with its final status.

## Semantic review reason codes

Patches are reviewed with structured reason codes:

**Blocking:** `forbidden_path`, `critical_path_write`, `test_removal`, `function_deletion`, `full_file_rewrite`, `dependency_change`, `secret_introduced`, `objective_mismatch`, `patch_noop`, `patch_does_not_apply`

**Repairable:** `anchor_not_found`, `empty_change`, `missing_required_symbol`, `missing_required_test`, `verification_plan_missing`, `wrong_insertion_location`

**Non-blocking:** `formatting_change_detected`, `partial_objective_coverage`, `source_change_without_test`

File roles (`production_source`, `test_code`, `test_fixture`, `docs`, `config`) determine whether a source change requires a test change.

## Agent configuration

After install, your Codex project has these agents:

### Runtime-controlled (recommended)

| Agent | Model | Use |
|-------|-------|-----|
| `oss_kimi_investigator` | `mission-a3-kimi` | Primary investigation and structured patch |
| `oss_deepseek_investigator` | `mission-a3-deepseek` | Deeper investigation and implementation reasoning |
| `oss_flash_context` | `mission-a2-flash` | Fast context and fallback summarization |

### Raw experimental (research)

| Agent | Model |
|-------|-------|
| `oss_deepseek_pro` | `ocg-deepseek-v4-pro` |
| `oss_kimi_rapid` | `ocg-kimi-k2.6` |
| `oss_flash_support` | `ocg-deepseek-v4-flash` |

Raw agents use prompt-only discipline with broad tools. Runtime-controlled agents use MissionV1 contracts with governed tool access. Runtime-controlled is the product path; raw is research.

## Testing

```bash
# Core contract tests (no bridge needed)
python3 tests/test_runtime_contracts.py
python3 tests/test_patch_pipeline.py
python3 tests/test_mission_cli.py
python3 tests/test_mission_v1_http.py

# Claim surface check
bin/codex-oss claim-status --project . --json

# Opt-in live burn-in (requires running bridge + API key)
LIVE_BURNIN=1 python3 tests/test_a3_open_burnin_pack.py
LIVE_BURNIN=1 python3 tests/test_a5_ladder_burnin.py
LIVE_BURNIN=1 python3 tests/test_a4a5_burnin_pack.py
```

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `OPENCODE_GO_API_KEY` | OpenCode Go API key | Required |
| `PROXY_API_KEY` | Codex auth key for bridge | `sk-local-codex-bridge` |
| `MODEL_MAP_JSON` | Override model name mapping | Built-in map |
| `FALLBACK_MODEL_MAP_JSON` | Per-model fallback chains | Built-in chains |
| `GPT_MODEL_STRATEGY` | How to handle GPT requests | `error` |
| `GPT_MODEL_OSS_FALLBACK` | OSS model for GPT fallback | `deepseek-v4-pro` |
| `MAX_GLOBAL_UPSTREAM_CONCURRENCY` | Concurrency cap | `5` |
| `MODEL_CONCURRENCY_JSON` | Per-model concurrency caps | Built-in defaults |
| `UPSTREAM_STREAM` | Enable SSE streaming | `1` |
| `SSE_UPSTREAM_HEARTBEAT_SECONDS` | SSE keepalive interval | `15` |

## Supported upstream models

| Model ID | Status |
|----------|--------|
| `deepseek-v4-pro` | Tested |
| `deepseek-v4-flash` | Tested |
| `kimi-k2.6` | Tested |
| `kimi-k2.5` | Available |
| `qwen3.6-plus` | Available |
| `glm-5.1` | Available |
| `minimax-m2.7` | Available |
