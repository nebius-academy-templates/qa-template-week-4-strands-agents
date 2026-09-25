# Evidence-backed API and mobile workflow with Strands

This starter coordinates assigned API and mobile cases through readiness, generation and
fresh exact execution, conditional repair, and a final check against each case.
Cases run in the supplied order with separate repository adapters. Cases that
proceed to model execution also use a new graph.
It uses the documents, skills, hook, Kotlin suite and workbook already installed
in the project repository.

The starter supplies four roles. Its graph connects readiness, generation and
conditional repair. The review agent is deliberately not registered as a graph
node. Until the review route is connected, a passing test ends as `VERIFIED`
with review still pending. The batch may continue to later
cases, but those results remain unresolved until reviewed.

## Prerequisites

Use Python 3.11 or newer. Before running the application, verify that the
project repository contains:

- `AGENTS.md` and `agent_docs/AI_POLICY.md`;
- `agent_docs/task-automation-readiness-instructions.md`;
- `.agents/skills/automate-test-case/SKILL.md`;
- `.agents/skills/gen-api-test/SKILL.md`;
- `.agents/skills/test-repair/SKILL.md`;
- `.agents/hooks/test_repair.py` and its installed hook configuration;
- `agent_docs/templates/automation_plan.api.workflow.md.template`;
- `test-cases/test-cases.xlsx`;
- `scripts/protected-paths.txt`.

Start the fake API at `http://127.0.0.1:8080` using `api-tests/README.md`
before a live workflow run. The workflow uses this fixed backend address. Finish unrelated repair work first because the existing
repair hook owns its queue and attempt counters.

For a mobile case, also install `gen-mobile-test`, `run-appium-suite`, and
`agent_docs/templates/automation_plan.mobile.workflow.md.template` from the
workflow skills package. Apply this package's `AppiumTestCase.kt` overlay: it
attaches both `Screen after step` screenshots and `UI page source` XML after
successful steps. The workflow needs both when preparing mobile review evidence.
Capture failures are attached separately and do not change a test's outcome;
missing required captures produce `VERIFICATION_INCOMPLETE` and prevent review.

Follow `appium-tests/README.md` and `run-appium-suite` for the Android SDK, pinned
Appium setup and emulator settings. Keep exactly one Android device connected
and booted, with animations disabled, and start the backend and Appium server.
The host discovers that device and uses the `stable` APK flavor. It calls the
existing OS suite runner for generation and the guarded exact-target Gradle run
for repair; this CLI has no device or flavor selection flag.

## Set up the Python environment

Run these commands from this `strands-workflow` directory.

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS or Linux:

```shell
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Set either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in the current environment.
Do not put a key in this repository. Select the matching provider with
`--provider`. `--model` selects the model for generation and repair;
`--analysis-model` selects the model for readiness and review. If omitted,
the analysis model defaults to `claude-sonnet-5` for Anthropic or `--model`
for OpenAI. One provider is selected for the entire run. The review model is
used once the lesson's review route is connected.
Anthropic generation and repair use effort `high` and `max_tokens=32768`;
readiness and review use effort `medium` and `max_tokens=16384`. The token limit
applies to each model response, including thinking tokens when used, rather
than the entire stage. These Anthropic settings do not change the OpenAI
configuration.

`model_factory(name)` in `make_agents()` assigns the analysis model to readiness
and review, and the implementation model to generation and repair. Each agent
keeps separate conversation state, tools and output schemas. A reused readiness
decision skips construction of the readiness agent. Reused
`BLOCKED` or `NEEDS_CLARIFICATION` decisions end the case without creating models.
The host closes each distinct client once, including clients created before
a setup failure.

## How the application is assembled

The implementation keeps three concerns separate:

| Concern | Location | Effect |
|---|---|---|
| Model instructions | `agents.py` and the installed Markdown procedures | Define what each model call evaluates or produces. |
| Available operations | Tool functions in `repository_tools.py`, backed by `repository.py` | Bound which files and commands each agent can use. |
| Execution dependencies | Edges and conditions in `workflow.py` | Decide which completed result can start another operation. |

`AgentSkills` loads the selected `gen-api-test` or `gen-mobile-test` skill and
`test-repair` instructions on demand.
It does not provide filesystem access, test execution, hook behavior, or proof
of a successful run. The host application supplies those operations separately.

| Agent | Instruction source | Supplied access |
|---|---|---|
| `readiness` | Existing readiness instructions | Read and search repository sources. |
| `generation` | `gen-api-test` or `gen-mobile-test` through `AgentSkills` | Check the assigned ID, create or validate the plan before changes, implement the selected case and run its exact method. |
| `repair` | `test-repair` through `AgentSkills` | Diagnose the selected target, use the existing queue, edit permitted test layers, and rerun that exact target. |
| `review` | Case-check section of `automate-test-case` | Receive the complete case, final test, optional plan and exact-target evidence; inspect called helpers and contracts with restricted `read_file` and `search_text` tools. It may make at most 20 model calls, including structured output. |

Every role also receives the complete original case, `AGENTS.md`, and
`agent_docs/AI_POLICY.md`. The case ID selects the suite: `API-*` uses
`api-tests`, and `MOB-*` uses `appium-tests`. API generation implements the
assigned case under its Allure ID and completes that case's existing method
when needed. An unrelated or duplicate assigned ID produces `BLOCKED`.
Mobile generation follows `gen-mobile-test` coverage preflight: existing full
coverage or an occupied assigned ID produces `BLOCKED` with source evidence;
an uncovered case proceeds to its plan, implementation and exact run.

For a repeated API batch, pass `--skip-implemented` to skip fully implemented
assigned API cases. Before edits or execution, generation compares the existing test and its
helpers with the complete case. If every requirement is implemented by an enabled
test, it returns `ALREADY_IMPLEMENTED` with the target and requirement-to-source
mapping. The host checks the unique assigned ID and unchanged source fingerprint.
The semantic comparison remains the model's assessment. The case then ends
without execution or final review, and the batch continues. This status does not
claim a fresh passing run. Incomplete tests still require implementation and
verification; without the flag, existing API tests also require fresh execution.
Mobile cases retain their skill's coverage preflight; this option does not override it.
Missing, stale or mismatched execution proof produces `VERIFICATION_INCOMPLETE`.

The supplied review invocation hook replaces graph task history with one
`_review_packet` JSON message. The version 2 packet contains the full selected
case; the line-numbered target test; the case plan when present; whitelisted
JUnit and Allure summaries; and ordered HTTP request/response attachments for
API tests. Mobile review receives the ordered Allure steps, runtime UI hierarchy
summaries and labeled PNG images as native model image content. Matching step
paths connect each image and XML capture; captures are sequential observations,
not an atomic snapshot, and do not replace the test's assertions.
The reviewer reads called helpers, including their assertions, and relevant
contracts through `read_file` and `search_text`. These tools expose source and
contract documents; execution artifacts remain available only in the packet.
The reviewer requests independent source reads together when their paths are
known; the host still executes those tool requests sequentially.
Packet construction does not parse Kotlin dependencies. A source change during
review invalidates the final execution claim.

Each packet artifact records its repository path, MIME type, byte size, and
SHA-256 digest. The run report also records a manifest for the selected JUnit
testcase, complete raw Allure result, and the HTTP or UI attachments. Review
compares the assembled packet with that manifest, then rechecks the archive and
current execution evidence before returning it. The Allure summary and hash use
the same captured bytes. Raw Allure parameters, details, host/thread metadata,
and free-form names are not model input. HTTP HTML
is converted to text. Authorization, cookie, API-key, sandbox-session, and token
values are redacted before model input. Missing, ambiguous, unsafe, non-text,
stale, or oversized required evidence stops review instead of producing a
partial packet. The complete packet is limited to 128 KiB; each HTTP attachment
is limited to 16 KiB and HTTP evidence to 64 KiB total. Mobile packets have a
256 KiB text limit, up to 20 screenshots, a 5 MiB and 16-million-pixel limit per
image, and 20 MiB of images total. UI XML is limited to 256 KiB per attachment;
its bounded element summaries mark any omitted details. Password fields are
redacted from those summaries. Screenshots retain their visible screen content.

For workbook input, the host reuses only exact readiness values from
`Case Summary.Automated Test`: `READY FOR AUTOMATION`, `BLOCKED`, and
`NEEDS_CLARIFICATION`. `TODO`, an empty cell, and a test source path are not
readiness results. The default mode calls the readiness model only when no
reusable status exists. Pass `--reassess-readiness` to request a new assessment
even when the workbook has a status. For a curated batch whose cases have
already been prepared in the workbook, pass `--prepared-cases`. This mode
accepts XLSX input only. Statusless cases then use deterministic workbook shape
checks plus non-empty values in the required selected-case fields, and continue
to generation without a separate readiness model call. Ordinary workbook loading
always checks the required sheets, columns, and one selected row per case, but
the non-empty-field gate belongs only to prepared mode. This preflight is not a
new semantic readiness opinion.

## State, evidence, and reports

`state.py` defines the structured result of each operation. Generation and repair
retain the selected exact target, changed files, current diff, command log and
matching execution evidence. `workspace.py` restricts file access, `repository.py`
integrates guarded execution, and `evidence.py` validates archived JUnit and Allure
results against the selected target and current source fingerprint.

Generation and repair format the permitted layers of the selected test suite before PRE and run
only the selected `package.Class.method`. Formatter changes to files outside the
selected test and files edited by this workflow are restored. Repair starts only
from a confirmed failure of that method and must verify the same target. A full regression suite
is a separate request. Before each run, existing reports are archived; only fresh
matching reports can verify the test. Previous unrelated results are preserved
for the repair queue.

`process_runner.py` uses an owned process group or Windows Job Object to stop
timed-out processes. Unconfirmed cleanup leaves execution unverified. Source
fingerprints include relevant new files and resources; generated backend logs
do not invalidate a run.

Each invocation writes a batch report to
`.agent-state/qa-workflow/<run-id>/result.json`. Per-case stage reports and
results are stored under `cases/001-<case-id>/`, `cases/002-<case-id>/`, and so
on. Each per-case report contains a terminal `next_action` computed from its
final status, and the batch copies that action into the corresponding case
entry. The batch report records the selected model IDs under `analysis` and
`implementation`, and the selected readiness mode.
Each per-case report records one of `model`,
`model_reassessment`, `workbook_status`, or `prepared_preflight` as the
readiness source. Reused and deterministic readiness also have a standalone
`readiness.json`; they are recorded stages but are not counted as model graph
execution. Every selected case is attempted in order. A final
`VERIFICATION_INCOMPLETE`, `INFRASTRUCTURE_ISSUE`, `FAILED`, unknown outcome,
or case-level exception is recorded without stopping the remaining cases.
`VERIFIED` also allows continuation but still requires review. A case setup or
workflow exception produces a `FAILED` case report with error details and a traceback.

Before each case, the runner inspects the installed repair queue. Unfinished
work is recorded as `repair_queue_unfinished: true` in the batch's case entry;
it does not stop the batch or modify, unlock, or complete queue items. The
existing per-test repair hooks still enforce their guards. A pending or
conflicting repair item may prevent that case's test from running; retain the
reported missing evidence and resolve the queue before rerunning affected cases.

After every case has been processed, `COMPLETED` means every result is `REVIEWED`
or an explicitly allowed `ALREADY_IMPLEMENTED`, with no case errors. Other outcomes produce
`COMPLETED_WITH_ISSUES`, a list of `unresolved_case_ids`, and a nonzero CLI exit.
`STOPPED` is reserved for interruption or a runner-level failure, such as an
unreadable shared repair queue, report persistence failure, telemetry failure,
or a test process that could not be stopped. It retains the remaining case IDs
and the reason continuation was blocked.
Each case keeps a separate plan at
`agent_docs/automation-plans/<case-id>.md`, so processing a later case does not
replace an earlier case's plan. Reports from agent invocations contain the domain
result plus duration, cycle count, model latency, token counts, cache counts when the
provider reports them, and per-tool name, count, success, error, and total-time
values. These metrics are selected directly from the Strands result; raw metric
summaries, messages, tool arguments, and tool results are not serialized. The
metrics are not added to the next agent's input. A graph status of `completed`
means only that graph execution stopped normally. Reused workbook and prepared
preflight readiness decisions have no model metrics because they invoke no model.
The batch report is saved before and after each case. Case setup and workflow
exceptions are saved in that case's report and batch entry; processing continues.
Runner-level errors retain the completed cases, remaining order and failure details.
Exhausting a stage's model-call budget produces `VERIFICATION_INCOMPLETE` with
`error_type: ModelCallLimitExceeded`, `error_code: MODEL_CALL_LIMIT`, the exact
`error_stage`, and `model_call_limit` containing `calls` and `maximum`. Inspect
the recorded stage budget and its work before retrying; this error does not mean
the model refused to return the structured schema. The final structured-output
request also counts toward the budget. The original traceback is retained in
the case's `error.log`.
The limits are 60 model calls for generation, 20 for review, and 120 each for
readiness and repair. These limits do not replace the repair hook's attempt limit.

An interrupted stage retains the native usage and tool counters recorded before
the failure in its stage JSON and case report, with `metrics.partial: true`.
Usage from a provider response that never arrived may be missing; these counters
are not a complete billing total. Unavailable measurements are `null`.

When a case reaches review, successful packet preparation writes
`review-packet.json` with the initial redacted review input to that case's directory.
The private `_review_packet` message is not copied into stage JSON or the case
`result.json`.

The application does not commit, push, update tickets, or change the product.
It has no checkpoint or restart protocol. After an interrupted process, inspect
the existing repair state and start a new invocation.

## Prompt caching and observability

Before creating provider models, the application saves the initial batch report
and prints its path. It then prints a short message when each stage starts and
its finalized status when the stage finishes. Interrupted stages report
`VERIFICATION_INCOMPLETE`; inspect the saved error fields and `error.log` for
details. After adding the review evidence check in lesson 4.5, a cancelled review
has no `started` message because its agent did not run.

```text
Batch report: /project/.agent-state/qa-workflow/<run-id>/result.json
[API-2009] generation: started
[API-2009] generation: VERIFIED
```

Progress is flushed immediately to stderr without prompts, tool arguments,
tool results, model responses or exception messages. The final JSON summary
remains on stdout. Stage JSON files are saved when a stage finishes or fails.
The initial batch report is available while the first stage is running; it is
not a live trace of every model request. A stage can spend time in a model or
tool call between progress messages.

For Anthropic, `tokens.total` is uncached input plus output; `cache_read_input`
and `cache_write_input` are reported separately. For OpenAI, cached prompt tokens
are already included in `tokens.input` and `tokens.total`, so do not add them again.
The pinned Strands 1.55.1 adapters for Anthropic and OpenAI do not measure
provider latency; `model.latency_ms` is zero. This does not mean an instantaneous
response. `duration_ms` measures elapsed stage time, including model and tool work.

Anthropic runs enable provider-side ephemeral caching for the stable system
prompt and tool definitions. The default TTL is five minutes. Set
`ANTHROPIC_CACHE_TTL=1h` when a longer cache lifetime is appropriate. A cache
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

## Review configuration

Connect the supplied `review` agent in `workflow.py`. A verified generation or
verified repair may proceed to review. Immediately before review starts, read
the current evidence again and cancel the node when it is missing or stale.
Do not change agent prompts, repository tools, repair budgets, or evidence
classification for this task. After the change, verified generation and repair
results must reach review with the prepared packet, while missing or stale
evidence must keep review from running. A completed review with findings and no
unverified claims or unresolved questions produces `CHANGES_REQUESTED`. An
incomplete comparison, unverified claim or unresolved question produces
`NEEDS_INVESTIGATION`, even when findings are present.

## Run

Use the completed graph with one or more assigned complete API or mobile cases. Start the
backend and any required mobile services, set the provider key, and run from `strands-workflow/`. Supply case IDs
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
  --case-id API-2010 MOB-1007 `
  --provider anthropic `
  --model claude-opus-5 `
  --analysis-model claude-sonnet-5
```

The readiness options are described under
[How the application is assembled](#how-the-application-is-assembled).
`--model` is required. Use `--analysis-model` to override the analysis default.
When updating commands from the four-role CLI, replace `--generation-model`
and `--repair-model` with one `--model` value, and `--readiness-model` and
`--review-model` with one `--analysis-model` value. The four former flags are
no longer accepted.

On macOS or Linux, use `./.venv/bin/python`, forward slashes, and shell line
continuations. Inspect the batch `result.json`, each per-case result and stage
file, and matching JUnit and Allure artifacts. If native OTLP tracing was
enabled, use the trace backend for detailed chronology. Report source-only
implementation decisions, verified execution, repair outcomes, and review findings as distinct
results.

### Use OpenAI

Set `OPENAI_API_KEY` in the environment and select `--provider openai`.
This run does not require `ANTHROPIC_API_KEY`. The existing `requirements.txt`
includes the OpenAI adapter; no dependency change is needed. The application
reads environment variables directly and does not load a `.env` file.

From `strands-workflow/`, with the practice checkout as its parent:

```powershell
.\.venv\Scripts\python.exe main.py `
  --repo .. `
  --case-file ..\test-cases\test-cases.xlsx `
  --case-id API-2010 `
  --provider openai `
  --model gpt-4.1
```

On macOS/Linux use `.venv/bin/python`, forward slashes, and `\` for line
continuations. Omit `--analysis-model` to use the selected OpenAI model for
both groups, or supply another OpenAI model ID for readiness and review.

The pinned Strands adapter uses Chat Completions. Select a model with streaming
and function calling; mobile review also needs image input.
[GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1) supports these
capabilities and is an example, not a required model. A model that requires
Responses API for tool calls is incompatible with this adapter.

A live run with your API key is needed to verify the selected model against a
course case in your environment.
