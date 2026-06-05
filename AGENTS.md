
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

### Handoff templates

Spawn <oss_agent> with fork_turns: "none".

For Codex Desktop runtime-controlled OSS roles (`oss_kimi_investigator`, `oss_deepseek_investigator`, `oss_flash_context`, `oss_deepseek_implementer`), send a native human-readable task contract to the child. The bridge synthesizes and validates MissionV1 internally; do not expose `OSS_HANDOFF_JSON` to the child unless you are intentionally testing the raw handoff parser:

```
ROLE: <worker role>.
MISSION ID: <stable mission id>
GOAL: <concrete sub-task>.
TASK TYPE: scout|review|docs_support|bounded_write|implementation.
OWNED PATHS: <paths or none>.
READ-ONLY PATHS: <files or dirs>.
DO NOT TOUCH: <paths or operations>.
VERIFICATION STEPS: <checks>.
DELIVERABLE: <fields>.
COMPLETION RULE: Clear result only.
ESCALATION RULE: Stop if scope or critical-path risk appears.
```

For compiled handoff files, CLI validation, or raw parser tests, use a canonical MissionV1 machine-readable handoff block before prose. Prefer generating it with `codex-oss mission compile --handoff`; the bridge validates this before trusting task routing:

<OSS_HANDOFF_JSON>
{
  "schema_version": "oss_agent_mission.v1",
  "mission_id": "<stable mission id>",
  "tier": "A3",
  "mode": "managed_investigation",
  "objective": "<concrete sub-task>",
  "risk_tier": "low",
  "write_allowed": false,
  "allowed_roots": [],
  "allowed_paths": ["<files or dirs>"],
  "read_only_paths": ["<files or dirs>"],
  "owned_paths": [],
  "forbidden_roots": [],
  "allowed_tool_classes": ["read", "search", "list", "safe_git"],
  "tool_budget": 10,
  "time_budget_seconds": 90,
  "objective_style": "open_investigation",
  "answer_obligations": [
    {"id": "files", "question": "Which files were inspected?"},
    {"id": "evidence", "question": "What evidence supports the answer?"},
    {"id": "caveats", "question": "What uncertainty remains?"}
  ],
  "required_outputs": [
    "files_inspected",
    "commands_run",
    "findings",
    "uncertainties",
    "confidence",
    "caveats",
    "escalation_recommendation"
  ],
  "must_inspect": ["<primary file or dir>"],
  "evidence_collection_mode": "agenda_guided",
  "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"]
}
</OSS_HANDOFF_JSON>

After the JSON block, add any human-readable context needed for the worker. For reusable or high-stakes handoffs, validate the draft first with `codex-oss validate-mission-handoff /path/to/handoff.md`.

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
