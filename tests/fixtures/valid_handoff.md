OSS_HANDOFF_JSON:
{
  "schema_version": 1,
  "role": "Read-only scout",
  "goal": "Find evidence for a deterministic bridge test",
  "task_type": "read-only scout",
  "owned_paths": [],
  "read_only_paths": ["tests/fixtures"],
  "forbidden_actions": ["Do not edit files"],
  "verification_steps": ["Search for terminal_blocker_state"],
  "deliverable_fields": ["confidence", "evidence"],
  "completion_rule": "Stop after the report",
  "escalation_rule": "Stop on active side effects"
}
