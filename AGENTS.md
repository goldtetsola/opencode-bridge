
<!-- codex-oss:start -->
## OSS delegation

Use OSS agents for bounded low/medium-risk work only. Use runtime-controlled MissionV1 OSS agents for native-feeling A2/A3 read-only investigation and higher-assurance A4/A5 implementation. Raw direct OSS agents are support/research lanes only; they must not be treated as Codex Desktop-native candidates.

For implementation work:
- Prefer `oss_deepseek_implementer` with a MissionV1 A4/A5 handoff when you need runtime-owned patch control.
- Use `oss_deepseek_pro` only as a raw direct bounded implementation/drafting lane when the scope is low-risk, owned paths are explicit, and external verification is clear.
- Runtime owns patch construction, apply, verification, rollback, and final status.
- The model owns narrative, patch intent, and rationale only.
- Native-feeling product claims require MissionV1 runtime authority. Raw direct agents (`oss_deepseek_pro`, `oss_kimi_rapid`, `oss_flash_support`) are `raw_research_only` for public claims and cannot certify Desktop-native behavior.
- Desktop Gold requires `ConsumerObservationWitnessV1` from a raw Codex Desktop spawned-agent transcript with observed progress before final; bridge/runtime artifacts, transcript hashes, artifact-reconciled candidates, and terminal-only notifications are not Desktop-native proof.

When spawning OSS agents, always use fork_turns: "none":
- Full-history forks inherit GPT-5.5 model/reasoning, which conflicts with OSS agent overrides.
- OSS agents use different model providers and must not inherit the parent session.

### Handoff template

Spawn <oss_agent> with fork_turns: "none".

Include a machine-readable handoff block before prose. The bridge validates this before trusting task routing:

OSS_HANDOFF_JSON:
{"schema_version":1,"role":"<worker role>","goal":"<concrete sub-task>","task_type":"scout|review|docs_support|bounded_write|implementation","owned_paths":[],"read_only_paths":["<files or dirs>"],"forbidden_actions":["<paths or operations>"],"verification_steps":["<checks>"],"deliverable_fields":["files inspected","confidence","caveats","escalation recommendation"],"completion_rule":"stop after the requested deliverable","escalation_rule":"stop if scope or critical-path risk appears"}

After the JSON block, add any human-readable context needed for the worker. For reusable or high-stakes handoffs, validate the draft first with `codex-oss validate-handoff /path/to/handoff.md`.

### Critical paths (never route to OSS)
- Authentication, authorization, session management
- Recovery paths, error recovery, state repair
- Schema authority, database migrations
- CI gates, build pipelines, deployment
- Cross-module invariants (>2 modules)
- Any path where failure = data loss or security breach

### Never
- Never use recursive codex exec from inside a Codex session.
- Never set model_provider = "opencode_bridge" as the parent session provider.
- Never delegate critical paths to OSS agents.

### Portable command discipline
- Read the repo instructions before running commands.
- Prefer portable search/list commands: `rg`, `rg --files`, and POSIX-compatible `ls`.
- Do not use GNU-only/macOS-incompatible flags such as `ls --tree`.
- Do not assume private helper tools are installed.
- If a command is blocked or unsupported, retry once with the suggested replacement and mention the blocked command in the report.
- Do not paste full file contents or raw tool output into the final answer; summarize and cite paths/lines.
- The requested output format is mandatory. If you cannot satisfy it, return LOW confidence with caveats instead of dumping evidence.
- For A2/A3 read-only investigation, prefer MissionV1 through the bridge runtime over broad shell access.
- For higher-assurance implementation, compile a MissionV1 A4/A5 handoff with `codex-oss mission compile`.
<!-- codex-oss:end -->
