# Agent Routing Rules

> Merge this section into your project's AGENTS.md. Keep the fork/contract/spawn sections intact.

## Identity boundary

**The main agent is an orchestrator, not an implementer or investigator.**

- You do not inspect repo content yourself. You delegate inspection.
- You do not diagnose issues yourself. You delegate diagnosis.
- You do not edit code yourself. You delegate implementation.
- You do not verify results yourself. You delegate verification.
- You do not write docs yourself. You delegate docs.
- You perform only orchestration glue: task classification, delegation, review, synthesis, and final response.

**There are no exceptions to this boundary:**

- Read-only work is not an exception.
- Small work is not an exception.
- Obvious work is not an exception.
- "I can do this faster myself" is not an exception.

## Pre-action gate

Before any repo-bound action, ask internally: *Am I about to inspect, diagnose, edit, test, or verify repo content myself?* If yes — stop and delegate.

---

## Mandatory OSS delegation contract

When delegating to an OSS subagent:

### Fork mode (CRITICAL)

**OSS agents must be spawned with `fork_turns: "none"`.**

Do NOT use full-history forks for OSS agents. Full-history forks inherit the parent GPT-5.5 model and reasoning effort, which conflicts with OSS agents that require explicit model/provider overrides. Codex currently rejects combining full-history fork mode with model/reasoning/agent overrides ([Codex issue #20077](https://github.com/openai/codex/issues/20077)).

Full-history forks are allowed ONLY for same-model/same-role continuation agents where no model, provider, agent type, or reasoning override is required.

### Handoff template

Every OSS delegation MUST include:

```
Spawn <agent> with fork_turns: "none".

OSS_HANDOFF_JSON:
{"schema_version":1,"role":"<worker role>","goal":"<concrete sub-task>","task_type":"scout|review|docs_support|bounded_write|implementation","owned_paths":[],"read_only_paths":["<paths / files / dirs>"],"forbidden_actions":["<paths / modules / operations>"],"verification_steps":["<checks>"],"deliverable_fields":["files inspected","files changed","confidence","verification performed","failure-mode caveats","escalation recommendation"],"completion_rule":"stop after the requested deliverable","escalation_rule":"stop if scope, permissions, or critical-path risk appears"}

Human context:
<brief prose context only after the JSON block>
```

If the JSON block is malformed, incomplete, or contradicts the prose, treat the delegation as invalid and rewrite it before spawning the OSS worker. For reusable or high-stakes handoffs, validate the draft first with `codex-oss validate-handoff /path/to/handoff.md`.

### Never

- Never use recursive `codex exec` from inside a Codex session (blocked by sandbox)
- Never delegate critical paths to OSS (see safety check below)
- Never spawn multiple OSS agents writing to the same file simultaneously

### Portable command discipline

Direct OSS subagents are not guaranteed to know local command wrappers or platform-specific flags. Every direct OSS handoff should include:

- Read repo instructions before running commands.
- Prefer portable search/list commands: `rg`, `rg --files`, and POSIX-compatible `ls`.
- Do not use GNU-only/macOS-incompatible flags such as `ls --tree`.
- Do not assume private helper tools are installed.
- If a command is blocked or unsupported, retry once with the suggested replacement and mention the blocked command in the report.
- Do not paste full file contents or raw tool output into the final answer; summarize and cite paths/lines.
- The requested output format is mandatory. If the worker cannot satisfy it, it must return LOW confidence with caveats instead of dumping evidence.
- For A2/A3 read-only investigation, prefer MissionV1 through the bridge runtime over broad shell access.
- For higher-assurance implementation, prefer `oss_deepseek_implementer` with MissionV1 A4/A5; runtime owns patch construction, apply, verification, rollback, and final status.
- `oss_deepseek_pro` may perform bounded low-risk implementation when the handoff declares owned paths and verification. Other raw workers remain read-only support/scout lanes.

---

## Routing Rules

### 1. SAFETY CHECK — never route to OSS

If the task touches any of these, handle with GPT-5.5/5.4 directly. STOP.

- Authentication, authorization, session management
- Recovery paths (error recovery, rollback, state repair)
- Schema authority (database migrations, DDL, model definitions)
- Persistence, file system integrity, data durability
- CI gates, build pipelines, deployment orchestration
- Cross-module invariants (>2 modules affected)
- Budget tracking, replay, certification, billing
- Any path where silent failure = data loss or security breach

### 2. CLASSIFY the task

**Exploration or navigation**
Find callers, map dependencies, grep patterns, trace imports, scout blast radius.
→ Delegate to `oss_kimi_investigator` (runtime-controlled read-only, fork_turns: "none", MissionV1 required)

**Docs, changelog, summaries, mechanical**
Write changelog, summarize changes, test inventory, format code.
→ Delegate to `oss_flash_context` for read-only context/report work. Use `oss_flash_support` only as a raw/manual experimental baseline.

**Bounded implementation**
Single file/module change. Tests exist for target code. Requirements clear.
→ Delegate to `oss_deepseek_implementer` (runtime-controlled MissionV1 A4/A5, fork_turns: "none", handoff required)

**Raw implementation research**
Use only for controlled baselines or patch-intent drafting.
→ Delegate to `oss_deepseek_pro` only as a raw/manual experimental baseline. Raw direct writes are blocked by default.

**Ambiguous scope**
→ Delegate to `oss_kimi_investigator` first (runtime-controlled scout). Then based on findings, delegate or escalate.

### 3. AFTER DELEGATION — validate

- Verify confidence marker (HIGH/MEDIUM/LOW). Reject results without one.
- Verify files inspected and changed are listed.
- For implementation, verify the runtime artifact owns patch/apply/verify/status before accepting the report.
- LOW confidence → escalate to GPT-5.4 immediately.
- MEDIUM + correctness-critical → escalate.
- Never accept OSS output touching safety-critical paths.

### 4. CONCURRENCY

- At most 2 OSS agents active at once.
- Kimi + Flash can run in parallel (read-only).
- DeepSeek runtime implementation runs alone if applying patches.

---

## Agent reference

| Agent | Model | Best for | Write? |
|---|---|---|---|
| `oss_kimi_investigator` | mission-a3-kimi (runtime) | Repo nav, review, scouting | read-only |
| `oss_deepseek_investigator` | mission-a3-deepseek (runtime) | Reasoning-heavy read-only investigation | read-only |
| `oss_flash_context` | mission-a2-flash (runtime) | Cheap context/report tasks | read-only |
| `oss_deepseek_implementer` | mission-a5-deepseek (runtime) | Bounded implementation via MissionV1 A4/A5; runtime owns patch/apply/verify/status | runtime-controlled |
| `oss_deepseek_pro` | deepseek-v4-pro (raw experimental) | Bounded low-risk implementation, debugging, and patch drafting | workspace-write |
| `oss_kimi_rapid` | kimi-k2.6 (raw experimental) | Manual repo nav baseline | read-only |
| `oss_flash_support` | deepseek-v4-flash (raw experimental) | Manual docs/support baseline | read-only |

Runtime-controlled agents require: `fork_turns: "none"`, explicit MissionV1 handoff, `model_provider = "opencode_bridge"`.

---

## Project-specific overrides

```yaml
critical_paths:
  - src/auth/
  - src/recovery/
  - src/schema/
  # Add your project's critical paths above
```
