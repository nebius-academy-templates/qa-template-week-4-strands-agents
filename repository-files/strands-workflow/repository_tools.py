"""Strands tools using the repository supplied for the current invocation."""

from pathlib import Path

from repository import Repository
from strands import ToolContext, tool


@tool(context=True)
def read_file(
    path: str, start_line: int = 1, line_count: int = 200, *, tool_context: ToolContext
) -> dict:
    """Read repository text with line numbers; continue reading truncated files.

    Args:
        path: Repository-relative file path.
        start_line: First line, starting at one.
        line_count: Number of lines, at most 400.
    """
    return tool_context.invocation_state["repository"].read_file(path, start_line, line_count)


@tool(context=True)
def list_files(path: str = ".", *, tool_context: ToolContext) -> dict:
    """List readable files under a repository-relative directory or file."""
    return tool_context.invocation_state["repository"].list_files(path)


@tool(context=True)
def search_text(pattern: str, path: str = ".", *, tool_context: ToolContext) -> dict:
    """Search repository text with a regular expression, returning path and line."""
    return tool_context.invocation_state["repository"].search_text(pattern, path)


_REVIEW_SOURCE_ROOTS = (
    "api-tests/src/test",
    "appium-tests/src/test",
    "app/src",
    "fake-api/src",
    "agent_docs",
    "docs",
    ".agents/skills",
)
_REVIEW_DOCUMENTS = (
    "AGENTS.md",
    "AI_POLICY.md",
    "baseline_report.md",
    "README.md",
    "api-tests/README.md",
    "appium-tests/README.md",
    "fake-api/openapi.yaml",
    "automation_plan.api.md.template",
    "automation_plan.mobile.md.template",
)


def _review_permitted(repository: Repository, file: Path) -> bool:
    if file.is_relative_to(repository.output_dir) or not repository.is_readable(file):
        return False
    if file in {repository.root / path for path in _REVIEW_DOCUMENTS}:
        return True
    return any(file.is_relative_to(repository.root / path) for path in _REVIEW_SOURCE_ROOTS) and (
        file.suffix.lower() in {".kt", ".kts", ".java", ".xml", ".yaml", ".yml", ".md"}
        or file.name.endswith(".md.template")
    )


@tool(name="read_file", context=True)
def review_read_file(
    path: str, start_line: int = 1, line_count: int = 200, *, tool_context: ToolContext
) -> dict:
    """Read sources or contracts; execution artifacts are available only in the packet."""
    repository = tool_context.invocation_state["repository"]
    if not _review_permitted(repository, repository.path_for(path)):
        raise ValueError("Reviewer tools may read only source and contract documents")
    return repository.read_file(path, start_line, line_count)


@tool(name="search_text", context=True)
def review_search_text(pattern: str, path: str = ".", *, tool_context: ToolContext) -> dict:
    """Search source and contract documents without reading logs or execution artifacts."""
    repository = tool_context.invocation_state["repository"]
    base = repository.path_for(path)
    source_roots = [repository.root / path for path in _REVIEW_SOURCE_ROOTS]
    documents = [repository.root / path for path in _REVIEW_DOCUMENTS]
    roots = [
        base if base.is_relative_to(root) else root
        for root in source_roots
        if base.is_relative_to(root) or root.is_relative_to(base)
    ]
    roots.extend(file for file in documents if file == base or file.is_relative_to(base))
    if not roots:
        raise ValueError("Reviewer tools may search only source and contract documents")
    matches = []
    for root in sorted(set(roots)):
        if not root.exists():
            continue
        for file in repository.files(root.relative_to(repository.root).as_posix()):
            if not _review_permitted(repository, file):
                continue
            result = repository.search_text(pattern, file.relative_to(repository.root).as_posix())
            matches.extend(result["matches"])
            if result["truncated"] or len(matches) >= 100:
                return {"matches": matches[:100], "truncated": True}
    return {"matches": matches, "truncated": False}


@tool(context=True)
def write_file(path: str, content: str, *, tool_context: ToolContext) -> dict:
    """Write an allowed test-layer file or this case's automation plan as UTF-8."""
    return tool_context.invocation_state["repository"].write_file(path, content)


@tool(context=True)
def edit_file(path: str, old_text: str, new_text: str, *, tool_context: ToolContext) -> dict:
    """Replace one exact text occurrence in an allowed test-layer or plan file."""
    return tool_context.invocation_state["repository"].edit_file(path, old_text, new_text)


@tool(context=True)
def workflow_diff(*, tool_context: ToolContext) -> str:
    """Read the complete diff produced by this workflow, including new files."""
    return tool_context.invocation_state["repository"].diff()


@tool(context=True)
def execution_evidence(*, tool_context: ToolContext) -> dict:
    """Read the latest run counts, target result and artifact paths; detect stale proof."""
    return tool_context.invocation_state["repository"].current_evidence()


@tool(context=True)
def run_api_test(target: str, *, tool_context: ToolContext) -> dict:
    """Format the API module and run the selected method fresh.

    Args:
        target: Fully qualified package.Class.method belonging to the supplied case.
    """
    return tool_context.invocation_state["repository"].run_api_test(target)


@tool(context=True)
def run_mobile_test(target: str, *, tool_context: ToolContext) -> dict:
    """Run the selected Appium method through the installed OS suite runner and verify it.

    Args:
        target: Fully qualified package.Class.method belonging to the supplied mobile case.
    """
    return tool_context.invocation_state["repository"].run_mobile_test(target)


def _selected_repair_target(tool_context: ToolContext) -> str:
    state = tool_context.invocation_state
    if not state.get("repair_target"):
        state["repair_target"] = (state["repository"].last_run or {}).get("target", "")
    if not state["repair_target"]:
        raise ValueError("Repair requires the exact target from the preceding exact run")
    return state["repair_target"]


@tool(context=True)
def run_repair_test(target: str, *, tool_context: ToolContext) -> dict:
    """Run the original test target through its existing runner and PRE/POST guard.

    Args:
        target: The same fully qualified package.Class.method as the preceding failed run.
    """
    if target != _selected_repair_target(tool_context):
        raise ValueError("Repair may run only the original workflow target")
    repository = tool_context.invocation_state["repository"]
    if getattr(repository, "layer", "api") == "mobile":
        return repository.run_mobile_test(target, repair=True)
    return repository.run_api_test(target)


@tool(context=True)
def repair_action(
    action: str,
    item_id: str = "",
    outcome: str = "",
    reason: str = "",
    *,
    tool_context: ToolContext,
) -> dict:
    """Call the existing repair queue CLI; it owns locks, attempts and completion.

    Args:
        action: refresh, lock, show, unlock or complete.
        item_id: ID returned by the queue; required for unlock and complete.
        outcome: fixed, blocked or skipped; required for complete.
        reason: Evidence-based reason for unresolved work.
    """
    _selected_repair_target(tool_context)
    repository = tool_context.invocation_state["repository"]
    return repository.repair_action(action, item_id, outcome, reason)
