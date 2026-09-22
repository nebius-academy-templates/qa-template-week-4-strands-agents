"""Selection and verification of archived JUnit and Allure evidence."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from workspace import RepositoryWorkspace

ARCHIVE_AREAS = frozenset({"allure", "junit"})
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_SCREENSHOT_PIXELS = 16_000_000
MAX_MOBILE_SCREENSHOTS = 20
MAX_UI_PAGE_SOURCE_BYTES = 256 * 1024


def parse_ui_page_source(raw: bytes) -> ET.Element:
    """Parse bounded Android UI XML, without accepting DTDs or entities."""
    if len(raw) > MAX_UI_PAGE_SOURCE_BYTES:
        raise ValueError("UI page source exceeds the evidence limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("UI page source is not UTF-8 text") from error
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("UI page source must not contain a DTD or entity declarations")
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("UI page source is not valid XML") from error


def mobile_step_evidence_reasons(artifacts: dict) -> list[str]:
    """Require a paired screenshot and XML capture for every completed step."""
    steps = artifacts.get("steps", [])
    attachments = artifacts["attachments"]
    reasons = []
    if not steps:
        reasons.append("Mobile evidence has no recorded scenario steps")
    for step in steps:
        label = ".".join(map(str, step["step_path"]))
        if step["status"] != "passed":
            reasons.append(f"Mobile step {label} has no passing step result")
            continue
        selected = [item for item in attachments if item["step_path"] == step["step_path"]]
        for name in ("Screen after step", "UI page source"):
            matches = [item for item in selected if item["metadata"].get("name") == name]
            if len(matches) != 1:
                reasons.append(f"Mobile step {label} requires exactly one {name} attachment")
            elif matches[0]["error"] or matches[0]["path"] is None:
                reasons.append(f"Mobile step {label} has missing or unsafe {name} evidence")
    screenshot_count = sum(item["kind"] == "screenshot" for item in attachments)
    if screenshot_count > MAX_MOBILE_SCREENSHOTS:
        reasons.append(
            f"Mobile evidence contains {screenshot_count} screenshots; "
            f"the explicit review limit is {MAX_MOBILE_SCREENSHOTS}"
        )
    return reasons


def png_dimensions(raw: bytes) -> tuple[int, int]:
    """Read bounded Appium screenshot dimensions without decoding the PNG."""
    if len(raw) > MAX_SCREENSHOT_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Screenshot must be a bounded PNG image")
    if len(raw) < 33 or raw[8:16] != b"\x00\x00\x00\rIHDR":
        raise ValueError("Screenshot PNG has no complete IHDR header")
    width, height = struct.unpack_from(">II", raw, 16)
    if not width or not height or width * height > MAX_SCREENSHOT_PIXELS:
        raise ValueError("Screenshot PNG dimensions exceed the image limit")
    return width, height


class ExecutionEvidence:
    """Read one run archive and bind its artifacts to an exact test target."""

    def __init__(self, workspace: RepositoryWorkspace) -> None:
        self.workspace = workspace

    @staticmethod
    def junit_status(case) -> str:
        outcomes = {
            child.tag.rsplit("}", 1)[-1] for child in case.iter() if isinstance(child.tag, str)
        }
        if outcomes & {"failure", "error"}:
            return "failed"
        return "skipped" if "skipped" in outcomes else "passed"

    def _archive_file(self, folder: Path, area: str, name: str) -> Path:
        """Resolve one artifact without permitting a path outside this run."""
        workspace = self.workspace
        folder = workspace.ensure_safe(folder)
        if folder.parent != workspace.output_dir or not folder.name.startswith("run-"):
            raise ValueError("execution artifacts must belong to the current workflow output")
        if (
            area not in ARCHIVE_AREAS
            or PurePosixPath(name).name != name
            or "\\" in name
            or ":" in name
        ):
            raise ValueError("execution artifact has an unsafe archive path")

        area_root = workspace.ensure_safe(folder / area)
        file = workspace.ensure_safe(area_root / name)
        if not file.is_file() or not file.resolve().is_relative_to(area_root.resolve()):
            raise ValueError("execution artifact is missing or outside its archived run")
        return file

    @staticmethod
    def _http_attachment_kind(name: str) -> str:
        lowered = name.lower()
        # Response reason phrases can contain "request", as in 400 Bad Request.
        if re.match(r"^http/\d+(?:\.\d+)? [1-5]\d{2}(?:\s|$)", lowered):
            return "response"
        if "request" in lowered:
            return "request"
        if "response" in lowered:
            return "response"
        return ""

    def _attachment_kind(self, metadata: dict) -> str:
        if getattr(self.workspace, "layer", "api") == "api":
            return self._http_attachment_kind(str(metadata.get("name", "")))
        name = metadata.get("name", "")
        if not isinstance(name, str):
            raise ValueError("UI attachment name must be text")
        return {
            "Screen after step": "screenshot",
            "Failure screenshot": "screenshot",
            "UI page source": "page_source",
            "Device logcat": "logcat",
        }.get(name, "")

    def select(self, folder: Path, selected_test: dict) -> dict:
        """Parse and match the exact artifacts used by evidence and review."""
        cases = []
        for candidate in sorted((folder / "junit").glob("*.xml")):
            file = self._archive_file(folder, "junit", candidate.name)
            cases.extend(
                {"path": file, "case": case} for case in ET.parse(file).getroot().iter("testcase")
            )

        allure = []
        for candidate in sorted((folder / "allure").glob("*-result.json")):
            file = self._archive_file(folder, "allure", candidate.name)
            raw = file.read_bytes()
            report = json.loads(raw.decode("utf-8"))
            if not isinstance(report, dict):
                raise ValueError("Allure result must be a JSON object")
            if not isinstance(report.get("fullName"), str) or not isinstance(
                report.get("status"), str
            ):
                raise ValueError("Allure results require a test fullName and status string")
            allure.append({"path": file, "report": report, "raw": raw})

        junit_names = {
            selected_test["method"],
            selected_test["method"] + "()",
            selected_test["display"],
        }
        selected_junit = [
            record
            for record in cases
            if record["case"].get("classname") == selected_test["class"]
            and record["case"].get("name") in junit_names
        ]
        selected_allure = [
            record
            for record in allure
            if record["report"].get("fullName") == selected_test["target"]
        ]
        attachments = []
        recorded_steps = []
        mobile = getattr(self.workspace, "layer", "api") == "mobile"

        def collect(node, step_path: tuple[int, ...] = ()) -> None:
            if not isinstance(node, dict):
                raise ValueError("Allure step must be an object")
            entries = node.get("attachments", [])
            steps = node.get("steps", [])
            if (
                not isinstance(entries, list)
                or not isinstance(steps, list)
                or any(not isinstance(entry, dict) for entry in entries)
            ):
                raise ValueError("Allure steps and attachments must be lists of objects")
            if step_path:
                recorded_steps.append({"step_path": list(step_path), "status": node.get("status")})
            if mobile:
                # AppiumTestCase captures after the step body. Nested steps
                # finish before their parent captures its resulting screen.
                for ordinal, step in enumerate(steps, 1):
                    collect(step, (*step_path, ordinal))
            for entry in entries:
                source = entry.get("source", "")
                try:
                    path = (
                        self._archive_file(folder, "allure", source)
                        if isinstance(source, str) and source
                        else None
                    )
                    error = "" if path else "Missing or unsafe execution attachment"
                except ValueError:
                    path, error = None, "Missing or unsafe execution attachment"
                attachments.append(
                    {
                        "metadata": entry,
                        "path": path,
                        "error": error,
                        "kind": self._attachment_kind(entry),
                        "step_path": list(step_path),
                    }
                )
            if not mobile:
                for ordinal, step in enumerate(steps, 1):
                    collect(step, (*step_path, ordinal))

        if len(selected_allure) == 1:
            collect(selected_allure[0]["report"])
        return {
            "cases": cases,
            "allure": allure,
            "selected_junit": selected_junit,
            "selected_allure": selected_allure,
            "attachments": attachments,
            "steps": recorded_steps,
        }

    def manifest(self, artifacts: dict, target: str) -> dict:
        """Fingerprint the exact archived evidence selected for one target."""
        junit = artifacts["selected_junit"]
        allure = artifacts["selected_allure"]
        if len(junit) != 1 or len(allure) != 1:
            raise ValueError("exact evidence requires one JUnit testcase and one Allure result")

        def identity(path: Path, raw: bytes, scope: str) -> dict:
            return {
                "path": path.relative_to(self.workspace.root).as_posix(),
                "scope": scope,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        junit_raw = ET.tostring(junit[0]["case"], encoding="utf-8")
        allure_raw = allure[0]["raw"]
        mobile = getattr(self.workspace, "layer", "api") == "mobile"
        if mobile:
            reasons = mobile_step_evidence_reasons(artifacts)
            if reasons:
                raise ValueError("; ".join(reasons))
        selected = []
        for attachment in artifacts["attachments"]:
            if not attachment["kind"]:
                continue
            if attachment["error"] or attachment["path"] is None:
                raise ValueError("exact evidence has a missing or unsafe execution attachment")
            path = attachment["path"]
            raw = path.read_bytes()
            if mobile and attachment["kind"] == "screenshot":
                png_dimensions(raw)
            if mobile and attachment["kind"] == "page_source":
                parse_ui_page_source(raw)
            item = identity(path, raw, "full-attachment")
            item.update(index=len(selected), kind=attachment["kind"])
            if mobile:
                item["step_path"] = attachment["step_path"]
            selected.append(item)
        if mobile and not any(item["kind"] == "screenshot" for item in selected):
            raise ValueError("exact mobile evidence requires screenshot attachments")
        if not mobile and {item["kind"] for item in selected} != {"request", "response"}:
            raise ValueError("exact evidence requires HTTP request and response attachments")
        return {
            "version": 1,
            "target": target,
            "junit": identity(junit[0]["path"], junit_raw, "matching-testcase"),
            "allure": identity(allure[0]["path"], allure_raw, "full-result"),
            "ui" if mobile else "http": selected,
        }

    def summarize(
        self,
        folder: Path,
        selected_test: dict,
        exit_code: int,
    ) -> dict:
        target = selected_test["target"]
        artifacts = self.select(folder, selected_test)
        cases = artifacts["cases"]
        allure = artifacts["allure"]

        counts = {"total": len(cases), "passed": 0, "failed": 0, "skipped": 0}
        for record in cases:
            counts[self.junit_status(record["case"])] += 1
        reasons = []
        inventory_mismatch = len(cases) != 1 or len(allure) != 1
        if inventory_mismatch:
            reasons.append("JUnit/Allure counts do not match the selected target")
        selected_allure = artifacts["selected_allure"]
        selected_junit = artifacts["selected_junit"]
        if (
            len(selected_junit) != 1
            or len(selected_allure) != 1
            or selected_allure[0]["report"].get("status") != "passed"
        ):
            reasons.append(f"Missing, ambiguous, or unsuccessful evidence for {target}")
        attachments = artifacts["attachments"]
        mobile = getattr(self.workspace, "layer", "api") == "mobile"
        target_reasons = [attachment["error"] for attachment in attachments if attachment["error"]]
        kinds = [attachment["kind"] for attachment in attachments]
        if mobile:
            target_reasons.extend(mobile_step_evidence_reasons(artifacts))
        if not mobile and ("request" not in kinds or "response" not in kinds):
            target_reasons.append("Target has no attached HTTP request/response evidence")
        if len(selected_allure) != 1 or len(selected_junit) != 1:
            target_reasons.append("Target must have exactly one matching JUnit and Allure result")

        junit_status = (
            self.junit_status(selected_junit[0]["case"]) if len(selected_junit) == 1 else ""
        )
        allure_status = (
            selected_allure[0]["report"].get("status") if len(selected_allure) == 1 else ""
        )
        target_failed = junit_status == "failed" or allure_status in {"failed", "broken"}
        target_passed = (
            junit_status == "passed" and allure_status == "passed" and not target_reasons
        )

        reasons.extend(target_reasons)
        if exit_code or counts["skipped"]:
            reasons.append("Command failed or selected tests were skipped")
        if inventory_mismatch:
            status = "VERIFICATION_INCOMPLETE"
        elif target_failed:
            status = "FAILED"
        elif reasons:
            status = "VERIFICATION_INCOMPLETE"
        else:
            status = "VERIFIED"

        if target_failed:
            target_status = "FAILED"
        elif target_passed:
            target_status = "VERIFIED"
        else:
            target_status = "VERIFICATION_INCOMPLETE"

        result = {
            "status": status,
            "target_status": target_status,
            "counts": counts,
            "reasons": reasons,
        }
        if target_passed:
            result["evidence_manifest"] = self.manifest(artifacts, target)
        return result
