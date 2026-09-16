from __future__ import annotations

import json

from metrics import stage_metrics
from state import EVIDENCE_STATUSES
from strands.hooks import AfterNodeCallEvent
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status


def verified_full_suite(evidence: dict, target: str) -> bool:
    """Require current full-suite proof for the exact selected target."""
    return (
        bool(target)
        and evidence.get("target") == target
        and evidence.get("full_suite") is True
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


def build_graph(agents: dict, repository):
    """Repair owns its existing run loop and budgets; the graph chooses stages."""
    builder = GraphBuilder()
    for name in ("readiness", "generation", "repair"):
        builder.add_node(agents[name], name)

    # The review agent and its output schema are supplied, but this starter does
    # not connect them. The review-route practice adds that dependency.

    def ready(state):
        return result_data(state, "readiness").get("status") == "READY"

    def passed(state, name):
        return result_data(state, name).get("status") == "VERIFIED"

    def failed_target(state):
        output = result_data(state, "generation")
        return (
            output.get("status") == "FAILED"
            and output.get("evidence", {}).get("target_status") == "FAILED"
        )

    builder.add_edge("readiness", "generation", ready)
    builder.add_edge("generation", "repair", failed_target)
    builder.set_entry_point("readiness")
    builder.set_max_node_executions(4)
    graph = builder.build()

    def after_node(event):
        node = event.source.state.results[event.node_id]
        result = node.result
        if getattr(result, "structured_output", None) is None:
            return
        output = result.structured_output.model_dump()
        if event.node_id in {"generation", "repair"}:
            evidence = repository.current_evidence()
            output["evidence"] = evidence
            if output["status"] == "ALREADY_COVERED":
                claimed_target = output.get("target", "")
                if repository.changed_files:
                    output["status"] = "NOT_VERIFIED"
                    output["summary"] = (
                        "Source changes are incompatible with an already-covered result"
                    )
                elif (
                    evidence.get("full_suite") is True
                    and evidence.get("target") == claimed_target
                    and evidence.get("target_status") == "FAILED"
                ):
                    output["status"] = "FAILED"
                    output["summary"] = "The equivalent existing test failed its full-suite run"
                elif not verified_full_suite(evidence, claimed_target):
                    output["status"] = "NOT_VERIFIED"
                    output["summary"] = (
                        "Equivalent source coverage has no matching verified full-suite evidence"
                    )
            elif output["status"] in EVIDENCE_STATUSES:
                output["status"] = evidence.get("status", "NOT_VERIFIED")
                if output["status"] == "FAILED" and evidence.get("target_status") != "FAILED":
                    output["status"] = "NOT_VERIFIED"
            if event.node_id == "repair" and output["status"] == "VERIFIED":
                generation_target = result_data(event.source.state, "generation").get("target", "")
                if not (
                    generation_target
                    and evidence.get("target") == generation_target
                    and evidence.get("full_suite") is False
                    and evidence.get("status") == "VERIFIED"
                    and evidence.get("target_status") == "VERIFIED"
                ):
                    output["status"] = "NOT_VERIFIED"
                    output["summary"] = (
                        "Repair evidence does not verify the exact target selected by generation"
                    )
            output["target"] = evidence.get("target", output.get("target", ""))
            output["changed_files"] = sorted(repository.changed_files)
            output["diff"] = repository.diff()
        elif event.node_id == "review":
            unresolved = any(
                str(output.get(field, "none")).strip().lower() not in {"", "none"}
                for field in ("unverified", "question")
            )
            output["status"] = (
                "NEEDS_INVESTIGATION"
                if not output["complete"] or unresolved
                else "CHANGES_REQUESTED"
                if output["findings"]
                else "REVIEWED"
            )
        result.message = {"role": "assistant", "content": [{"text": json.dumps(output)}]}
        result.structured_output = None
        report = {**output, "metrics": stage_metrics(node)}
        (repository.output_dir / f"{event.node_id}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    graph.add_hook(after_node, AfterNodeCallEvent)
    return graph


def run_workflow(case: str, repository, agents: dict) -> dict:
    graph = build_graph(agents, repository)
    task = (
        f"Complete the API automation workflow for {repository.case_id}. "
        "The request includes source assessment, generation, execution, repair of an "
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
    order = [node.node_id for node in state.execution_order]
    final = stages.get(order[-1], {}) if order else {}
    status = final.get("status", "NOT_VERIFIED")
    evidence = repository.current_evidence()
    blocking_reason = ""
    if error or state.status != Status.COMPLETED:
        status = "NOT_VERIFIED"
    if status == "REVIEWED" and evidence.get("status") != "VERIFIED":
        status = "NOT_VERIFIED"
    generation = stages.get("generation", {})
    generation_evidence = generation.get("evidence", {})
    repair_evidence = stages.get("repair", {}).get("evidence", {})
    # Repair reruns only the exact target. Keep the original suite's result for
    # every other test instead of launching a second full-suite run.
    if "repair" in order and status in {"VERIFIED", "REVIEWED"}:
        repair_verified_exact_target = (
            generation.get("target")
            and repair_evidence.get("target") == generation.get("target")
            and repair_evidence.get("full_suite") is False
            and repair_evidence.get("status") == "VERIFIED"
            and repair_evidence.get("target_status") == "VERIFIED"
        )
        generation_suite_supports_repair = (
            generation_evidence.get("full_suite") is True
            and generation_evidence.get("target") == generation.get("target")
            and generation_evidence.get("target_status") == "FAILED"
            and generation_evidence.get("non_target_status") == "VERIFIED"
        )
        if not repair_verified_exact_target:
            status = "NOT_VERIFIED"
            blocking_reason = "Repair evidence does not verify the target selected by generation"
        elif not generation_suite_supports_repair:
            status = "NOT_VERIFIED"
            blocking_reason = (
                "Exact repair verified the target, but the generation full suite did not "
                "establish the target failure with all non-target tests verified"
            )
    if status == "ALREADY_COVERED" and not (
        not repository.changed_files and verified_full_suite(evidence, generation.get("target", ""))
    ):
        status = "NOT_VERIFIED"
        blocking_reason = "Equivalent source coverage has no matching verified full-suite evidence"
    report = {
        "case_id": repository.case_id,
        "status": status,
        "graph_status": state.status.value,
        "error_type": error,
        "execution_order": order,
        "changed_files": sorted(repository.changed_files),
        "evidence": evidence,
        "stages": stages,
    }
    if blocking_reason:
        report["blocking_reason"] = blocking_reason
    path = repository.output_dir / "result.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
