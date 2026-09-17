from __future__ import annotations

import inspect
import json
import logging

from metrics import stage_metrics
from state import EVIDENCE_STATUSES, Assessment
from strands.hooks import AfterMultiAgentInvocationEvent, AfterNodeCallEvent, BeforeNodeCallEvent
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status

logger = logging.getLogger(__name__)


def verified_target(evidence: dict, target: str) -> bool:
    """Require current execution proof for the exact selected target."""
    return (
        bool(target)
        and evidence.get("target") == target
        and evidence.get("status") == "VERIFIED"
        and evidence.get("target_status") == "VERIFIED"
    )


def result_data(state, name: str) -> dict:
    node = state.results.get(name)
    if node is None or node.status != Status.COMPLETED:
        return {}
    try:
        output = json.loads(str(node))
        return output if isinstance(output, dict) else {}
    except (ValueError, TypeError):
        return {}


def stage_report(state, name: str) -> dict:
    """Combine one stage's domain result with its content-free native metrics."""
    output = result_data(state, name)
    if not output:
        return {}
    return {**output, "metrics": stage_metrics(state.results[name])}


def validated_gap_handoff(output: dict, repository) -> bool:
    """Accept a GAP only for this case and the still-current source state."""
    return (
        output.get("status") == "GAP"
        and output.get("case_id") == repository.case_id
        and bool(output.get("source_fingerprint"))
        and output.get("source_fingerprint") == repository.source_fingerprint()
        and not repository.changed_files
    )


def _normalize_coverage_identity(output: dict, identity: dict) -> dict:
    """Derive the coverage route from the selected case's assigned Allure ID."""
    if not identity["case_targets"]:
        return {
            **output,
            "status": "GAP",
            "target": "",
            "summary": (
                f'No test with @AllureId("{identity["expected_allure_id"]}") covers the '
                "selected case; a target under another ID cannot satisfy this case identity"
            ),
        }
    if identity["matches_case_id"] and output.get("status") != "GAP":
        return output
    targets = ", ".join(identity["case_targets"])
    return {
        **output,
        "status": "BLOCKED",
        "target": targets if len(identity["case_targets"]) == 1 else "",
        "summary": (
            f'@AllureId("{identity["expected_allure_id"]}") is occupied by {targets}, '
            "so generation cannot create another test for the selected case"
        ),
    }


def _finalize_readiness(output: dict) -> dict:
    """Replace a model-owned READY handoff with the graph's next action."""
    if output.get("status") != "READY":
        return dict(output)
    return {**output, "next_action_or_question": "Continue to the coverage preflight."}


def _finalize_coverage_gap(
    output: dict,
    *,
    case_id: str,
    coverage_start_fingerprint: str,
    current_fingerprint: str,
    sources_changed: bool,
) -> dict:
    """Bind a source-only GAP to the case and unchanged coverage snapshot."""
    if (
        not coverage_start_fingerprint
        or current_fingerprint != coverage_start_fingerprint
        or sources_changed
    ):
        return {
            **output,
            "status": "NOT_VERIFIED",
            "summary": (
                "Repository sources changed during the coverage preflight; "
                "generation was not authorized"
            ),
        }
    return {
        **output,
        "case_id": case_id,
        "source_fingerprint": current_fingerprint,
    }


def _apply_execution_evidence(
    output: dict,
    evidence: dict,
    *,
    sources_changed: bool,
) -> dict:
    """Derive execution-owned statuses without mutating the model output."""
    finalized = {**output, "evidence": evidence}
    claimed_target = output.get("target", "")
    if output["status"] == "ALREADY_COVERED":
        if sources_changed:
            finalized["status"] = "NOT_VERIFIED"
            finalized["summary"] = "Source changes are incompatible with an already-covered result"
        elif evidence.get("target") == claimed_target and evidence.get("target_status") == "FAILED":
            finalized["status"] = "FAILED"
            finalized["summary"] = "The equivalent existing test failed its exact run"
        elif not verified_target(evidence, claimed_target):
            finalized["status"] = "NOT_VERIFIED"
            finalized["summary"] = (
                "Equivalent source coverage has no matching verified target evidence"
            )
    elif output["status"] in EVIDENCE_STATUSES:
        if not claimed_target or evidence.get("target") != claimed_target:
            finalized["status"] = "NOT_VERIFIED"
            finalized["summary"] = "Execution evidence does not match the selected target"
        else:
            finalized["status"] = evidence.get("status", "NOT_VERIFIED")
            if finalized["status"] == "FAILED" and evidence.get("target_status") != "FAILED":
                finalized["status"] = "NOT_VERIFIED"
    return finalized


def _enforce_repair_origin(output: dict, evidence: dict, origin_target: str) -> dict:
    """Require repair evidence for the target selected before repair."""
    if output["status"] != "VERIFIED" or verified_target(evidence, origin_target):
        return dict(output)
    return {
        **output,
        "status": "NOT_VERIFIED",
        "summary": "Repair evidence does not verify the exact target selected before repair",
    }


def _attach_change_context(
    output: dict,
    *,
    changed_files,
    diff: str,
    target: str | None = None,
) -> dict:
    """Add the repository fields shared by persisted stage handoffs."""
    finalized = dict(output)
    if target is not None:
        finalized["target"] = target
    finalized["changed_files"] = sorted(changed_files)
    finalized["diff"] = diff
    return finalized


def _finalize_review(output: dict) -> dict:
    """Derive the review status from completeness, questions, and findings."""
    unresolved = any(
        str(output.get(field, "none")).strip().lower() not in {"", "none"}
        for field in ("unverified", "question")
    )
    status = (
        "NEEDS_INVESTIGATION"
        if not output["complete"] or unresolved
        else "CHANGES_REQUESTED"
        if output["findings"]
        else "REVIEWED"
    )
    return {**output, "status": status}


def _finalize_stage_output(
    stage: str,
    output: dict,
    state,
    repository,
    coverage_start_fingerprint: str,
) -> dict:
    """Dispatch one structured stage result through the host-owned policies."""
    if stage == "coverage":
        identity = repository.coverage_identity(output.get("target", ""))
        output = _normalize_coverage_identity(output, identity)
    if stage == "readiness":
        return _finalize_readiness(output)
    if stage == "coverage" and output.get("status") == "GAP":
        current_fingerprint = repository.source_fingerprint()
        sources_changed = (
            not coverage_start_fingerprint
            or current_fingerprint != coverage_start_fingerprint
            or bool(repository.changed_files)
        )
        output = _finalize_coverage_gap(
            output,
            case_id=repository.case_id,
            coverage_start_fingerprint=coverage_start_fingerprint,
            current_fingerprint=current_fingerprint,
            sources_changed=sources_changed,
        )
        return _attach_change_context(
            output,
            changed_files=repository.changed_files,
            diff=repository.diff(),
        )
    if stage in {"coverage", "generation", "repair"}:
        evidence = repository.current_evidence()
        claimed_target = output.get("target", "")
        sources_changed = (
            bool(repository.changed_files) if output["status"] == "ALREADY_COVERED" else False
        )
        output = _apply_execution_evidence(
            output,
            evidence,
            sources_changed=sources_changed,
        )
        if stage == "repair" and output["status"] == "VERIFIED":
            origin = result_data(state, "generation") or result_data(state, "coverage")
            output = _enforce_repair_origin(output, evidence, origin.get("target", ""))
        return _attach_change_context(
            output,
            target=claimed_target or evidence.get("target", ""),
            changed_files=repository.changed_files,
            diff=repository.diff(),
        )
    if stage == "review":
        return _finalize_review(output)
    return dict(output)


def _publish_stage_output(stage: str, node, result, output: dict, output_dir) -> None:
    """Replace the native result handoff and persist its metrics-bearing report."""
    result.message = {"role": "assistant", "content": [{"text": json.dumps(output)}]}
    result.structured_output = None
    report = {**output, "metrics": stage_metrics(node)}
    (output_dir / f"{stage}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _final_next_action(status: str, target: str = "") -> str:
    selected = target or "the selected target"
    actions = {
        "REVIEWED": "No further workflow action is required for this case.",
        "ALREADY_COVERED": f"Reuse {selected}; do not generate a duplicate test.",
        "VERIFIED": f"Run the final case-conformance review for {selected}.",
        "CHANGES_REQUESTED": (
            f"Correct the review findings for {selected}, rerun it, and review it again."
        ),
        "GAP": "Generate and execute a test with the selected case's assigned Allure ID.",
        "FAILED": f"Repair {selected} using its current exact-target evidence.",
        "BLOCKED": "Resolve the reported blocker before continuing.",
        "NEEDS_CLARIFICATION": "Answer the reported readiness question before continuing.",
        "NOT_VERIFIED": (
            "Obtain current exact-target execution evidence before treating this case as complete."
        ),
        "PRODUCT_BUG": "Report the product defect and keep the test expectation unchanged.",
        "NEEDS_INVESTIGATION": "Collect the missing evidence before continuing.",
        "INFRASTRUCTURE_ISSUE": "Restore the test infrastructure and rerun the exact target.",
        "EXHAUSTED": "Stop automatic repair and inspect the retained evidence.",
    }
    return actions.get(status, "Inspect the final stage result before continuing.")


async def _close_model_clients(agents: dict) -> None:
    """Close every distinct provider client without hiding the workflow result."""
    seen: set[int] = set()
    for agent in agents.values():
        client = getattr(getattr(agent, "model", None), "client", None)
        if client is None or id(client) in seen:
            continue
        close = getattr(client, "close", None)
        if not callable(close):
            continue
        seen.add(id(client))
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Could not close a workflow model client", exc_info=True)


def build_graph(agents: dict, repository, *, include_readiness: bool = True):
    """Repair owns its existing run loop and budgets; the graph chooses stages."""
    builder = GraphBuilder()
    node_names = ["coverage", "generation", "repair"]
    if include_readiness:
        node_names.insert(0, "readiness")
    for name in node_names:
        builder.add_node(agents[name], name)

    # The review agent and its output schema are supplied, but this starter does
    # not connect them. The review-route practice adds that dependency.

    def ready(state):
        return result_data(state, "readiness").get("status") == "READY"

    def gap(state):
        return validated_gap_handoff(result_data(state, "coverage"), repository)

    def failed_target(state, name):
        output = result_data(state, name)
        return (
            output.get("status") == "FAILED"
            and output.get("evidence", {}).get("target_status") == "FAILED"
        )

    if include_readiness:
        builder.add_edge("readiness", "coverage", ready)
        builder.set_entry_point("readiness")
    else:
        builder.set_entry_point("coverage")
    builder.add_edge("coverage", "generation", gap)
    builder.add_edge("coverage", "repair", lambda state: failed_target(state, "coverage"))
    builder.add_edge("generation", "repair", lambda state: failed_target(state, "generation"))
    builder.set_max_node_executions(5)
    graph = builder.build()
    coverage_start_fingerprint = ""

    def before_node(event):
        nonlocal coverage_start_fingerprint
        if event.node_id == "coverage":
            coverage_start_fingerprint = repository.source_fingerprint()

    def after_node(event):
        node = event.source.state.results[event.node_id]
        result = node.result
        if getattr(result, "structured_output", None) is None:
            return
        output = _finalize_stage_output(
            event.node_id,
            result.structured_output.model_dump(),
            event.source.state,
            repository,
            coverage_start_fingerprint,
        )
        _publish_stage_output(event.node_id, node, result, output, repository.output_dir)

    async def close_model_clients(event):
        """Close async provider clients before Strands closes its invocation loop."""
        del event
        await _close_model_clients(agents)

    graph.add_hook(before_node, BeforeNodeCallEvent)
    graph.add_hook(after_node, AfterNodeCallEvent)
    graph.add_hook(close_model_clients, AfterMultiAgentInvocationEvent)
    return graph


def run_workflow(
    case: str,
    repository,
    agents: dict,
    *,
    initial_assessment: Assessment | None = None,
    readiness_source: str = "model",
) -> dict:
    initial_stage = None
    if initial_assessment is not None:
        initial_stage = {**initial_assessment.model_dump(), "source": readiness_source}
        if initial_stage["status"] == "READY":
            initial_stage["next_action_or_question"] = "Continue to the coverage preflight."
        (repository.output_dir / "readiness.json").write_text(
            json.dumps(initial_stage, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if initial_assessment.status != "READY":
            report = {
                "case_id": repository.case_id,
                "status": initial_assessment.status,
                "graph_status": "skipped",
                "error_type": None,
                "execution_order": [],
                "changed_files": sorted(repository.changed_files),
                "evidence": repository.current_evidence(),
                "stages": {"readiness": initial_stage},
                "readiness_source": readiness_source,
                "next_action": _final_next_action(initial_assessment.status),
            }
            path = repository.output_dir / "result.json"
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            return report

    include_readiness = initial_assessment is None
    graph = build_graph(agents, repository, include_readiness=include_readiness)
    task = (
        f"Complete the API automation workflow for {repository.case_id}. "
        "The request includes readiness selection, coverage comparison, generation, "
        "execution, repair of an "
        "evidence-backed test automation defect if needed, and local read-only review. "
        "Keep the complete case as the source of expected behavior at every stage.\n\n" + case
    )
    error = None
    try:
        graph(task)
    except Exception as failure:
        error = type(failure).__name__
    state = graph.state
    stages = {name: stage_report(state, name) for name in state.results}
    if initial_stage is not None:
        stages = {"readiness": initial_stage, **stages}
    elif stages.get("readiness"):
        stages["readiness"]["source"] = readiness_source
    order = [node.node_id for node in state.execution_order]
    final = stages.get(order[-1], {}) if order else {}
    status = final.get("status", "NOT_VERIFIED")
    evidence = repository.current_evidence()
    blocking_reason = ""
    if error or state.status != Status.COMPLETED:
        status = "NOT_VERIFIED"
    generation = stages.get("generation", {})
    coverage = stages.get("coverage", {})
    repair = stages.get("repair", {})
    final_target = repair.get("target") or generation.get("target") or coverage.get("target", "")
    if status in {"VERIFIED", "REVIEWED"} and not verified_target(evidence, final_target):
        status = "NOT_VERIFIED"
        blocking_reason = "Final result has no matching verified exact-target evidence"
    repair_evidence = repair.get("evidence", {})
    if "repair" in order and status in {"VERIFIED", "REVIEWED"}:
        repair_origin = generation if "generation" in order else coverage
        origin_evidence = repair_origin.get("evidence", {})
        origin_failed_exact_target = (
            repair_origin.get("status") == "FAILED"
            and origin_evidence.get("target") == repair_origin.get("target")
            and origin_evidence.get("target_status") == "FAILED"
        )
        if not origin_failed_exact_target:
            status = "NOT_VERIFIED"
            blocking_reason = "Pre-repair evidence does not establish failure of the target"
        elif not verified_target(repair_evidence, repair_origin.get("target", "")):
            status = "NOT_VERIFIED"
            blocking_reason = "Repair evidence does not verify the selected target"
    if status == "ALREADY_COVERED" and not (
        not repository.changed_files and verified_target(evidence, coverage.get("target", ""))
    ):
        status = "NOT_VERIFIED"
        blocking_reason = "Equivalent source coverage has no matching verified target evidence"
    if status == "GAP" and not validated_gap_handoff(coverage, repository):
        status = "NOT_VERIFIED"
        blocking_reason = "Coverage GAP handoff does not match the current case and source state"
    report = {
        "case_id": repository.case_id,
        "status": status,
        "graph_status": state.status.value,
        "error_type": error,
        "execution_order": order,
        "changed_files": sorted(repository.changed_files),
        "evidence": evidence,
        "stages": stages,
        "readiness_source": readiness_source,
        "next_action": _final_next_action(status, final_target),
    }
    if blocking_reason:
        report["blocking_reason"] = blocking_reason
    path = repository.output_dir / "result.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
