# Evidence-backed API workflow with Strands

This starter coordinates one or more complete API cases through readiness
selection, coverage comparison, generation and fresh execution, conditional
repair, and a read-only check against each original case. Cases run in the
supplied order, with a fresh graph and repository adapter for each case. It uses
the documents, skills, hook, Kotlin suite, and case workbook already installed
in the practice repository.

The starter supplies five roles and the deterministic repository adapter. Its
graph connects readiness, coverage, generation, and conditional repair. The
review agent is deliberately not registered as a graph node until the
review-route practice is completed.

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
available models explicitly when running `main.py`. The analysis model handles
readiness, coverage, and the later review route. The implementation model is
used only when coverage reports a real gap or an exact failing target enters
repair. For Anthropic, both model configurations request `medium` effort.

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
| `coverage` | Coverage preflight in `gen-api-test` through `AgentSkills` | Bind coverage to the selected case's assigned Allure ID, then run that equivalent existing test's exact target when found. |
| `generation` | Remaining `gen-api-test` procedure through `AgentSkills` | Reuse a `GAP` handoff, plan, edit permitted API test layers, and run the selected API test method fresh. |
| `repair` | `test-repair` through `AgentSkills` | Diagnose the selected target, use the existing queue, edit permitted test layers, and rerun that exact target. |
| `review` | Case-check section of `automate-test-case` | Receive one host-prepared packet with the complete case, final test, helpers, optional plan, and exact-target evidence. It has no repository tools and may make at most two model calls. |

Every role also receives the complete original case, `AGENTS.md`, and
`agent_docs/AI_POLICY.md`. A generated status cannot replace the host's JUnit
and Allure validation. Equivalent source coverage is reported as
`ALREADY_COVERED` only when the test carries the selected case's assigned Allure
ID, a matching exact-target run verifies it, and no source changed. A semantically
similar test under another ID is an implementation reference and leaves the
selected case as a `GAP`. A bare source-level coverage claim or evidence for
another target is `NOT_VERIFIED`. An occupied assigned Allure ID without
equivalent behavior is reported as `BLOCKED`.

The supplied review invocation hook replaces graph task history with one
`_review_packet` JSON message. The packet contains the full selected case; the
line-numbered target test; its transitive repository-local Kotlin helpers from
the API test source tree; the case plan when present; whitelisted JUnit and
Allure summaries; and ordered HTTP request/response attachments. Therefore, once
the practice connects the review
node behind the existing freshness gate, the review model does not repeat
repository discovery or receive a failed pre-repair run or workflow diff.

Each packet artifact records its repository path, MIME type, byte size, and
SHA-256 digest. The run report also records a manifest for the selected JUnit
testcase, complete raw Allure result, and each ordered HTTP attachment; review
recomputes that manifest before and after packet assembly. Raw Allure parameters,
details, host/thread metadata, and free-form names are not model input. HTTP HTML
is converted to text. Authorization, cookie, API-key, sandbox-session, and token
values are redacted before model input. Missing, ambiguous, unsafe, non-text,
stale, or oversized required evidence stops review instead of producing a
partial packet. The complete packet is limited to 128 KiB; each HTTP attachment
is limited to 16 KiB and HTTP evidence to 64 KiB total.

For workbook input, the host reuses only exact readiness values from
`Case Summary.Automated Test`: `READY FOR AUTOMATION`, `BLOCKED`, and
`NEEDS_CLARIFICATION`. `TODO`, an empty cell, and a test source path are not
readiness results. The default mode calls the readiness model only when no
reusable status exists. Pass `--reassess-readiness` to request a new assessment
even when the workbook has a status. For a curated batch whose cases have
already been prepared in the course workbook, pass `--prepared-cases`. This mode
accepts XLSX input only. Statusless cases then use deterministic workbook shape
checks plus non-empty values in the required selected-case fields, and continue
to coverage without a separate readiness model call. Ordinary workbook loading
always checks the required sheets, columns, and one selected row per case, but
the non-empty-field gate belongs only to prepared mode. This preflight is not a
new semantic readiness opinion.

## State, evidence, and reports

`state.py` defines the structured result of each operation. An equivalent test
run, generation, or repair receives the selected `Class.method`, changed files,
current diff, command log, JUnit counts, and Allure attachment evidence. A
coverage `GAP` has no execution result to attach. Instead, the host binds that
handoff to the selected case ID and a source fingerprint, then allows generation
only while both still match and no workflow source change exists. A missing or
stale handoff becomes `NOT_VERIFIED`.

Coverage executes an exact target only when it finds an equivalent existing
test. This read-only coverage run skips Kotlin formatting so deduplication cannot
change source. Generation and repair retain formatting because they may have
edited permitted test layers. Generation runs only after coverage returns `GAP`, and it also executes
only the exact selected `Class.method`. If either exact target fails, repair
reruns the same method through the existing repair guard. The repair can
complete only when the preceding stage established failure for that exact
target and the post-repair run verifies the same target. The repository adapter
always adds `--tests <package.Class.method>`; it exposes no broad-suite option.

Each invocation writes a batch report to
`.agent-state/qa-workflow/<run-id>/result.json`. Per-case stage reports and
results are stored under `cases/001-<case-id>/`, `cases/002-<case-id>/`, and so
on. Each per-case report contains a terminal `next_action` computed from its
final status, and the batch copies that action into the corresponding case
entry. The batch report records the analysis and implementation models and the
selected readiness mode. Each per-case report records one of `model`,
`model_reassessment`, `workbook_status`, or `prepared_preflight` as the
readiness source. Reused and deterministic readiness also have a standalone
`readiness.json`; they are recorded stages but are not counted as model graph
execution. A case runs only after the preceding case finishes with `REVIEWED`, or with
`ALREADY_COVERED` backed by unchanged source and matching verified exact-target
evidence; any other status stops the batch before the next case can modify the
same checkout. Each case keeps a separate plan at
`agent_docs/automation-plans/<case-id>.md`, so processing a later case does not
replace an earlier case's plan. Each stage report contains its domain result plus
duration, cycle count, model latency, token counts, cache counts when the
provider reports them, and per-tool name, count, success, error, and total-time
values. These metrics are selected directly from the Strands result; raw metric
summaries, messages, tool arguments, and tool results are not serialized. The
metrics are not added to the next agent's input. A graph status of `completed`
means only that graph execution stopped normally.

After the review route is connected, each case directory also receives a
`review-packet.json` containing the exact redacted model input. The private
`_review_packet` message is not copied into stage JSON or the case
`result.json`.

The application does not commit, push, update tickets, or change the product.
It has no checkpoint or restart protocol. After an interrupted process, inspect
the existing repair state and start a new invocation.

## Prompt caching and observability

Anthropic runs enable provider-side ephemeral caching for the stable system
prompt and tool definitions. The default TTL is five minutes. Set
`ANTHROPIC_CACHE_TTL=1h` when a longer exercise window is appropriate. A cache
hit reduces repeated prompt processing; it does not preserve workflow state or
prove a test result.

The application does not create a second event log. Use `result.json` and the
stage reports for local outcomes and aggregates. When a detailed chronology is
needed, pass `--otel` to enable the native Strands OTLP trace exporter. Do this
only when the existing observability setup and standard `OTEL_*` environment
variables are configured. Strands exports prompt and tool content unredacted by
default, so explicitly require full redaction before using `--otel`:

```powershell
$env:OTEL_SEMCONV_STABILITY_OPT_IN = "gen_ai_unredacted_attributes="
```

On macOS/Linux, use
`export OTEL_SEMCONV_STABILITY_OPT_IN="gen_ai_unredacted_attributes="`.
Other semantic-convention options may be added as comma-separated values, but
do not add attribute names after `gen_ai_unredacted_attributes=`. The
application validates this setting and does not rewrite the process
environment. The model-call limit remains active independently of whether
native tracing is enabled.

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

Use the completed graph with one or more assigned complete API cases. Start the
backend, set the provider key, and run from `strands-workflow/`. Supply case IDs
in their required execution order. This example uses two workbook cases;
replace the IDs and model with the assigned values. A `.md` or `.txt` case file
can be used only when one case ID is supplied, and its content must explicitly
include that ID. The workbook must contain the `Case Summary` and `Steps`
worksheets with their standard columns: `Case ID`, `Title`, `Description`,
`Preconditions`, `Actions`, and `Expected Results`.

```powershell
.\.venv\Scripts\python.exe main.py `
  --repo .. `
  --case-file ..\test-cases\test-cases.xlsx `
  --case-id FIRST_CASE_ID SECOND_CASE_ID `
  --provider anthropic `
  --model claude-opus-5 `
  --analysis-model claude-sonnet-5 `
  --prepared-cases
```

Use `--reassess-readiness` for the full teaching readiness flow. Without either
readiness flag, cases without a stored status run model readiness and cases with
a status reuse it. For Anthropic, omitting `--analysis-model` still uses
`claude-sonnet-5` for readiness, coverage, and review. For OpenAI, it keeps the
older single-model behavior by using `--model` for every role unless an analysis
model is supplied.

On macOS or Linux, use `./.venv/bin/python`, forward slashes, and shell line
continuations. Inspect the batch `result.json`, each per-case result and stage
file, and matching JUnit and Allure artifacts. If native OTLP tracing was
enabled, use the trace backend for detailed chronology. Report source-only
coverage, verified execution, repair outcomes, and review findings as distinct
results.
