# Evidence-backed API workflow with Strands

This starter coordinates one complete API case through source assessment,
generation and fresh execution, conditional repair, and a read-only check
against the original case. It uses the documents, skills, hook, Kotlin suite,
and case workbook already installed in the practice repository.

The starter supplies all four agents and the deterministic repository adapter.
Its graph connects readiness, generation, and conditional repair. The review
agent is deliberately not registered as a graph node until the review-route
practice is completed.

## Prerequisites

Use Python 3.11 or newer. Before running the application, verify that the
practice repository contains:

- `AGENTS.md` and `agent_docs/AI_POLICY.md`;
- `agent_docs/task-automation-readiness-instructions.md`;
- `.agents/skills/automate-test-case/SKILL.md`;
- `.agents/skills/gen-api-test/SKILL.md`;
- `.agents/skills/test-repair/SKILL.md`;
- `.agents/hooks/test_repair.py` and its installed hook configuration;
- `agent_docs/templates/automation_plan.api.workflow.md.template`;
- `test-cases/test-cases.xlsx`;
- `scripts/protected-paths.txt`.

Start the fake API using the instructions in `api-tests/README.md` before a
live workflow run. Finish unrelated repair work first because the existing
repair hook owns its queue and attempt counters.

## Set up the Python environment

Run these commands from this `strands-workflow` directory.

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest
```

On macOS or Linux:

```shell
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install pytest
```

Set either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in the current environment.
Do not put a key in this repository. Select the matching provider and an
available model explicitly when running `main.py`.

## How the application is assembled

The implementation keeps three concerns separate:

| Concern | Location | Effect |
|---|---|---|
| Model instructions | `agents.py` and the installed Markdown procedures | Define what each model call evaluates or produces. |
| Available operations | Tool functions in `agents.py`, backed by `repository.py` | Bound which files and commands each agent can use. |
| Execution dependencies | Edges and conditions in `workflow.py` | Decide which completed result can start another operation. |

`AgentSkills` loads `gen-api-test` and `test-repair` instructions on demand.
It does not provide filesystem access, test execution, hook behavior, or proof
of a successful run. The host application supplies those operations separately.

| Agent | Instruction source | Supplied access |
|---|---|---|
| `readiness` | Existing readiness instructions | Read and search repository sources. |
| `generation` | `gen-api-test` through `AgentSkills` | Read, plan, edit permitted API test layers, and run the fresh full API suite. |
| `repair` | `test-repair` through `AgentSkills` | Diagnose the selected target, use the existing queue, edit permitted test layers, and rerun that exact target. |
| `review` | Case-check section of `automate-test-case` | Read the complete case, final test, helpers, plan, diff, and current evidence. |

Every role also receives the complete original case, `AGENTS.md`, and
`agent_docs/AI_POLICY.md`. A generated status cannot replace the host's JUnit
and Allure validation. Equivalent source coverage is reported as
`ALREADY_COVERED`; an occupied Allure ID without equivalent behavior is
reported as `BLOCKED`.

## State, evidence, and reports

`state.py` defines the structured result of each operation. After generation
or repair, the host attaches the selected `Class.method`, changed files, current
diff, command log, JUnit counts, and Allure attachment evidence. Any source
change makes earlier execution evidence stale.

Each invocation writes to `.agent-state/qa-workflow/<run-id>/` in the practice
repository. `result.json` records the domain status and executed stages.
Individual stage JSON files preserve their handoffs. A graph status of
`completed` means only that graph execution stopped normally.

The application does not commit, push, update tickets, or change the product.
It has no checkpoint or restart protocol. After an interrupted process, inspect
the existing repair state and start a new invocation.

## Prompt caching and telemetry

Anthropic runs enable provider-side ephemeral caching for the stable system
prompt and tool definitions. The default TTL is five minutes. Set
`ANTHROPIC_CACHE_TTL=1h` when a longer exercise window is appropriate. A cache
hit reduces repeated prompt processing; it does not preserve workflow state or
prove a test result.

`events.jsonl` records stage, model, tool, duration, status, token, cache-read,
and cache-write metadata. It excludes prompts, file contents, tool arguments,
and tool results. Pass `--otel` only when the existing observability setup and
standard `OTEL_*` environment variables are configured.

## Offline checks

The foundation checks must pass in the distributed starter:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

On macOS or Linux, use `./.venv/bin/python`. These checks use synthetic model
responses and temporary repositories. They do not establish a live provider
call, backend response, Gradle run, Kotlin change, or successful review.

## Practice: Add the Review Route

Connect the supplied `review` agent in `workflow.py`. A verified generation or
verified repair may proceed to review. Immediately before review starts, read
the current evidence again and cancel the node when it is missing or stale.
Do not change agent prompts, repository tools, repair budgets, or evidence
classification for this task.

The practice contract is executable:

```powershell
.\.venv\Scripts\python.exe -m pytest practice-tests/test_review_route.py -q
```

The command is expected to fail in the original starter because the route is
absent. After the route and freshness gate are implemented, it verifies both
the generation-to-review and repair-to-review handoffs, review findings, and
stale-evidence blocking. Run the foundation checks again after it passes.

## Capstone: Run an Evidence-Backed Workflow

Use the completed graph with the assigned complete API case. Start the backend,
set the provider key, and run from `strands-workflow/`. This example uses a
workbook case ID; replace the ID and model with the assigned values.

```powershell
.\.venv\Scripts\python.exe main.py `
  --repo .. `
  --case-file ..\test-cases\test-cases.xlsx `
  --case-id YOUR_CASE_ID `
  --provider anthropic `
  --model YOUR_MODEL_ID
```

On macOS or Linux, use `./.venv/bin/python`, forward slashes, and shell line
continuations. Inspect `result.json`, the executed stage files, matching JUnit
and Allure artifacts, and telemetry events. Report source-only coverage,
verified execution, repair outcomes, and review findings as distinct results.
