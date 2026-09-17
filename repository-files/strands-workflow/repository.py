"""Repository tools and fresh API execution evidence for the QA workflow."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from copy import deepcopy
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from review_packet import build_review_packet, encode_review_packet, evidence_manifest_from_packet

# fmt: off
TEXT_SUFFIXES = {
    ".md", ".kt", ".kts", ".py", ".yaml", ".yml", ".json", ".jsonl", ".txt", ".log",
    ".diff", ".xml", ".html", ".properties", ".bat", ".sh", ".toml",
}
RESULT_DIRS = {
    "junit": "api-tests/build/test-results/test", "allure": "api-tests/build/allure-results",
}
# fmt: on
HIDDEN = {".git", "build", "node_modules", "venv", "answers", "grader", "fixtures", "course"}
SECRET_NAMES = {"local.properties", "gradle.properties", "id_rsa", "id_ed25519"}
LAYERS = {"tests", "client", "model", "testdata"}
TARGET_PATTERN = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}"


class Repository:
    def __init__(self, root: Path, output_dir: Path, case_id: str, api_url="http://127.0.0.1:8080"):
        self.root = Path(root).resolve(strict=True)
        self.output_dir = self._contained(Path(output_dir).resolve())
        if self.output_dir == self.root:
            raise ValueError("Output must be a dedicated directory inside the repository")
        if not re.fullmatch(r"API-[0-9]+", case_id):
            raise ValueError("Use an API case ID such as API-1234")
        self.case_id = case_id
        url = urlsplit(api_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not url.port
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Use an explicit local HTTP backend host and port")
        self.api_url = api_url.rstrip("/")
        self.last_run: dict | None = None
        self.changed_files: set[str] = set()
        self._original: dict[str, str] = {}
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _contained(self, path: Path) -> Path:
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Path must remain inside the repository")
        for candidate in (path, *path.parents):
            if candidate == self.root:
                break
            if candidate.is_symlink() or (
                candidate.exists() and getattr(candidate.stat(), "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("Links and junctions are unavailable to repository tools")
        return path

    def _path(self, value: str) -> Path:
        value = value.replace("\\", "/")
        parts = PurePosixPath(value).parts
        if not value or value.startswith("/") or ":" in value or ".." in parts or "\0" in value:
            raise ValueError("Use a repository-relative path without traversal")
        if any(
            (part != "." and part.endswith((" ", ".")))
            or re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part)
            for part in parts
        ):
            raise ValueError("Unsupported path component")
        return self._contained(self.root.joinpath(*parts))

    def _readable(self, path: Path) -> bool:
        parts = path.relative_to(self.root).as_posix().lower().split("/")
        if any(
            part in SECRET_NAMES
            or part.startswith(".env")
            or any(word in part for word in ("secret", "credential", "password"))
            for part in parts
        ):
            return False
        if path.is_relative_to(self.output_dir):
            return "previous" not in parts and path.suffix.lower() in TEXT_SUFFIXES
        return not any(
            part in HIDDEN or (part.startswith(".") and part != ".agents") for part in parts
        ) and (
            path.suffix.lower() in TEXT_SUFFIXES
            or path.name.lower().endswith(".md.template")
            or path.name == "gradlew"
        )

    def read_file(self, path: str, start_line=1, line_count=200) -> dict:
        file = self._path(path)
        if not self._readable(file) or not file.is_file():
            raise ValueError("File is not available to repository tools")
        if start_line < 1 or not 1 <= line_count <= 400 or file.stat().st_size > 1_000_000:
            raise ValueError("Read exceeds the text limits")
        lines = file.read_text(encoding="utf-8-sig").splitlines()
        return {
            "path": path,
            "total_lines": len(lines),
            "content": "\n".join(
                f"{number}: {line}"
                for number, line in enumerate(
                    lines[start_line - 1 : start_line - 1 + line_count], start_line
                )
            ),
        }

    def _files(self, path="."):
        base = self._path(path)
        if base.is_file():
            if self._readable(base):
                yield base
            return
        if not base.is_dir():
            raise ValueError("Directory does not exist")
        for current, directories, files in os.walk(base, followlinks=False):
            keep = []
            for name in sorted(directories):
                child = Path(current) / name
                try:
                    self._contained(child)
                except ValueError:
                    continue
                if name.lower() not in HIDDEN and (not name.startswith(".") or name == ".agents"):
                    keep.append(name)
            directories[:] = keep
            for name in sorted(files):
                file = Path(current) / name
                try:
                    self._contained(file)
                except ValueError:
                    continue
                if self._readable(file):
                    yield file

    def list_files(self, path=".") -> dict:
        files = []
        for file in self._files(path):
            files.append(file.relative_to(self.root).as_posix())
            if len(files) > 500:
                break
        return {"files": files[:500], "truncated": len(files) > 500}

    def search_text(self, pattern: str, path=".") -> dict:
        if not pattern or len(pattern) > 300:
            raise ValueError("Provide a pattern of 1 to 300 characters")
        expression, matches = re.compile(pattern), []
        for file in self._files(path):
            if file.stat().st_size > 1_000_000:
                continue
            for number, line in enumerate(
                file.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1
            ):
                if expression.search(line):
                    matches.append(
                        {
                            "path": file.relative_to(self.root).as_posix(),
                            "line": number,
                            "text": line[:1000],
                        }
                    )
                    if len(matches) == 100:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _writable(self, path: str) -> Path:
        file = self._path(path)
        relative = file.relative_to(self.root).as_posix()
        parts = relative.split("/")
        plan_path = f"agent_docs/automation-plans/{self.case_id}.md"
        allowed = relative == plan_path or (
            parts[:4] == ["api-tests", "src", "test", "kotlin"]
            and len(parts) > 5
            and parts[4] in LAYERS
            and file.suffix == ".kt"
        )
        protected_file = self._path("scripts/protected-paths.txt")
        protected = [
            line.strip().lower()
            for line in protected_file.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if (
            not allowed
            or not self._readable(file)
            or any(
                relative.lower() == item.rstrip("/")
                or (item.endswith("/") and relative.lower().startswith(item))
                for item in protected
            )
        ):
            raise ValueError("Write is outside the permitted API test layers")
        if (
            relative == plan_path
            and file.exists()
            and self.case_id not in file.read_text(encoding="utf-8-sig")
        ):
            raise ValueError("Existing automation plan belongs to another case; preserve it")
        return file

    def write_file(self, path: str, content: str) -> dict:
        file = self._writable(path)
        if not content.strip() or len(content.encode("utf-8")) > 100_000:
            raise ValueError("Write must contain nonempty bounded text")
        relative = file.relative_to(self.root).as_posix()
        self._original.setdefault(
            relative, file.read_text(encoding="utf-8-sig") if file.exists() else ""
        )
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content.rstrip() + "\n", encoding="utf-8")
        self.changed_files.add(relative)
        return {"path": relative}

    def edit_file(self, path: str, old_text: str, new_text: str) -> dict:
        file = self._writable(path)
        content = file.read_text(encoding="utf-8-sig")
        if not old_text or content.count(old_text) != 1:
            raise ValueError("The old text must match exactly once")
        return self.write_file(path, content.replace(old_text, new_text, 1))

    def diff(self) -> str:
        return "".join(
            "".join(
                difflib.unified_diff(
                    self._original[name].splitlines(keepends=True),
                    (self.root / name).read_text(encoding="utf-8-sig").splitlines(keepends=True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            )
            for name in sorted(self.changed_files)
        )

    def _source_digest(self) -> str:
        result = hashlib.sha256()
        for file in self._files():
            if not file.is_relative_to(self.output_dir):
                result.update(file.relative_to(self.root).as_posix().encode())
                result.update(file.read_bytes())
        return result.hexdigest()

    def source_fingerprint(self) -> str:
        """Fingerprint the readable source state used by a coverage handoff."""
        return self._source_digest()

    def current_evidence(self) -> dict:
        if self.last_run is None:
            return {"status": "NOT_VERIFIED", "reason": "No API execution has completed"}
        if self.last_run.get("source_digest") != self._source_digest():
            return {
                **deepcopy(self.last_run),
                "status": "NOT_VERIFIED",
                "target_status": "NOT_VERIFIED",
                "reason": "Repository sources changed after execution",
            }
        return deepcopy(self.last_run)

    def _run(self, command: list[str], log: Path, input_text=None, timeout=900):
        # Windows resolves the executable before applying the child's cwd.
        # Resolve repository-local wrappers without changing the recorded command.
        executable = Path(command[0])
        if not executable.is_absolute() and (self.root / executable).is_file():
            command = [str(self._contained((self.root / executable).resolve())), *command[1:]]
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            output = getattr(error, "stdout", "") or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            result = subprocess.CompletedProcess(
                command,
                124 if isinstance(error, subprocess.TimeoutExpired) else 127,
                output + "\n" + str(error),
            )
        log.write_text(result.stdout, encoding="utf-8")
        return result

    def _hook_command(self, action: str) -> list[str]:
        return [
            sys.executable,
            str(self._path(".agents/hooks/test_repair.py")),
            "--project-root",
            str(self.root),
            action,
        ]

    def _hook(self, phase: str, command: str, folder: Path, output="", exit_code=0) -> dict:
        event = (
            "PreToolUse"
            if phase == "before"
            else "PostToolUse"
            if exit_code == 0
            else "PostToolUseFailure"
        )
        payload = {
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"output": output, "exit_code": exit_code},
        }
        result = self._run(
            self._hook_command(phase) + ["--adapter", "codex"],
            folder / f"hook-{phase}.log",
            json.dumps(payload),
            30,
        )
        if result.returncode:
            raise RuntimeError(f"Existing {phase} hook failed; inspect its log")
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict) or (
            phase == "before"
            and envelope
            and envelope.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
        ):
            raise RuntimeError("Existing hook returned an unrecognized response")
        if "hook failed" in str(envelope).lower():
            raise RuntimeError("Existing hook could not process execution evidence")
        return envelope

    def repair_action(self, action: str, item_id="", outcome="", reason="") -> dict:
        if action not in {"refresh", "lock", "show", "unlock", "complete"}:
            raise ValueError("Unsupported repair action")
        command = self._hook_command(action)
        if action in {"unlock", "complete"}:
            if not item_id or len(item_id) > 200:
                raise ValueError("An existing repair item ID is required")
            command += ["--id", item_id]
        if action == "complete":
            if outcome not in {"fixed", "blocked", "skipped"} or (
                outcome != "fixed" and not reason.strip()
            ):
                raise ValueError("Use a supported outcome and explain unresolved work")
            command += ["--outcome", outcome]
            if reason:
                command += ["--reason", reason]
        log = self.output_dir / f"repair-{action}-{uuid.uuid4().hex}.log"
        result = self._run(command, log, timeout=30)
        response = {
            "exit_code": result.returncode,
            "output": result.stdout,
            "log": log.relative_to(self.root).as_posix(),
        }
        with suppress(ValueError):
            response["data"] = json.loads(result.stdout)
        return response

    def _inventory(self) -> list[dict]:
        inventory = []
        for file in sorted((self.root / "api-tests/src/test/kotlin/tests").rglob("*.kt")):
            text = self._contained(file).read_text(encoding="utf-8-sig")
            package = re.search(r"(?m)^package\s+([\w.]+)", text)
            klass = re.search(r"\bclass\s+(\w+)", text)
            if not package or not klass:
                raise ValueError(f"Cannot identify test class: {file.name}")
            found = []
            for match in re.finditer(
                r"(?P<annotations>(?:^[ \t]*@\w+(?:\([^\n]*\))?[ \t]*\r?\n)+)"
                r"[ \t]*fun\s+(?P<method>\w+)\s*\(",
                text,
                re.MULTILINE,
            ):
                if not re.search(r"@Test\b", match["annotations"]):
                    continue
                display = re.search(r'@DisplayName\("([^"\n]+)"\)', match["annotations"])
                allure_id = re.search(r'@AllureId\("([^"\n]+)"\)', match["annotations"])
                name = package[1] + "." + klass[1]
                found.append(
                    {
                        "path": file.relative_to(self.root).as_posix(),
                        "class": name,
                        "method": match["method"],
                        "target": name + "." + match["method"],
                        "display": display[1] if display else match["method"],
                        "allure_id": allure_id[1] if allure_id else None,
                    }
                )
            if len(found) != len(re.findall(r"@Test\b", text)):
                raise ValueError(f"Unsupported test declaration in {file.name}")
            inventory.extend(found)
        return inventory

    def coverage_identity(self, target: str) -> dict:
        """Bind source coverage to the selected case's assigned Allure ID."""
        expected_allure_id = self.case_id.removeprefix("API-")
        inventory = self._inventory()
        matching_target = [item for item in inventory if item["target"] == target]
        case_targets = [
            item["target"] for item in inventory if item["allure_id"] == expected_allure_id
        ]
        target_allure_id = matching_target[0]["allure_id"] if len(matching_target) == 1 else ""
        return {
            "case_id": self.case_id,
            "expected_allure_id": expected_allure_id,
            "target": target,
            "target_allure_id": target_allure_id,
            "case_targets": case_targets,
            "matches_case_id": case_targets == [target]
            and len(matching_target) == 1
            and target_allure_id == expected_allure_id,
        }

    @staticmethod
    def _junit_status(case) -> str:
        outcomes = {
            child.tag.rsplit("}", 1)[-1] for child in case.iter() if isinstance(child.tag, str)
        }
        if outcomes & {"failure", "error"}:
            return "failed"
        return "skipped" if "skipped" in outcomes else "passed"

    def _archive_file(self, folder: Path, area: str, name: str) -> Path:
        """Resolve one artifact without permitting a path outside this archived run."""
        folder = self._contained(folder)
        if folder.parent != self.output_dir or not folder.name.startswith("run-"):
            raise ValueError("Execution artifacts must belong to the current workflow output")
        if (
            area not in RESULT_DIRS
            or PurePosixPath(name).name != name
            or "\\" in name
            or ":" in name
        ):
            raise ValueError("Execution artifact has an unsafe archive path")
        area_root = self._contained(folder / area)
        file = self._contained(area_root / name)
        if not file.is_file() or not file.resolve().is_relative_to(area_root.resolve()):
            raise ValueError("Execution artifact is missing or outside its archived run")
        return file

    @staticmethod
    def _http_attachment_kind(name: str) -> str:
        lowered = name.lower()
        if "request" in lowered:
            return "request"
        if "response" in lowered or re.match(r"^http/\d+(?:\.\d+)? [1-5]\d{2}(?:\s|$)", lowered):
            return "response"
        return ""

    def _select_exact_artifacts(
        self,
        folder: Path,
        expected: list[dict],
        target: str,
    ) -> dict:
        """Parse and match the exact artifacts used by both evidence and review."""
        cases = []
        junit_root = folder / "junit"
        for candidate in sorted(junit_root.glob("*.xml")):
            file = self._archive_file(folder, "junit", candidate.name)
            cases.extend(
                {"path": file, "case": case} for case in ET.parse(file).getroot().iter("testcase")
            )

        allure = []
        allure_root = folder / "allure"
        for candidate in sorted(allure_root.glob("*-result.json")):
            file = self._archive_file(folder, "allure", candidate.name)
            report = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("Allure result must be a JSON object")
            if not isinstance(report.get("fullName"), str) or not isinstance(
                report.get("status"), str
            ):
                raise ValueError("Allure results require a test fullName and status string")
            allure.append({"path": file, "report": report})

        matches = {}
        for item in expected:
            names = {item["method"], item["method"] + "()", item["display"]}
            matches[item["target"]] = {
                "junit": [
                    record
                    for record in cases
                    if record["case"].get("classname") == item["class"]
                    and record["case"].get("name") in names
                ],
                "allure": [
                    record
                    for record in allure
                    if record["report"].get("fullName") == item["target"]
                ],
            }
        selected = matches.get(target, {"junit": [], "allure": []})
        attachments = []

        def collect(node) -> None:
            if not isinstance(node, dict):
                raise ValueError("Allure step must be an object")
            entries, steps = node.get("attachments", []), node.get("steps", [])
            if (
                not isinstance(entries, list)
                or not isinstance(steps, list)
                or any(not isinstance(entry, dict) for entry in entries)
            ):
                raise ValueError("Allure steps and attachments must be lists of objects")
            for entry in entries:
                source = entry.get("source", "")
                try:
                    path = (
                        self._archive_file(folder, "allure", source)
                        if isinstance(source, str) and source
                        else None
                    )
                    error = "" if path else "Missing or unsafe HTTP attachment"
                except ValueError:
                    path, error = None, "Missing or unsafe HTTP attachment"
                attachments.append(
                    {
                        "metadata": entry,
                        "path": path,
                        "error": error,
                        "kind": self._http_attachment_kind(str(entry.get("name", ""))),
                    }
                )
            for step in steps:
                collect(step)

        if len(selected["allure"]) == 1:
            collect(selected["allure"][0]["report"])
        return {
            "cases": cases,
            "allure": allure,
            "matches": matches,
            "selected_junit": selected["junit"],
            "selected_allure": selected["allure"],
            "attachments": attachments,
        }

    def _exact_evidence_manifest(self, artifacts: dict, target: str) -> dict:
        """Fingerprint the exact archived evidence selected for one target."""
        junit = artifacts["selected_junit"]
        allure = artifacts["selected_allure"]
        if len(junit) != 1 or len(allure) != 1:
            raise ValueError("Exact evidence requires one JUnit testcase and one Allure result")

        def identity(path: Path, raw: bytes, scope: str) -> dict:
            return {
                "path": path.relative_to(self.root).as_posix(),
                "scope": scope,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        junit_raw = ET.tostring(junit[0]["case"], encoding="utf-8")
        allure_raw = allure[0]["path"].read_bytes()
        http = []
        for attachment in artifacts["attachments"]:
            if not attachment["kind"]:
                continue
            if attachment["error"] or attachment["path"] is None:
                raise ValueError("Exact evidence has a missing or unsafe HTTP attachment")
            path = attachment["path"]
            item = identity(path, path.read_bytes(), "full-attachment")
            item.update(index=len(http), kind=attachment["kind"])
            http.append(item)
        if {item["kind"] for item in http} != {"request", "response"}:
            raise ValueError("Exact evidence requires HTTP request and response attachments")
        return {
            "version": 1,
            "target": target,
            "junit": identity(junit[0]["path"], junit_raw, "matching-testcase"),
            "allure": identity(allure[0]["path"], allure_raw, "full-result"),
            "http": http,
        }

    def _evidence(
        self,
        folder: Path,
        expected: list[dict],
        target: str,
        exit_code: int,
    ) -> dict:
        artifacts = self._select_exact_artifacts(folder, expected, target)
        cases = artifacts["cases"]
        allure = artifacts["allure"]

        counts = {"total": len(cases), "passed": 0, "failed": 0, "skipped": 0}
        for record in cases:
            counts[self._junit_status(record["case"])] += 1
        reasons = []
        inventory_mismatch = (
            not expected or len(cases) != len(expected) or len(allure) != len(expected)
        )
        if inventory_mismatch:
            reasons.append("JUnit/Allure counts do not match the selected target")
        for item in expected:
            matching = artifacts["matches"][item["target"]]
            if (
                len(matching["junit"]) != 1
                or len(matching["allure"]) != 1
                or matching["allure"][0]["report"].get("status") != "passed"
            ):
                reasons.append(f"Missing, ambiguous, or unsuccessful evidence for {item['target']}")
        selected = artifacts["selected_allure"]
        selected_junit = artifacts["selected_junit"]
        attachments = artifacts["attachments"]
        target_reasons = []
        for attachment in attachments:
            if attachment["error"]:
                target_reasons.append("Missing or unsafe HTTP attachment")
        kinds = [item["kind"] for item in attachments]
        if "request" not in kinds or "response" not in kinds:
            target_reasons.append("Target has no attached HTTP request/response evidence")
        if len(selected) != 1 or len(selected_junit) != 1:
            target_reasons.append("Target must have exactly one matching JUnit and Allure result")
        junit_status = (
            self._junit_status(selected_junit[0]["case"]) if len(selected_junit) == 1 else ""
        )
        allure_status = selected[0]["report"].get("status") if len(selected) == 1 else ""
        target_failed = junit_status == "failed" or allure_status in {"failed", "broken"}
        target_passed = (
            junit_status == "passed" and allure_status == "passed" and not target_reasons
        )
        reasons.extend(target_reasons)
        if exit_code or counts["skipped"]:
            reasons.append("Command failed or selected tests were skipped")
        if inventory_mismatch:
            status = "NOT_VERIFIED"
        elif target_failed:
            status = "FAILED"
        elif reasons:
            status = "NOT_VERIFIED"
        else:
            status = "VERIFIED"
        result = {
            "status": status,
            "target_status": "FAILED"
            if target_failed
            else "VERIFIED"
            if target_passed
            else "NOT_VERIFIED",
            "counts": counts,
            "reasons": reasons,
        }
        if target_passed:
            result["evidence_manifest"] = self._exact_evidence_manifest(artifacts, target)
        return result

    def _review_run_folder(self, evidence: dict) -> Path:
        report_value = evidence.get("report", "")
        if not isinstance(report_value, str) or not report_value:
            raise ValueError("Current evidence has no archived run report")
        report = self._path(report_value)
        folder = report.parent
        if (
            report.name != "result.json"
            or not report.is_file()
            or folder.parent != self.output_dir
            or not folder.name.startswith("run-")
        ):
            raise ValueError("Current evidence does not reference this workflow run archive")
        for area, key in (("junit", "junit_dir"), ("allure", "allure_dir")):
            value = evidence.get(key, "")
            if not isinstance(value, str) or self._path(value) != folder / area:
                raise ValueError("Current evidence artifact paths do not match its run archive")
        return folder

    def prepare_review_packet(self, case: str, target: str) -> dict:
        """Build and persist a bounded, redacted packet for the tool-free review agent."""
        if not isinstance(case, str) or not case.strip():
            raise ValueError("Review requires the complete selected case")
        if not isinstance(target, str) or not re.fullmatch(TARGET_PATTERN, target):
            raise ValueError("Review requires an exact package.Class.method target")
        evidence = self.current_evidence()
        if not (
            evidence.get("status") == "VERIFIED"
            and evidence.get("target_status") == "VERIFIED"
            and evidence.get("target") == target
            and evidence.get("source_digest") == self.source_fingerprint()
        ):
            raise ValueError("Review requires current VERIFIED evidence for the exact target")
        folder = self._review_run_folder(evidence)
        matching = [item for item in self._inventory() if item["target"] == target]
        if len(matching) != 1:
            raise ValueError("Review target must identify exactly one current API test")
        if matching[0]["allure_id"] != self.case_id.removeprefix("API-"):
            raise ValueError("Review target Allure ID does not match the selected case")
        artifacts = self._select_exact_artifacts(folder, matching, target)
        junit = artifacts["selected_junit"]
        allure = artifacts["selected_allure"]
        if len(junit) != 1 or len(allure) != 1:
            raise ValueError("Review requires exactly one matching JUnit and Allure result")
        junit_status = self._junit_status(junit[0]["case"])
        if junit_status != "passed" or allure[0]["report"].get("status") != "passed":
            raise ValueError("Review requires passing exact-target JUnit and Allure evidence")
        manifest = evidence.get("evidence_manifest")
        if not isinstance(manifest, dict) or manifest != self._exact_evidence_manifest(
            artifacts, target
        ):
            raise ValueError("Exact execution evidence changed after its verified run")

        packet = build_review_packet(
            self, case, target, evidence, matching[0], artifacts, junit_status
        )
        if manifest != evidence_manifest_from_packet(packet):
            raise ValueError("Review packet evidence differs from its verified run")

        # Packet assembly reads source and artifact files. Recompute both identities
        # afterwards so a concurrent mutation cannot become review input.
        if evidence["source_digest"] != self.source_fingerprint():
            raise ValueError("Repository sources changed while assembling the review packet")
        refreshed = self._select_exact_artifacts(folder, matching, target)
        if manifest != self._exact_evidence_manifest(refreshed, target):
            raise ValueError("Exact execution evidence changed while assembling the review packet")

        encoded = encode_review_packet(packet)
        (self.output_dir / "review-packet.json").write_bytes(encoded + b"\n")
        return packet

    def _format_api_tests(self, wrapper: str, folder: Path):
        """Capture module formatting in the review diff and restore out-of-scope edits."""

        def sources():
            return {
                file.relative_to(self.root).as_posix(): file.read_bytes()
                for file in self._files("api-tests")
                if file.suffix in {".kt", ".kts"}
            }

        before = sources()
        run = self._run([wrapper, ":api-tests:ktlintFormat"], folder / "format.log")
        after = sources()
        changed = sorted(
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        )
        rejected = []
        for name in changed:
            try:
                self._writable(name)
            except ValueError:
                file = self._path(name)
                if name in before:
                    file.write_bytes(before[name])
                elif file.exists():
                    file.unlink()
                rejected.append(name)
                continue
            self._original.setdefault(name, before.get(name, b"").decode("utf-8-sig"))
            self.changed_files.add(name)
        if rejected:
            run = subprocess.CompletedProcess(
                run.args,
                1,
                run.stdout
                + "\nRestored formatter changes outside permitted layers: "
                + ", ".join(rejected),
            )
            (folder / "format.log").write_text(run.stdout, encoding="utf-8")
        return run

    def run_api_test(self, target: str, *, format_sources: bool = True) -> dict:
        if not isinstance(target, str) or not re.fullmatch(TARGET_PATTERN, target):
            raise ValueError("Use an exact package.Class.method target")
        expected = self._inventory()
        matching = [item for item in expected if item["target"] == target]
        if len(matching) != 1:
            raise ValueError("Target must identify exactly one existing API test")
        if matching[0]["allure_id"] != self.case_id.removeprefix("API-"):
            raise ValueError("Selected test Allure ID must match the supplied case number")
        expected = matching
        folder = self.output_dir / ("run-" + uuid.uuid4().hex)
        folder.mkdir()
        (folder / "suite.log").write_text("API test command has not run.\n", encoding="utf-8")
        wrapper = ".\\gradlew.bat" if os.name == "nt" else "./gradlew"
        command = [
            wrapper,
            ":api-tests:test",
            "--rerun",
            f"-Dapi.url={self.api_url}",
            "--tests",
            target,
        ]
        command_text = " ".join(command)

        def relative(name: str) -> str:
            return (folder / name).relative_to(self.root).as_posix()

        summary = {
            "status": "NOT_VERIFIED",
            "target_status": "NOT_VERIFIED",
            "target": target,
            "case_id": self.case_id,
            "command": command_text,
            "log": relative("suite.log"),
            "junit_dir": relative("junit"),
            "allure_dir": relative("allure"),
            "reasons": [],
        }

        def invalidate(reason: str) -> None:
            summary["status"] = "NOT_VERIFIED"
            summary["target_status"] = "NOT_VERIFIED"
            summary["reasons"].append(reason)

        approved = False
        executed_source_digest = None
        run = subprocess.CompletedProcess(command, 127, "Execution did not start")
        try:
            if format_sources:
                # Formatting is a source-changing operation. Generation and repair
                # perform it before PRE; read-only coverage execution skips it.
                run = self._format_api_tests(wrapper, folder)
                summary["format_log"] = relative("format.log")
                summary["format_exit_code"] = run.returncode
                if run.returncode:
                    raise RuntimeError(
                        "API formatting did not complete within the permitted test layers; "
                        "inspect format_log"
                    )
            pre = self._hook("before", command_text, folder)
            denial = pre.get("hookSpecificOutput", {})
            if denial.get("permissionDecision") == "deny":
                summary["reasons"].append(
                    denial.get("permissionDecisionReason", "Existing hook denied execution")
                )
            else:
                approved = True
                for name, path in RESULT_DIRS.items():
                    source = self._path(path)
                    if source.exists():
                        previous = folder / "previous" / name
                        previous.parent.mkdir(exist_ok=True)
                        shutil.move(str(source), str(previous))
                executed_source_digest = self.source_fingerprint()
                run = self._run(command, folder / "suite.log")
                for name, path in RESULT_DIRS.items():
                    source = self._path(path)
                    destination = folder / name
                    destination.mkdir()
                    for file in source.rglob("*") if source.exists() else ():
                        self._contained(file)
                        if file.is_file():
                            copy = destination / file.relative_to(source)
                            copy.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(file, copy)
                summary.update(self._evidence(folder, expected, target, run.returncode))
        except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
            invalidate(str(error))
        finally:
            if approved:
                try:
                    summary["post_hook"] = self._hook(
                        "after", command_text, folder, run.stdout, run.returncode
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    invalidate(str(error))
        current_source_digest = self.source_fingerprint()
        if executed_source_digest is not None and executed_source_digest != current_source_digest:
            invalidate("Sources changed during or after execution")
        summary["exit_code"] = run.returncode
        summary["source_digest"] = executed_source_digest or current_source_digest
        summary["report"] = relative("result.json")
        (folder / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        self.last_run = deepcopy(summary)
        return deepcopy(summary)
