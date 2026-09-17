# QA workflow with Strands

Install this package into the existing `AI-for-Kotlin-practice` repository. It
adds a small Python application that coordinates the repository's established
readiness, API generation, execution, repair, and case-check procedures with a
Strands graph.

Install the [QA workflow skills package](https://github.com/nebius-academy-templates/qa-template-week-4-workflow)
first. This package uses those skills and hooks; it does not duplicate them.

## Install

1. Download this repository using **Code → Download ZIP** and open
   `repository-files/`.
2. Copy the contents of `repository-files/` into the root of your existing
   practice repository, preserving the directory structure.
3. Open [`strands-workflow/README.md`](repository-files/strands-workflow/README.md)
   in the practice repository and configure the Python environment from that
   directory.

The copy adds only `strands-workflow/`. It does not replace application code,
tests, repository rules, skills, hooks, the case workbook, or observability
configuration.

## Included workflow

| Component | Responsibility |
|---|---|
| `agents.py` | Builds the four agents from repository policy, role instructions, tools, and installed skills. |
| `state.py` | Defines the structured results passed between operations. |
| `workflow.py` | Defines the starter graph and evidence-derived result handling. |
| `workspace.py` | Restricts repository file access and computes source fingerprints. |
| `repository.py` | Runs the exact API test through the existing repair hook. |
| `evidence.py` | Validates matching fresh JUnit and Allure evidence. |
| `process_runner.py` | Stops owned test processes on timeout. |
| `metrics.py` | Selects safe numeric aggregates for local stage reports. |
| `safety.py` | Limits model calls independently of observability. |
| `telemetry.py` | Validates redaction and enables optional native Strands OTLP traces. |
| `main.py` | Loads one or more complete API cases and runs one graph per case in order. |

The distributed graph intentionally leaves the supplied `review` agent
disconnected. This is the bounded code change in the first practice. The
completed graph is then used for the capstone. These are the package's only two
practices.

Generation checks each case's assigned Allure ID. For a repeated batch,
`--skip-implemented` skips a complete unchanged implementation after source
inspection and reports `ALREADY_IMPLEMENTED`, without claiming a fresh passing
run. Other cases continue through generation, exact execution and conditional
repair. The backend address is fixed at `http://127.0.0.1:8080`.

## Course use

| Course section | Material in this package |
|---|---|
| From Markdown to Strands | Inspect how `agents.py` combines repository policy, role instructions, tools, and `AgentSkills`. |
| Workflow State and Handoffs | Follow the structured results in `state.py` and the evidence added to stage reports. |
| Building the Workflow Graph | Read the supplied dependencies and conditions in the starter `workflow.py`. |
| Observing Runs and Reusing Prompt Context | Inspect aggregate stage metrics in `result.json`, native Strands tracing setup in `telemetry.py`, and Anthropic cache configuration in `agents.py`. |
| Practice: Add the Review Route | Connect the supplied review agent and require current verified evidence. |
| Capstone: Run an Evidence-Backed Workflow | Run the completed graph on the assigned API cases and inspect their saved evidence. |
