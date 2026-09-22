from __future__ import annotations

import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import Iterable

from metrics import partial_stage_metrics, stage_metrics
from safety import ModelCallLimitExceeded
from state import EVIDENCE_STATUSES, Assessment
from strands.hooks import (
    AfterMultiAgentInvocationEvent,
    AfterNodeCallEvent,
    BeforeNodeCallEvent,
)
from strands.models import Model
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status
from strands.types.exceptions import EventLoopException

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


def _finalize_readiness(output: dict, skip_implemented: bool = False) -> dict:
    """Apply the host-owned transition for one readiness result."""
    finalized = {**output}
    if finalized.get("status") == "READY":
        finalized["next_action_or_question"] = (
            "Inspect the assigned implementation; skip it only if complete, "
            "otherwise automate and execute."
            if skip_implemented
            else "Automate and execute the selected case."
        )
    return finalized


def _apply_execution_evidence(output: dict, evidence: dict) -> dict:
    """Normalize one execution claim against current exact-target evidence."""
    finalized = {**output, "evidence": evidence}
    claimed_target = finalized.get("target", "")
    if finalized["status"] in EVIDENCE_STATUSES:
        if not claimed_target or evidence.get("target") != claimed_target:
            finalized["status"] = "VERIFICATION_INCOMPLETE"
            finalized["blocking_reason"] = "Execution evidence does not match the selected target"
        else:
            finalized["status"] = evidence.get("status", "VERIFICATION_INCOMPLETE")
            if finalized["status"] == "FAILED" and evidence.get("target_status") != "FAILED":
                finalized["status"] = "VERIFICATION_INCOMPLETE"

    finalized["target"] = claimed_target or evidence.get("target", "")
    return finalized


def _enforce_repair_origin(output: dict, evidence: dict, failed_target: str) -> dict:
    """Require a verified repair for the exact target that previously failed."""
    finalized = {**output}
    if finalized["status"] == "VERIFIED" and not verified_target(evidence, failed_target):
        finalized["status"] = "VERIFICATION_INCOMPLETE"
        finalized["blocking_reason"] = (
            "Repair evidence does not verify the exact target selected by the failed stage"
        )
    return finalized


def _attach_repository_state(output: dict, changed_files, diff: str) -> dict:
    """Attach the source snapshot emitted with an executable stage."""
    return {**output, "changed_files": sorted(changed_files), "diff": diff}


def _finalize_review(output: dict) -> dict:
    """Derive the review status from completeness, unresolved fields, and findings."""
    finalized = {**output}
    unresolved = any(
        str(finalized.get(field, "none")).strip().lower() not in {"", "none"}
        for field in ("unverified", "question")
    )
    finalized["status"] = (
        "NEEDS_INVESTIGATION"
        if not finalized["complete"] or unresolved
        else "CHANGES_REQUESTED"
        if finalized["findings"]
        else "REVIEWED"
    )
    return finalized


def _finalize_stage_output(stage: str, output: dict, state, repository) -> dict:
    """Dispatch one model result through the matching host-owned policy."""
    if stage == "readiness":
        return _finalize_readiness(output, repository.skip_implemented)
    if stage in {"generation", "repair"}:
        evidence = repository.current_evidence()
        output = _apply_execution_evidence(output, evidence)
        if stage == "generation" and output["status"] == "ALREADY_IMPLEMENTED":
            try:
                if not output.get("summary", "").strip():
                    raise ValueError(
                        "Implementation inspection requires a source-supported summary"
                    )
                output["implementation"] = repository.existing_implementation(output["target"])
            except ValueError as error:
                output["status"] = "VERIFICATION_INCOMPLETE"
                output["blocking_reason"] = str(error)
        if stage == "repair" and output["status"] == "VERIFIED":
            failure = result_data(state, "generation")
            output = _enforce_repair_origin(output, evidence, failure.get("target", ""))
        return _attach_repository_state(output, repository.changed_files, repository.diff())
    if stage == "review":
        return _finalize_review(output)
    return {**output}


def _publish_stage_output(stage: str, node, output: dict, output_dir) -> None:
    """Replace the routed result and persist its content-free stage report."""
    result = node.result
    result.message = {"role": "assistant", "content": [{"text": json.dumps(output)}]}
    result.structured_output = None
    (output_dir / f"{stage}.json").write_text(
        json.dumps({**output, "metrics": stage_metrics(node)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _final_next_action(status: str, target: str = "") -> str:
    selected = target or "the selected target"
    actions = {
        "REVIEWED": "No further workflow action is required for this case.",
        "ALREADY_IMPLEMENTED": (
            "Implementation already exists; no fresh execution was requested. "
            "Run without --skip-implemented when fresh verification and review are needed."
        ),
        "VERIFIED": f"Run the final case-conformance review for {selected}.",
        "CHANGES_REQUESTED": (
            f"Correct the review findings for {selected}, rerun it, and review it again."
        ),
        "FAILED": f"Repair {selected} using its current exact-target evidence.",
        "BLOCKED": "Resolve the reported blocker before continuing.",
        "NEEDS_CLARIFICATION": "Answer the reported readiness question before continuing.",
        "VERIFICATION_INCOMPLETE": (
            "Obtain current exact-target execution evidence before treating this case as complete."
        ),
        "PRODUCT_BUG": "Report the product defect and keep the test expectation unchanged.",
        "NEEDS_INVESTIGATION": "Collect the missing evidence before continuing.",
        "INFRASTRUCTURE_ISSUE": "Restore the test infrastructure and rerun the exact target.",
        "EXHAUSTED": "Stop automatic repair and inspect the retained evidence.",
    }
    return actions.get(status, "Inspect the final stage result before continuing.")


async def close_model_clients(models: Iterable[Model | None]) -> None:
    """Close each distinct provider client once."""
    closed: set[int] = set()
    for model in models:
        client = getattr(model, "client", None)
        if client is None or id(client) in closed:
            continue
        close = getattr(client, "close", None)
        if not callable(close):
            continue
        closed.add(id(client))
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Could not close a workflow model client", exc_info=True)


def build_graph(agents: dict):
    """Repair owns its existing run loop and budgets; the graph chooses stages."""
    builder = GraphBuilder()
    # Practice: register review, connect verified results, and check freshness before review.
    for name in ("readiness", "generation", "repair"):
        if name in agents:
            builder.add_node(agents[name], name)

    def ready(state):
        return result_data(state, "readiness").get("status") == "READY"

    def failed_target(state, name):
        output = result_data(state, name)
        return (
            output.get("status") == "FAILED"
            and output.get("evidence", {}).get("target_status") == "FAILED"
        )

    if "readiness" in agents:
        builder.add_edge("readiness", "generation", ready)
        builder.set_entry_point("readiness")
    else:
        builder.set_entry_point("generation")
    builder.add_edge("generation", "repair", lambda state: failed_target(state, "generation"))
    builder.set_max_node_executions(4)
    graph = builder.build()

    def before_node(event):
        event.invocation_state["active_stage"] = event.node_id

    def after_node(event):
        repository = event.invocation_state["repository"]
        node = event.source.state.results[event.node_id]
        result = node.result
        if getattr(result, "structured_output", None) is None:
            return
        output = _finalize_stage_output(
            event.node_id,
            result.structured_output.model_dump(),
            event.source.state,
            repository,
        )
        _publish_stage_output(event.node_id, node, output, repository.output_dir)

    async def close_clients(_event):
        await close_model_clients(agent.model for agent in agents.values())

    graph.add_hook(before_node, BeforeNodeCallEvent)
    graph.add_hook(after_node, AfterNodeCallEvent)
    graph.add_hook(close_clients, AfterMultiAgentInvocationEvent)
    return graph


def _readiness_stage(assessment: Assessment, source: str, skip_implemented: bool) -> dict:
    return {**_finalize_readiness(assessment.model_dump(), skip_implemented), "source": source}


def _write_initial_readiness(repository, assessment: Assessment, source: str) -> dict:
    stage = _readiness_stage(assessment, source, repository.skip_implemented)
    (repository.output_dir / "readiness.json").write_text(
        json.dumps(stage, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return stage


def _write_result(repository, readiness_source: str, outcome: dict) -> dict:
    report = {
        "case_id": repository.case_id,
        "skip_implemented": repository.skip_implemented,
        "readiness_source": readiness_source,
        "changed_files": sorted(repository.changed_files),
        **outcome,
    }
    path = repository.output_dir / "result.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def run_workflow(
    case: str,
    repository,
    agents: dict,
    initial_assessment: Assessment | None = None,
    readiness_source: str = "model",
) -> dict:
    graph = None
    try:
        initial_readiness = (
            _write_initial_readiness(repository, initial_assessment, readiness_source)
            if initial_assessment is not None
            else None
        )
        if initial_assessment is not None and initial_assessment.status != "READY":
            return _write_result(
                repository,
                readiness_source,
                {
                    "status": initial_assessment.status,
                    "graph_status": "skipped",
                    "error_type": None,
                    "execution_order": [],
                    "evidence": repository.current_evidence(),
                    "stages": {"readiness": initial_readiness},
                    "next_action": _final_next_action(initial_assessment.status),
                },
            )

        graph = build_graph(agents)
    finally:
        if graph is None:
            asyncio.run(close_model_clients(agent.model for agent in agents.values()))
    task = (
        f"Complete the test automation workflow for {repository.case_id}. "
        "The request includes the selected readiness decision, automation of this case "
        "and exact execution, repair of an evidence-backed "
        "test automation defect if needed, and local read-only review. Keep the complete "
        "case as the source of expected behavior at every stage.\n\n" + case
    )
    if repository.skip_implemented:
        task += (
            "\n\nThis invocation uses --skip-implemented: inspect the assigned-ID test first. "
            "If it already implements every case requirement, return ALREADY_IMPLEMENTED "
            "without editing, execution or final review. Otherwise complete the full workflow."
        )
    error = None
    error_message = None
    error_log = None
    model_call_limit = None
    invocation_state = {"repository": repository, "case": case}
    try:
        graph(task, invocation_state=invocation_state)
    except Exception as failure:
        cause = failure
        while isinstance(cause, EventLoopException):
            cause = cause.original_exception
        if isinstance(cause, ModelCallLimitExceeded):
            model_call_limit = cause
            failure = cause
        error = type(failure).__name__
        error_message = str(failure)
        try:
            (repository.output_dir / "error.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            error_log = "error.log"
        except OSError:
            logger.warning("Could not save the workflow error log", exc_info=True)
    state = graph.state
    stages = {name: stage_report(state, name) for name in state.results}
    if initial_readiness is not None:
        stages = {
            "readiness": initial_readiness,
            **stages,
        }
    order = [node.node_id for node in state.execution_order]
    error_stage = None
    if error:
        error_stage = (
            model_call_limit.stage
            if model_call_limit is not None
            else next(
                (name for name, node in state.results.items() if node.status == Status.FAILED),
                invocation_state.get("active_stage"),
            )
        )
        if error_stage in agents:
            node = state.results.get(error_stage)
            stage = {
                **stages.get(error_stage, {}),
                "status": "VERIFICATION_INCOMPLETE",
                "error_type": error,
                "error_message": error_message,
                "metrics": partial_stage_metrics(
                    node,
                    getattr(agents[error_stage], "event_loop_metrics", None)
                    if node is not None
                    else None,
                ),
            }
            stages[error_stage] = stage
            try:
                (repository.output_dir / f"{error_stage}.json").write_text(
                    json.dumps(stage, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                logger.warning("Could not save the failed stage report", exc_info=True)
    final = stages.get(order[-1], {}) if order else {}
    status = final.get("status", "VERIFICATION_INCOMPLETE")
    evidence = repository.current_evidence()
    blocking_reason = final.get("blocking_reason", "")
    if error or state.status != Status.COMPLETED:
        status = "VERIFICATION_INCOMPLETE"
    generation = stages.get("generation", {})
    repair = stages.get("repair", {})
    final_target = repair.get("target") or generation.get("target", "")
    if status == "ALREADY_IMPLEMENTED":
        try:
            repository.existing_implementation(final_target)
        except ValueError as failure:
            status = "VERIFICATION_INCOMPLETE"
            blocking_reason = str(failure)
    if status == "REVIEWED" and not verified_target(evidence, final_target):
        status = "VERIFICATION_INCOMPLETE"
        blocking_reason = "Review has no matching verified exact-target evidence"
    if "repair" in order and status in {"VERIFIED", "REVIEWED"}:
        failure = generation
        failure_evidence = failure.get("evidence", {})
        failed_exact_target = (
            failure.get("status") == "FAILED"
            and failure_evidence.get("target") == failure.get("target")
            and failure_evidence.get("target_status") == "FAILED"
        )
        if not failed_exact_target:
            status = "VERIFICATION_INCOMPLETE"
            blocking_reason = "Preceding evidence does not establish exact-target failure"
        elif not verified_target(repair.get("evidence", {}), failure.get("target", "")):
            status = "VERIFICATION_INCOMPLETE"
            blocking_reason = "Repair evidence does not verify the failed exact target"
    report = {
        "status": status,
        "graph_status": state.status.value,
        "error_type": error,
        "execution_order": order,
        "evidence": evidence,
        "stages": stages,
        "next_action": _final_next_action(status, final_target),
    }
    if blocking_reason:
        report["blocking_reason"] = blocking_reason
    if error:
        report["error_message"] = error_message
        report["error_stage"] = error_stage
        if error_log is not None:
            report["error_log"] = error_log
    if model_call_limit is not None:
        report.update(
            error_code="MODEL_CALL_LIMIT",
            error_stage=model_call_limit.stage,
            model_call_limit={
                "calls": model_call_limit.calls,
                "maximum": model_call_limit.maximum,
            },
            next_action=(
                f"Inspect the {model_call_limit.stage} stage's recorded work and "
                "model-call budget before retrying the workflow."
            ),
        )
    return _write_result(repository, readiness_source, report)
