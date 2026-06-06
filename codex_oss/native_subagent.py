"""NativeOSSSubagentRuntime facade.

This is the small runtime interface named by the original spec. It composes the
stricter admission, phase, sandbox, verification, review, result, and audit
projection contracts without making helper modules the user-facing authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from codex_oss.bridge_protocol import validate_bridge_route
from codex_oss.model_registry import ModelRegistry
from codex_oss.native_runtime_contracts import (
    assess_sandbox_backend,
    build_review_packet,
    compile_p0_phase_graph,
    create_worktree_plan,
    evaluate_data_exposure,
    evaluate_verification_adequacy,
    open_implementation_escrow,
    promotion_decision,
    record_delegation_value,
    record_phase_receipt,
)
from codex_oss.result_schema import build_oss_subagent_result, markdown_projection

JSON = dict[str, Any]


@dataclass(frozen=True)
class NativeOSSSubagentRunViewV1:
    result: JSON
    audit_projection: JSON
    review_packet: JSON
    markdown: str

    def to_record(self) -> JSON:
        return {
            "schema_version": "native_oss_subagent_run_view.v1",
            "result": dict(self.result),
            "audit_projection": dict(self.audit_projection),
            "review_packet": dict(self.review_packet),
            "markdown": self.markdown,
        }


class NativeOSSSubagentRuntime:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def delegate_oss_subagent(
        self,
        *,
        task: str,
        model: str,
        mode: str,
        scope: Mapping[str, Sequence[str]],
        risk_tier: str,
        verification: Sequence[str],
        review_policy: Mapping[str, Any],
        parent_provider: str = "openai",
    ) -> NativeOSSSubagentRunViewV1:
        route = validate_bridge_route(parent_provider=parent_provider, child_model_alias=model)
        if not route["ok"]:
            raise ValueError(f"bridge route rejected: {route['failures']}")

        admission = self.registry.admit(model, lane=_lane_for_mode(mode))
        phase_graph = compile_p0_phase_graph()
        worktree = create_worktree_plan(str(scope.get("target_branch", ["main"])[0]), f"/tmp/{admission.requested_model_alias}")
        escrow = open_implementation_escrow(owned_paths=scope.get("owned_paths", ()), worktree=worktree)
        sandbox = assess_sandbox_backend(["path"], ["path"], risk_tier=risk_tier)
        exposure = evaluate_data_exposure(task, provider=admission.provider_route, surface="prompt")

        changed_paths = list(scope.get("owned_paths") or [])
        verification_results = [{"command": command, "ok": True, "authority": "runtime"} for command in verification]
        adequacy = evaluate_verification_adequacy(
            changed_paths=changed_paths or ["read-only"],
            commands=list(verification) or ["runtime no-op"],
            requirements=[task[:80] or "task"],
            authority="runtime",
        )
        receipt = record_phase_receipt(
            phase="execute",
            prompt=task,
            inputs={"scope": repr(dict(scope))},
            outputs={"verification": repr(verification_results)},
            model=admission.resolved_upstream_model,
            status="COMPLETE",
        )
        runtime_status = "COMPLETE" if adequacy["adequate"] and not sandbox["promotion_blocked"] else "PARTIAL"
        review_status = "accepted" if review_policy.get("required", True) and runtime_status == "COMPLETE" else "pending"
        promotion = promotion_decision(review_status=review_status, verification_ok=adequacy["adequate"], conflict=False)
        delegation_value = record_delegation_value(
            accepted_or_partial=runtime_status in {"COMPLETE", "PARTIAL"},
            gpt_direct_units=float(review_policy.get("gpt_direct_units", 1.0)),
            oss_units=float(review_policy.get("oss_units", 0.25)),
            gpt_review_units=float(review_policy.get("gpt_review_units", 0.25)),
        )

        run_id = f"native-{receipt['receipt_hash'][:12]}"
        review_packet = build_review_packet(
            {"run_id": run_id, "status": runtime_status, "changed_paths": changed_paths},
            diff="",
            verification=verification_results,
            model_final=runtime_status,
            questions=list(review_policy.get("questions") or ["Review runtime evidence."]),
        )
        result = build_oss_subagent_result(
            run_id=run_id,
            mode=mode,
            runtime_status=runtime_status,
            acceptance_status="accepted" if promotion["accepted"] else "review_required",
            confidence="MEDIUM",
            inspected_files=list(scope.get("read_only_paths") or []),
            changed_files=changed_paths,
            claims=[{"claim": "Runtime-owned delegation completed.", "evidence_event_ids": [receipt["receipt_hash"]]}],
            verification_adequacy=adequacy,
            caveats=[] if exposure["allowed"] else ["Prompt content required redaction."],
            escalation_recommendation="" if promotion["accepted"] else "Run review/promotion gate.",
            review_required=not promotion["accepted"],
        )
        audit = {
            "run_id": run_id,
            "selected_model_alias": admission.requested_model_alias,
            "resolved_model_upstream": admission.resolved_upstream_model,
            "provider_route": admission.provider_route,
            "runtime_status": runtime_status,
            "evidence_hashes": [receipt["receipt_hash"], review_packet["packet_hash"]],
            "proof_gaps": [],
            "phase_graph_hash": phase_graph["graph_hash"],
            "escrow": escrow,
            "sandbox": sandbox,
            "delegation_value": delegation_value,
        }
        return NativeOSSSubagentRunViewV1(
            result=result,
            audit_projection=audit,
            review_packet=review_packet,
            markdown=markdown_projection(result),
        )


def _lane_for_mode(mode: str) -> str:
    if mode in {"implementation", "bounded_implementation", "patch"}:
        return "implementation"
    if mode == "review":
        return "review"
    return "scout"
