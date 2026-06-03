# OpenCode Bridge

OpenCode Bridge lets Codex use open-source models as governed subagents. It routes OSS workers through a local Responses-compatible bridge, gives them scoped runtime tools, records what they did, and returns evidence-backed reports for GPT review.

Use it when you want cheaper or parallel OSS help without handing an OSS model the keys to your repo.

## What You Get

- **Runtime-controlled investigations**: OSS agents can read and search only the paths their mission allows.
- **Evidence-backed reports**: final answers include files inspected, commands run, findings, caveats, and confidence.
- **Visible progress**: spawned OSS agents stream safe working commentary such as planned actions, tool runs, coverage updates, and closure decisions.
- **Patch safety rails**: bounded implementation missions validate patches, apply them in an isolated worktree, verify them, and record rollback data.
- **Auditable artifacts**: every runtime mission writes JSON reports, traces, summaries, ledgers, and decision logs under `.codex-oss/missions/`.
- **Fail-closed behavior**: missing evidence, invalid reports, blocked paths, contradictions, and provider failures downgrade or escalate instead of pretending success.

## Who This Is For

OpenCode Bridge is for Codex users who want to delegate bounded work to OSS models while keeping GPT in charge of final judgment.

Good uses:

- Ask an OSS investigator to inspect a few files and summarize what it found.
- Run a low-risk repo scout in parallel while GPT continues the main task.
- Generate or validate a small patch in an isolated worktree.
- Burn in runtime behavior with deterministic proof packs.

Avoid it for:

- Authentication, authorization, migrations, recovery paths, CI gates, deployment, or schema authority.
- Any task where an OSS model's mistake could cause data loss or a security issue.
- Raw, open-ended "go change the repo" work.

## How It Works

```text
Codex / GPT orchestrator
  |
  | spawns a configured OSS subagent
  v
OpenCode Bridge on localhost:4000
  |
  | validates a MissionV1 contract
  v
Mission runtime
  |
  | owns tools, paths, evidence, validation, audit, and closure
  v
OSS model: Kimi, DeepSeek, or Flash
  |
  | returns actions or narrative under runtime control
  v
Evidence-backed report for GPT review
```

The important split is simple:

- **Runtime owns execution.** It decides which tools may run, checks scope, records evidence, validates reports, and closes safely.
- **OSS model owns reasoning.** It proposes the next action, explains why it wants that action, and drafts findings.
- **GPT owns judgment.** Treat OSS output as evidence, not final authority.

## Quick Start

### 1. Clone the Repo

```bash
git clone https://github.com/goldtetsola/opencode-bridge.git
cd opencode-bridge
```

### 2. Add Your OpenCode Go Key

Create the local env file:

```bash
mkdir -p .codex-oss/env
cp opencode-go.env.example .codex-oss/env/opencode-go.env
```

Edit `.codex-oss/env/opencode-go.env`:

```bash
OPENCODE_GO_API_KEY=sk-...
```

Do not commit this file.

### 3. Install the Bridge into a Codex Project

Run this from the bridge repo:

```bash
bin/codex-oss install --project /path/to/your/codex/project
```

This adds:

- a local `opencode_bridge` provider block
- runtime-controlled OSS agent TOMLs
- raw OSS agent TOMLs for experiments
- safety rules for subagent handoffs

### 4. Start the Bridge

```bash
bin/codex-oss up --daemon
```

The bridge listens on:

```text
http://127.0.0.1:4000/v1
```

### 5. Check Your Setup

```bash
bin/codex-oss doctor --network
```

For JSON output:

```bash
bin/codex-oss doctor --network --json
```

### 6. Check the Current Claim Surface

```bash
bin/codex-oss claim-status --project . --json
```

This tells you which runtime claims are currently supported by fresh artifacts.

## The Most Important Rule

Do **not** set `model_provider = "opencode_bridge"` as your top-level Codex provider.

Keep GPT native as the parent session. Only OSS subagent TOMLs should use the bridge provider.

Why: GPT requests must stay with OpenAI. The bridge is for OSS workers and MissionV1 runtime aliases.

## Agents

After installation, your Codex project can use these agents.

### Runtime-Controlled Agents

Use these for normal work.

| Agent | Model alias | Best for |
|---|---|---|
| `oss_kimi_investigator` | `mission-a3-kimi` | General read-only investigations |
| `oss_deepseek_investigator` | `mission-a3-deepseek` | Deeper read-only investigations |
| `oss_flash_context` | `mission-a2-flash` | Cheap context gathering and lookups |
| `oss_deepseek_implementer` | `mission-a5-deepseek` | Bounded implementation through MissionV1 A4/A5 |

Runtime-controlled agents require a MissionV1 handoff:

```text
<OSS_HANDOFF_JSON>
{ ... MissionV1 JSON ... }
</OSS_HANDOFF_JSON>
```

Put exactly one of these blocks in the worker prompt you spawn. Do not copy literal `<OSS_HANDOFF_JSON>` examples into project `AGENTS.md` files; every spawned worker inherits those standing instructions, and inherited examples can be mistaken for extra handoff blocks.

Use `fork_turns: "none"` or the equivalent "do not fork context" option when spawning OSS agents. Full-history forks can inherit GPT settings that conflict with OSS model routing.

### Raw OSS Agents

These are useful for experiments and low-stakes support work, but they do not get the full MissionV1 runtime control plane. `oss_deepseek_pro` can perform bounded low-risk implementation when the handoff declares owned paths and verification. Use `oss_deepseek_implementer` with MissionV1 A4/A5 when you want runtime-owned patch validation, apply, verification, rollback, and final status.

| Agent | Model |
|---|---|
| `oss_kimi_rapid` | `ocg-kimi-k2.6` |
| `oss_deepseek_pro` | `ocg-deepseek-v4-pro` |
| `oss_flash_support` | `ocg-deepseek-v4-flash` |

For serious read-only investigations, prefer the runtime-controlled agents.
For higher-assurance implementation, runtime owns patch construction, apply, verification, rollback, and final status. The model owns narrative, patch intent, and rationale only.

Raw workers cannot prove their own routing from inside the task. Treat their files inspected, findings, and caveats as their deliverable. Treat bridge routing and live OSS inference as bridge-owned facts, proven by:

```bash
bin/codex-oss status
bin/codex-oss doctor --live-model
bin/codex-oss smoke native-polish
```

In `doctor --live-model`, `bridge.oss_inference` is the check that proves the bridge can reach a live OSS model. A raw worker may report its configured model alias, but that is configuration context, not independent proof of transport.

`smoke native-polish` checks the user-facing surface: the runtime implementation agent is installed, raw writes demote safely, A5 can apply and verify an isolated patch, visible commentary is recorded, and the main workspace stays unchanged.

## Ways To Delegate

The runtime has internal labels because different tasks need different safety rules. You do not need to memorize them.

| Plain name | Internal label | What it does | Current use |
|---|---|---|
| Quick lookup | A2 | Fast read/search/extract tasks | Use today |
| Read-only investigation | A3 | Scoped investigation with evidence requirements | Use today |
| Open-ended investigation | A3-open | Ambiguous read-only questions with stricter evidence checks | Use today, monitored |
| Patch proposal | A4 | Build a proposed patch without applying it to the workspace | Use for low-risk patches |
| Isolated implementation | A5 | Apply and verify a bounded patch in an isolated worktree | Use for bounded low-risk work |
| Critical-path rehearsal | A6 | Simulate high-risk work without trusting OSS to change critical paths | Proof/simulation only |
| Raw OSS worker | Raw OSS | Prompt-only worker behavior without the full runtime control plane | Research lane |

## A Minimal MissionV1 Example

Use this shape when asking a runtime-controlled investigator to inspect a file:

```text
<OSS_HANDOFF_JSON>
{
  "schema_version": "oss_agent_mission.v1",
  "mission_id": "mission_find_runtime_streaming",
  "tier": "A3",
  "mode": "managed_investigation",
  "objective": "Inspect bridge.py and report where managed runtime streaming starts.",
  "risk_tier": "low",
  "write_allowed": false,
  "allowed_roots": [],
  "allowed_paths": ["bridge.py"],
  "tool_budget": 3,
  "time_budget_seconds": 60,
  "allowed_tool_classes": ["read", "search"],
  "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
  "report_schema": "managed_investigation_report.v1",
  "required_outputs": [
    "files_inspected",
    "commands_run",
    "findings",
    "uncertainties",
    "confidence",
    "caveats",
    "escalation_recommendation"
  ]
}
</OSS_HANDOFF_JSON>
```

For reusable handoffs, validate before sending:

```bash
bin/codex-oss validate-handoff path/to/handoff.md
```

For generated mission files, prefer the CLI:

```bash
bin/codex-oss mission template --tier A3
bin/codex-oss mission compile mission.json --handoff
```

## Visible Progress

Runtime-backed OSS agents now stream public progress commentary. This is not hidden chain-of-thought. It is safe working narration generated from runtime state and model-declared action rationale.

You should see updates like:

```text
I'm loading the compiled MissionV1 contract.
I'm asking the OSS model for the next safe investigation action.
The model chose rtk_read on bridge.py as the next investigation step.
I'm running rtk_read on bridge.py.
I finished rtk_read; exit_code=0. The runtime refreshed coverage from the new evidence.
Current phase is REPORT. Evidence refs now available: file:bridge.py#extract:1, command:0.
```

The runtime does **not** stream:

- raw hidden chain-of-thought
- provider `reasoning_content`
- full file contents
- long command output
- secrets or tokens
- system/developer prompts

If the UI does not show progress, inspect the artifact:

```bash
bin/codex-oss show-trace MISSION_ID
bin/codex-oss show-summary MISSION_ID
```

## Source-Pack Recovery

For direct read-only support agents, the bridge first tries to let the OSS model run a normal live agent loop: inspect sources, gather evidence, and produce the final answer. If the model times out, loops, returns intent text, or produces an invalid final report, the bridge may use a gathered source pack to produce an explicit recovery report.

Recovery reports say:

```text
Synthesis status: SOURCE_PACK_RECOVERY
```

That means the bridge preserved useful evidence, but the live OSS synthesis did not complete cleanly. Treat it as a recovery artifact for GPT review, not as a native-feeling successful OSS answer.

## Mission Artifacts

Each runtime mission writes artifacts under:

```text
.codex-oss/missions/<mission_id>/
```

Common files:

| File | Purpose |
|---|---|
| `report.json` | Final structured report |
| `summary.md` | Human-readable mission summary |
| `visible_commentary.jsonl` | User-facing progress events |
| `trace.jsonl` | Runtime action trace |
| `decision_trace.json` | Policy and closure decisions |
| `ledger.json` | Files inspected, commands run, budget, redaction state |
| `claim_graph.json` | Investigation claim/evidence state |
| `answer_graph.json` | Open-investigation obligation state, when applicable |
| `implementation_report.json` | Patch/apply/verify result for A4/A5 |
| `semantic_review.json` | Patch review result for implementation missions |

Audit a mission:

```bash
bin/codex-oss audit-mission MISSION_ID --project . --json
```

Explain why a mission ended the way it did:

```bash
bin/codex-oss explain MISSION_ID --project . --json
```

## Safety Model

The runtime blocks or downgrades work when evidence is weak.

For investigations:

- allowed paths and roots are enforced
- secrets are redacted before returning observations to the model
- critical paths are blocked unless explicitly allowed
- missing required sources block `COMPLETE`
- unsupported tool arguments are rejected or repaired
- duplicate reads are suppressed or served from cached evidence
- contradictions and blocked sources escalate to GPT review
- hollow `COMPLETE` reports are repaired or downgraded

For implementation:

- patches are validated before apply
- raw model diffs are not trusted blindly
- PatchRecipe and PatchIntent can be turned into runtime-built diffs
- `git apply --check` runs before apply
- secret scans and semantic review run before apply
- A5 applies in an isolated worktree by default
- verification commands must be allowlisted
- rollback artifacts are recorded

## CLI Reference

### Setup and Health

```bash
bin/codex-oss install --project PATH
bin/codex-oss up --daemon
bin/codex-oss start --port 4000
bin/codex-oss stop
bin/codex-oss restart --port 4000
bin/codex-oss status
bin/codex-oss doctor --network
bin/codex-oss smoke native-polish
```

### Missions and Artifacts

```bash
bin/codex-oss mission template --tier A3
bin/codex-oss mission compile --help
bin/codex-oss mission run --project .
bin/codex-oss validate-handoff path/to/handoff.md
bin/codex-oss show-trace MISSION_ID
bin/codex-oss show-summary MISSION_ID
bin/codex-oss audit-mission MISSION_ID --project . --json
bin/codex-oss explain MISSION_ID --project . --json
```

### Proof and Certification

```bash
bin/codex-oss claim-status --project . --json
bin/codex-oss refresh-proofs --project . --suite all --json
bin/codex-oss burnin --project . --suite operational --json
bin/codex-oss certify --project . --target open_investigation --json
```

### Raw Research Lane

```bash
bin/codex-oss raw-probe --help
bin/codex-oss raw-claim-status --project . --json
```

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `OPENCODE_GO_API_KEY` | OpenCode Go API key for upstream OSS models | Required |
| `PROXY_PORT` | Local bridge port | `4000` |
| `PROXY_API_KEY` | Local auth key expected by the bridge | `sk-local-codex-bridge` |
| `GPT_MODEL_STRATEGY` | What to do if a GPT request hits the bridge | `error` |
| `MODEL_MAP_JSON` | Override bridge model mapping | built-in map |
| `FALLBACK_MODEL_MAP_JSON` | Override fallback chains | built-in chains |
| `UPSTREAM_STREAM` | Use upstream streaming where supported | `1` |
| `OSS_VISIBLE_TRACE` | Visible commentary mode: `off`, `summary`, `detailed` | `summary` |
| `OSS_VISIBLE_TRACE_STREAM` | Stream visible commentary to Codex | `1` |
| `OSS_VISIBLE_TRACE_MAX_EVENTS` | Max visible commentary events per mission | `40` |
| `REQUEST_DEADLINE_SECONDS` | Default request deadline | `90` |
| `MAX_GLOBAL_UPSTREAM_CONCURRENCY` | Global upstream request cap | `5` |
| `MODEL_CONCURRENCY_JSON` | Per-model concurrency caps | built-in defaults |

## Local Development

This is a Python project with no required package install for the core test suite.

Run core tests:

```bash
python3 tests/test_runtime_contracts.py
python3 tests/test_burnin_harness.py
python3 tests/test_patch_pipeline.py
python3 tests/test_mission_cli.py
```

Run the bridge in the foreground:

```bash
bin/codex-oss up
```

Run the bridge as a daemon:

```bash
bin/codex-oss up --daemon
```

Restart after code changes:

```bash
bin/codex-oss restart --port 4000
```

Verify the running process matches the source on disk:

```bash
bin/codex-oss doctor --network --json
```

## Project Structure

```text
agents/                 Codex agent TOMLs for runtime and raw OSS workers
bin/codex-oss           Main CLI entry point
bridge.py               Responses-compatible bridge server
codex_oss/              Runtime, policy, mission, audit, and implementation code
docs/                   Optional local notes and generated docs (ignored by git)
examples/               Example inputs and handoffs
orchestration/          Supporting orchestration files
tests/                  Runtime, bridge, mission, and burn-in tests
config.toml.example     Provider config to merge into Codex config
opencode-go.env.example API key env template
```

Key modules:

| Module | Role |
|---|---|
| `codex_oss/managed_bridge.py` | Turns Responses requests into MissionV1 runtime runs |
| `codex_oss/runtime/loop.py` | A2/A3 plan-act-observe loop and visible commentary |
| `codex_oss/answer_graph.py` | Open-investigation obligations and sufficiency |
| `codex_oss/implementation.py` | A4/A5 patch proposal, validation, apply, verify |
| `codex_oss/visible_commentary.py` | Safe user-facing progress events |
| `codex_oss/audit.py` | Mission artifact checks |
| `codex_oss/transport/emitter.py` | Responses JSON/SSE output |

## Troubleshooting

### Bridge is not running

Run:

```bash
bin/codex-oss up --daemon
bin/codex-oss doctor --network
```

### Bridge is running stale code

Restart it:

```bash
bin/codex-oss restart --port 4000
```

Then confirm:

```bash
bin/codex-oss doctor --network --json
```

Look for:

```text
bridge.source_hash: PASS
runtime.identity: PASS
```

### Doctor says the bridge is serving another project

If `bridge.project_root` points at another repo, a different bridge supervisor still owns the port. Restart from the repo you want to serve:

```bash
bin/codex-oss restart --port 4000
bin/codex-oss doctor --network
```

Current versions clear the port owner during restart so an old source-checkout supervisor cannot quietly serve a target project.

### Runtime agent says MissionV1 is missing

Runtime-controlled agents need exactly one tagged block:

```text
<OSS_HANDOFF_JSON>
{ "schema_version": "oss_agent_mission.v1", ... }
</OSS_HANDOFF_JSON>
```

The older lightweight `OSS_HANDOFF_JSON:` handoff is for raw/legacy delegation, not MissionV1 runtime agents.

If this fails with "Multiple OSS_HANDOFF_JSON blocks", check the project's `AGENTS.md`. Standing repo instructions should describe the handoff rule, but they should not contain literal `<OSS_HANDOFF_JSON>` examples.

### Codex routes GPT through the bridge

Remove any session-wide setting like:

```toml
model_provider = "opencode_bridge"
```

Use the bridge provider only inside OSS agent TOMLs.

### No live commentary appears

First check the artifact:

```bash
bin/codex-oss show-trace MISSION_ID
```

Then check streaming config:

```bash
echo "$OSS_VISIBLE_TRACE"
echo "$OSS_VISIBLE_TRACE_STREAM"
```

Expected defaults:

```text
OSS_VISIBLE_TRACE=summary
OSS_VISIBLE_TRACE_STREAM=1
```

If `commentary_delivery.json` shows `sse_emitted > 0` but `consumer_observed=0`,
the bridge produced progress but the spawned-agent consumer did not render it.
Current final reports also include a `Delivery status:` line so this is visible
even when the parent UI only receives the terminal report.

Before treating Desktop live commentary as a product claim, run the stripped-down
renderer probe. It removes MissionV1, tools, upstream models, and bridge
fallbacks from the question:

```bash
bin/codex-oss desktop-pre-final-text-probe self-test
bin/codex-oss desktop-pre-final-text-probe config --port 43211
bin/codex-oss desktop-pre-final-text-probe server --port 43211
```

Then start a fresh Codex Desktop session if the agent list was already loaded,
and spawn the dedicated `desktop_pre_final_text_probe` child/subagent. It uses
the printed provider config and should not use tools. Record the observed
result:

```bash
bin/codex-oss desktop-pre-final-text-probe record \
  --status pass \
  --observed-progress-before-final true \
  --observed-final true \
  --notes "PROGRESS_ONE/TWO/THREE appeared before FINAL_DONE"
```

Interpretation:

- `pass`: Desktop rendered ordinary child assistant text before final; Desktop
  Gold can use the observation gate.
- `fail`: only final appeared; Desktop live-commentary claims are disallowed.
- `flaky`: treat as best-effort only.
- `setup_failed`: rerun the probe; do not classify Desktop rendering.

There is also a second progress surface: Codex app-server. It exposes
structured notifications such as `item/agentMessage/delta`, which can carry
pre-final assistant text without depending on the spawned-child renderer:

```bash
bin/codex-oss app-server-pre-final-text-probe --json
```

If this returns `probe_status: pass`, app-server can be used as an owned
native-feeling progress surface. That does not by itself prove the existing
Desktop spawned-child view renders pre-final text; it proves a separate Codex
event channel can.

Codex Desktop or the multi-agent consumer should feed the child Responses SSE
through the Desktop consumer adapter:

```bash
bin/codex-oss desktop-observation from-sse \
  --mission-id MISSION_ID \
  --sse child-response.sse \
  --agent-id AGENT_ID \
  --agent-name AGENT_NAME \
  --output .codex-oss/missions/MISSION_ID/desktop_transcript.json

bin/codex-oss desktop-observation reconcile \
  --mission-dir .codex-oss/missions/MISSION_ID \
  --transcript .codex-oss/missions/MISSION_ID/desktop_transcript.json
```

Only raw Desktop-spawned transcripts can mark progress as observed. Bridge
harness transcripts and artifact-reconstructed traces still fail Desktop Gold.

### Provider auth fails

Check the key file:

```bash
python3 - <<'PY'
from pathlib import Path
path = Path(".codex-oss/env/opencode-go.env")
print("env file present:", path.exists())
print("contains OPENCODE_GO_API_KEY:", "OPENCODE_GO_API_KEY=" in path.read_text() if path.exists() else False)
PY
```

Do not paste the key into issues, logs, or chat.

## Current Status

The runtime-backed path is the supported product path for bounded OSS delegation. Raw OSS editing remains a research lane.

Use `claim-status`, `audit-mission`, and `doctor` when you need proof rather than vibes:

```bash
bin/codex-oss claim-status --project . --json
bin/codex-oss audit-mission MISSION_ID --project . --json
bin/codex-oss doctor --network --json
```

## License

See [LICENSE](LICENSE).
