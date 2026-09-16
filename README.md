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
| `repository.py` | Restricts repository access and validates fresh JUnit and Allure evidence. |
| `metrics.py` | Selects safe numeric aggregates for local stage reports. |
| `safety.py` | Limits model calls independently of observability. |
| `telemetry.py` | Validates redaction and enables optional native Strands OTLP traces. |
| `main.py` | Loads one or more complete API cases and runs one graph per case in order. |

The distributed graph intentionally leaves the supplied `review` agent
disconnected. This is the bounded code change in the first practice. The
completed graph is then used for the capstone. These are the package's only two
practices.

## Course use

| Course section | Material in this package |
|---|---|
| From Markdown to Strands | Inspect how `agents.py` combines repository policy, role instructions, tools, and `AgentSkills`. |
| Workflow State and Handoffs | Follow the structured results in `state.py` and the evidence added to stage reports. |
| Building the Workflow Graph | Read the supplied dependencies and conditions in the starter `workflow.py`. |
| Observing Runs and Reusing Prompt Context | Inspect aggregate stage metrics in `result.json`, native Strands tracing setup in `telemetry.py`, and Anthropic cache configuration in `agents.py`. |
| Practice: Add the Review Route | Connect the supplied review agent and require current verified evidence. |
| Capstone: Run an Evidence-Backed Workflow | Run the completed graph on the assigned API cases and inspect their saved evidence. |

## Offline verification

From `strands-workflow/`, install the dependencies and run:

```shell
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest tests -q
```

These tests use synthetic model output and do not call a provider, backend,
Gradle, or a product test. The separate checks under `practice-tests/` express
the review-route acceptance criteria and are expected to fail before that
route is implemented.
