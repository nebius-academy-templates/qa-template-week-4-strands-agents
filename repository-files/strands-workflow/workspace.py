"""Constrained repository file access for workflow agents."""

from __future__ import annotations

import difflib
import hashlib
import re
import stat
from collections.abc import Iterator
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath

_TEXT_SUFFIXES = frozenset(
    {
        ".bat",
        ".diff",
        ".html",
        ".json",
        ".jsonl",
        ".kt",
        ".kts",
        ".log",
        ".md",
        ".properties",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_EXCLUDED_DIRECTORIES = frozenset(
    {".git", "answers", "build", "course", "fixtures", "grader", "node_modules", "venv"}
)
_SENSITIVE_FILE_NAMES = frozenset({"gradle.properties", "id_ed25519", "id_rsa", "local.properties"})
_SENSITIVE_NAME_MARKERS = ("credential", "password", "secret")
_WRITABLE_API_LAYERS = frozenset({"client", "model", "testdata", "tests"})
_WRITABLE_MOBILE_LAYERS = frozenset({"actions", "pages", "testdata", "tests"})

_PROTECTED_PATHS_FILE = PurePosixPath("scripts/protected-paths.txt")
_CASE_ID = re.compile(r"(?:API|MOB)-[0-9]+\Z")
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

_MAX_FILE_BYTES = 1_000_000
_MAX_READ_LINES = 400
_MAX_SEARCH_MATCHES = 100
_MAX_LISTED_FILES = 500
_MAX_WRITE_BYTES = 100_000
_HASH_CHUNK_BYTES = 64 * 1024


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True

    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        return is_junction()

    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def ensure_safe_path(root: Path, path: Path) -> Path:
    """Reject paths that escape the root or pass through a filesystem link."""
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise ValueError("path must remain inside the repository") from error

    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if _is_link_or_junction(candidate):
            raise ValueError("repository tools do not follow symlinks or junctions")
    return path


def _portable_parts(value: str) -> tuple[str, ...]:
    if not value or "\0" in value:
        raise ValueError("path cannot be empty or contain a NUL byte")

    path = PureWindowsPath(value)
    invalid_component = any(":" in part or part.endswith((" ", ".")) for part in path.parts)
    if path.anchor or ".." in path.parts:
        raise ValueError("path must be relative and cannot contain traversal")
    if path.is_reserved() or invalid_component:
        raise ValueError("path contains a component that is not portable")
    return path.parts


def _is_sensitive_name(name: str) -> bool:
    return (
        name in _SENSITIVE_FILE_NAMES
        or name.startswith(".env")
        or any(marker in name for marker in _SENSITIVE_NAME_MARKERS)
    )


def _is_excluded_name(name: str) -> bool:
    return name in _EXCLUDED_DIRECTORIES or (name.startswith(".") and name != ".agents")


def _is_previous_results(name: str) -> bool:
    return name == "previous" or name.startswith("previous-")


def validate_case_id(case_id: str) -> str:
    """Return a canonical API or mobile case ID or raise a diagnostic error."""
    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        raise ValueError(f"case_id must match 'API-<digits>' or 'MOB-<digits>'; got {case_id!r}")
    return case_id


class RepositoryWorkspace:
    """Provide bounded file access and track edits made by workflow tools."""

    def __init__(self, root: Path, output_dir: Path, case_id: str) -> None:
        self.root = Path(root).resolve(strict=True)
        output = Path(output_dir)
        if not output.is_absolute():
            output = self.root / output
        self.output_dir = self.ensure_safe(output)
        if self.output_dir == self.root:
            raise ValueError("output_dir must be below the repository root")

        self.case_id = validate_case_id(case_id)
        self.layer = "mobile" if case_id.startswith("MOB-") else "api"
        self.test_module = "appium-tests" if self.layer == "mobile" else "api-tests"
        self.test_source_root = Path(self.test_module, "src", "test", "kotlin")
        self.changed_files: set[str] = set()
        self._original: dict[str, str] = {}
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def ensure_safe(self, path: Path) -> Path:
        return ensure_safe_path(self.root, path)

    def path_for(self, value: str) -> Path:
        """Resolve a portable repository-relative tool path."""
        if not isinstance(value, str):
            raise ValueError("path must be a repository-relative string")
        return self.ensure_safe(self.root.joinpath(*_portable_parts(value)))

    def is_readable(self, path: Path) -> bool:
        """Return whether a safe path may be exposed to a workflow agent."""
        relative = path.relative_to(self.root)
        parts = tuple(part.casefold() for part in relative.parts)
        if any(_is_sensitive_name(part) for part in parts):
            return False

        is_text = path.suffix.casefold() in _TEXT_SUFFIXES
        if path.is_relative_to(self.output_dir):
            return is_text and not any(_is_previous_results(part) for part in parts)
        if any(_is_excluded_name(part) for part in parts):
            return False

        name = path.name.casefold()
        return is_text or name.endswith(".md.template") or name == "gradlew"

    def read_file(self, path: str, start_line: int = 1, line_count: int = 200) -> dict:
        file = self.path_for(path)
        if not file.is_file() or not self.is_readable(file):
            raise ValueError("file is not available to repository tools")
        if start_line < 1 or not 1 <= line_count <= _MAX_READ_LINES:
            raise ValueError("read exceeds the line limits")
        if file.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError("file exceeds the text size limit")

        lines = file.read_text(encoding="utf-8-sig").splitlines()
        selected = lines[start_line - 1 : start_line - 1 + line_count]
        return {
            "path": path,
            "total_lines": len(lines),
            "content": "\n".join(
                f"{number}: {line}" for number, line in enumerate(selected, start=start_line)
            ),
        }

    def _can_descend(self, directory: Path) -> bool:
        name = directory.name.casefold()
        if _is_excluded_name(name) or _is_sensitive_name(name):
            return False
        return not (directory.is_relative_to(self.output_dir) and _is_previous_results(name))

    def _walk(self, directory: Path) -> Iterator[Path]:
        for child in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
            try:
                child = self.ensure_safe(child)
            except ValueError:
                continue
            if child.is_dir():
                if self._can_descend(child):
                    yield from self._walk(child)
            elif child.is_file() and self.is_readable(child):
                yield child

    def files(self, path: str = ".") -> Iterator[Path]:
        """Yield readable files below a validated repository-relative path."""
        base = self.path_for(path)
        if base.is_file():
            if self.is_readable(base):
                yield base
            return
        if not base.is_dir():
            raise ValueError("directory does not exist")
        yield from self._walk(base)

    def list_files(self, path: str = ".") -> dict:
        selected = list(islice(self.files(path), _MAX_LISTED_FILES + 1))
        return {
            "files": [
                file.relative_to(self.root).as_posix() for file in selected[:_MAX_LISTED_FILES]
            ],
            "truncated": len(selected) > _MAX_LISTED_FILES,
        }

    def search_text(self, pattern: str, path: str = ".") -> dict:
        if not pattern or len(pattern) > 300:
            raise ValueError("pattern must contain 1 to 300 characters")
        try:
            expression = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid search pattern: {error}") from error

        matches = []
        for file in self.files(path):
            if file.stat().st_size > _MAX_FILE_BYTES:
                continue
            with file.open(encoding="utf-8-sig", errors="replace") as lines:
                for number, line in enumerate(lines, 1):
                    if not expression.search(line):
                        continue
                    matches.append(
                        {
                            "path": file.relative_to(self.root).as_posix(),
                            "line": number,
                            "text": line.rstrip("\r\n")[:1000],
                        }
                    )
                    if len(matches) == _MAX_SEARCH_MATCHES:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _is_writable_test_source(self, relative: Path) -> bool:
        try:
            source = relative.relative_to(self.test_source_root)
        except ValueError:
            return False
        return (
            len(source.parts) >= 2
            and source.parts[0].casefold()
            in (_WRITABLE_MOBILE_LAYERS if self.layer == "mobile" else _WRITABLE_API_LAYERS)
            and source.suffix.casefold() == ".kt"
        )

    def _protected_paths(self) -> Iterator[tuple[PurePosixPath, bool]]:
        file = self.path_for(_PROTECTED_PATHS_FILE.as_posix())
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                portable = value.replace("\\", "/")
                yield PurePosixPath(portable.rstrip("/").casefold()), portable.endswith("/")

    def _is_protected(self, relative: Path) -> bool:
        path = PurePosixPath(relative.as_posix().casefold())
        return any(
            path == protected or recursive and path.is_relative_to(protected)
            for protected, recursive in self._protected_paths()
        )

    def writable_path(self, path: str) -> Path:
        """Return an allowed selected-suite path, preserving protected repository files."""
        file = self.path_for(path)
        relative = file.relative_to(self.root)
        plan = Path("agent_docs", "automation-plans", f"{self.case_id}.md")
        is_allowed = relative == plan or self._is_writable_test_source(relative)
        if not is_allowed or not self.is_readable(file) or self._is_protected(relative):
            raise ValueError("write is outside the permitted test layers")
        if file == self.root / plan and file.exists():
            existing = file.read_text(encoding="utf-8-sig")
            if self.case_id not in existing:
                raise ValueError("existing automation plan belongs to another case")
        return file

    def _write(self, file: Path, content: str) -> dict:
        if not content.strip() or len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            raise ValueError("write must contain nonempty bounded text")

        relative = file.relative_to(self.root).as_posix()
        original = file.read_text(encoding="utf-8-sig") if file.exists() else ""
        self._original.setdefault(relative, original)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content.rstrip() + "\n", encoding="utf-8")
        self.changed_files.add(relative)
        return {"path": relative}

    def write_file(self, path: str, content: str) -> dict:
        return self._write(self.writable_path(path), content)

    def edit_file(self, path: str, old_text: str, new_text: str) -> dict:
        file = self.writable_path(path)
        content = file.read_text(encoding="utf-8-sig")
        if not old_text or old_text == new_text or content.count(old_text) != 1:
            raise ValueError("old_text must match exactly once and differ from new_text")
        return self._write(file, content.replace(old_text, new_text, 1))

    def diff(self) -> str:
        changes = []
        for name in sorted(self.changed_files):
            current = (self.root / name).read_text(encoding="utf-8-sig")
            changes.extend(
                difflib.unified_diff(
                    self._original[name].splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            )
        return "".join(changes)

    def source_fingerprint(self) -> str:
        """Hash execution inputs, including untracked and binary source resources.

        Agent-readable logs and reports are outputs, not execution inputs. This
        inventory is independent of both Git tracking and agent read permissions.
        """
        roots = (
            f"{self.test_module}/src",
            "fake-api/src",
            "gradle",
            "buildSrc",
            "build-logic",
            ".agents",
            "agent_docs",
            "scripts",
        )
        if self.layer == "mobile":
            roots += ("app/src",)
        names = {
            "AGENTS.md",
            "AI_POLICY.md",
            "gradlew",
            "gradlew.bat",
            "local.properties",
            "fake-api/openapi.yaml",
        }
        if self.layer == "mobile":
            names.update({"package.json", "package-lock.json"})
        for module in (
            "",
            f"{self.test_module}/",
            "fake-api/",
            *(("app/",) if self.layer == "mobile" else ()),
        ):
            names.update(
                module + name
                for name in (
                    "build.gradle",
                    "build.gradle.kts",
                    "settings.gradle",
                    "settings.gradle.kts",
                    "gradle.properties",
                )
            )

        def inputs(directory: Path) -> Iterator[Path]:
            for child in directory.iterdir():
                self.ensure_safe(child)
                if child.is_relative_to(self.output_dir):
                    continue
                if child.is_dir():
                    if child.name not in {"build", ".gradle", "__pycache__", ".git"}:
                        yield from inputs(child)
                elif child.is_file():
                    yield child

        files = {self.path_for(name) for name in names if self.path_for(name).is_file()}
        for name in roots:
            directory = self.path_for(name)
            if directory.is_dir():
                files.update(inputs(directory))
        digest = hashlib.sha256()
        for file in sorted(files):
            digest.update(file.relative_to(self.root).as_posix().encode())
            digest.update(b"\0")
            digest.update(str(file.stat().st_size).encode() + b"\0")
            with file.open("rb") as source:
                for chunk in iter(lambda: source.read(_HASH_CHUNK_BYTES), b""):
                    digest.update(chunk)
        return digest.hexdigest()
