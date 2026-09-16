from __future__ import annotations

import json

from strands.hooks import AfterNodeCallEvent, BeforeNodeCallEvent
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status

from state import EVIDENCE_STATUSES


def result_data(state, name: str) -> dict:
    node = state.results.get(name)
    if node is None or node.status != Status.COMPLETED:
        return {}
    try:
        output = json.loads(str(node))
        return output if isinstance(output, dict) else {}
    except (ValueError, TypeError):
        return {}


def build_graph(agents: dict, repository, telemetry):
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

    def before_node(event):
        telemetry.stage("stage_start", node=event.node_id)

    def after_node(event):
        node = event.source.state.results[event.node_id]
        result = node.result
        if getattr(result, "structured_output", None) is None:
            telemetry.stage("stage_failed", node=event.node_id)
            return
        output = result.structured_output.model_dump()
        if event.node_id in {"generation", "repair"}:
            evidence = repository.current_evidence()
            output["evidence"] = evidence
            if output["status"] == "ALREADY_COVERED" and repository.changed_files:
                output["status"] = "NOT_VERIFIED"
                output["summary"] = "Source changes are incompatible with an already-covered result"
            elif output["status"] in EVIDENCE_STATUSES:
                output["status"] = evidence.get("status", "NOT_VERIFIED")
                if output["status"] == "FAILED" and evidence.get("target_status") != "FAILED":
                    output["status"] = "NOT_VERIFIED"
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
        (repository.output_dir / f"{event.node_id}.json").write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        telemetry.stage(
            "stage_end",
            node=event.node_id,
            status=output["status"],
            duration_ms=node.execution_time,
            input_tokens=node.accumulated_usage.get("inputTokens"),
            output_tokens=node.accumulated_usage.get("outputTokens"),
            total_tokens=node.accumulated_usage.get("totalTokens"),
            cache_read_input_tokens=node.accumulated_usage.get("cacheReadInputTokens"),
            cache_write_input_tokens=node.accumulated_usage.get("cacheWriteInputTokens"),
        )

    graph.add_hook(before_node, BeforeNodeCallEvent)
    graph.add_hook(after_node, AfterNodeCallEvent)
    return graph


def run_workflow(case: str, repository, agents: dict, telemetry) -> dict:
    graph = build_graph(agents, repository, telemetry)
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
        telemetry.stage("workflow_error", exception_type=error)
    state = graph.state
    stages = {name: result_data(state, name) for name in state.results}
    order = [node.node_id for node in state.execution_order]
    final = stages.get(order[-1], {}) if order else {}
    status = final.get("status", "NOT_VERIFIED")
    evidence = repository.current_evidence()
    if error or state.status != Status.COMPLETED:
        status = "NOT_VERIFIED"
    if status == "REVIEWED" and evidence.get("status") != "VERIFIED":
        status = "NOT_VERIFIED"
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
    path = repository.output_dir / "result.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    telemetry.stage("workflow_end", status=status, count=len(order), case_id=repository.case_id)
    return report
