"""Repository tools and fresh API/mobile execution evidence for the QA workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from evidence import ExecutionEvidence
from kotlin_source import mask_kotlin
from process_runner import ProcessCleanupError, run_process
from review_packet import build_review_input, encode_review_packet, evidence_manifest_from_packet
from workspace import RepositoryWorkspace

TEST_RESULT_DIRECTORIES = {
    "allure": "api-tests/build/allure-results",
    "junit": "api-tests/build/test-results/test",
}
TEST_TARGET_PATTERN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}\Z")


def validate_test_target(target: str) -> str:
    """Validate an exact package.Class.method selector."""
    if not isinstance(target, str) or TEST_TARGET_PATTERN.fullmatch(target) is None:
        raise ValueError(f"target must be an exact package.Class.method name; got {target!r}")
    return target


class Repository(RepositoryWorkspace):
    """Run one guarded API or mobile test and bind its evidence to the current workspace."""

    def __init__(
        self,
        root: Path,
        output_dir: Path,
        case_id: str,
        *,
        skip_implemented: bool = False,
    ) -> None:
        super().__init__(root, output_dir, case_id)
        self.evidence_archive = ExecutionEvidence(self)
        self.last_run: dict | None = None
        self.skip_implemented = skip_implemented
        self.initial_source_digest = self.source_fingerprint() if skip_implemented else None

    def current_evidence(self) -> dict:
        if self.last_run is None:
            return {
                "status": "VERIFICATION_INCOMPLETE",
                "reason": "No test execution has completed",
            }
        if self.last_run.get("source_digest") != self.source_fingerprint():
            return {
                **deepcopy(self.last_run),
                "status": "VERIFICATION_INCOMPLETE",
                "target_status": "VERIFICATION_INCOMPLETE",
                "reason": "Repository sources changed after execution",
            }
        mobile = self.last_run.get("mobile")
        if mobile and mobile.get("apk_sha256"):
            apk = self.path_for(mobile["apk"])
            if (
                not apk.is_file()
                or hashlib.sha256(apk.read_bytes()).hexdigest() != mobile["apk_sha256"]
            ):
                return {
                    **deepcopy(self.last_run),
                    "status": "VERIFICATION_INCOMPLETE",
                    "target_status": "VERIFICATION_INCOMPLETE",
                    "reason": "Mobile APK changed after execution",
                }
        return deepcopy(self.last_run)

    @property
    def result_directories(self) -> dict[str, str]:
        return {
            area: path.replace("api-tests/", self.test_module + "/", 1)
            for area, path in TEST_RESULT_DIRECTORIES.items()
        }

    def _run(self, command: list[str], log: Path, input_text=None, timeout=900, env=None):
        # Windows resolves the executable before applying the child's cwd.
        # Resolve repository-local wrappers without changing the recorded command.
        executable = Path(command[0])
        if not executable.is_absolute() and (self.root / executable).is_file():
            command = [str(self.ensure_safe((self.root / executable).resolve())), *command[1:]]
        try:
            result = run_process(
                command,
                cwd=self.root,
                input=input_text,
                timeout=timeout,
                **({"env": env} if env is not None else {}),
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
            str(self.path_for(".agents/hooks/test_repair.py")),
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
        """Read course test metadata; unrelated helper files are not test classes."""
        inventory = []
        for file in sorted((self.root / self.test_source_root / "tests").rglob("*.kt")):
            text = self.ensure_safe(file).read_text(encoding="utf-8-sig")
            code = mask_kotlin(text)
            if not re.search(r"(?m)^\s*@(?:org\.junit\.jupiter\.api\.)?Test\b", code):
                continue
            package = re.search(r"(?m)^package\s+([\w.]+)", code)
            klass = re.search(r"\bclass\s+(\w+)", code)
            if not package or not klass:
                raise ValueError(f"Cannot identify test class: {file.name}")
            found = []
            for match in re.finditer(
                r"(?P<annotations>(?:^[ \t]*@[\w.]+(?:[ \t]*\([\s\S]*?\))?[ \t]*\r?\n)+)"
                r"[ \t]*(?:(?:public|internal|override|suspend)\s+)*fun\s+(?P<method>\w+)\s*\(",
                code,
                re.MULTILINE,
            ):
                if not re.search(r"@(?:org\.junit\.jupiter\.api\.)?Test\b", match["annotations"]):
                    continue
                start, end = match.span("annotations")
                annotations = mask_kotlin(text[start:end], keep_strings=True)
                display = re.search(r'@DisplayName\(\s*"([^"\n]+)"\s*,?\s*\)', annotations)
                allure_id = re.search(r'@AllureId\(\s*"([^"\n]+)"\s*,?\s*\)', annotations)
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
            if len(found) != len(
                re.findall(r"(?m)^\s*@(?:org\.junit\.jupiter\.api\.)?Test\b", code)
            ):
                raise ValueError(f"Unsupported test declaration in {file.name}")
            inventory.extend(found)
        return inventory

    def _selected_test(self, target: str) -> dict:
        """Return the one current test assigned to this workflow case."""
        validate_test_target(target)
        inventory = self._inventory()
        matching = [item for item in inventory if item["target"] == target]
        if len(matching) != 1:
            raise ValueError("target must identify exactly one existing test in the selected suite")

        selected = matching[0]
        expected_allure_id = self.case_id.split("-", 1)[1]
        if selected["allure_id"] != expected_allure_id:
            raise ValueError(f"target must use Allure ID {expected_allure_id!r} for {self.case_id}")
        if sum(item["allure_id"] == expected_allure_id for item in inventory) != 1:
            label = "API" if self.layer == "api" else "mobile"
            raise ValueError(
                f"Allure ID {expected_allure_id!r} must identify exactly one {label} test"
            )
        return selected

    def existing_implementation(self, target: str) -> dict:
        """Bind a source-only skip to one unchanged test with the assigned ID."""
        if not self.skip_implemented:
            raise ValueError("ALREADY_IMPLEMENTED requires --skip-implemented")
        if self.changed_files or self.last_run is not None:
            raise ValueError("An already implemented case must not be edited or executed")
        digest = self.source_fingerprint()
        if digest != self.initial_source_digest:
            raise ValueError("Repository sources changed during implementation inspection")
        return {
            **self._selected_test(target),
            "case_id": self.case_id,
            "source_digest": digest,
            "basis": "source_inspection",
        }

    def _review_run_folder(self, evidence: dict) -> Path:
        report_value = evidence.get("report", "")
        if not isinstance(report_value, str) or not report_value:
            raise ValueError("Current evidence has no archived run report")
        report = self.path_for(report_value)
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
            if not isinstance(value, str) or self.path_for(value) != folder / area:
                raise ValueError("Current evidence artifact paths do not match its run archive")
        return folder

    def prepare_review_input(self, case: str, target: str) -> tuple[dict, list[dict]]:
        """Prepare verified JSON and images together; persist only the redacted JSON."""
        if not isinstance(case, str) or not case.strip():
            raise ValueError("Review requires the complete selected case")
        validate_test_target(target)
        evidence = self.current_evidence()
        if not (
            evidence.get("status") == "VERIFIED"
            and evidence.get("target_status") == "VERIFIED"
            and evidence.get("target") == target
        ):
            raise ValueError("Review requires current VERIFIED evidence for the exact target")
        folder = self._review_run_folder(evidence)
        selected_test = self._selected_test(target)
        artifacts = self.evidence_archive.select(folder, selected_test)
        junit = artifacts["selected_junit"]
        allure = artifacts["selected_allure"]
        if len(junit) != 1 or len(allure) != 1:
            raise ValueError("Review requires exactly one matching JUnit and Allure result")
        junit_status = self.evidence_archive.junit_status(junit[0]["case"])
        if junit_status != "passed" or allure[0]["report"].get("status") != "passed":
            raise ValueError("Review requires passing exact-target JUnit and Allure evidence")
        manifest = evidence.get("evidence_manifest")
        if not isinstance(manifest, dict):
            raise ValueError("Exact execution evidence changed after its verified run")

        packet, image_blocks = build_review_input(
            self, case, target, evidence, selected_test, artifacts, junit_status
        )
        if manifest != evidence_manifest_from_packet(packet):
            raise ValueError("Exact execution evidence changed after its verified run")

        # Packet metadata and native images already share the verified bytes.
        # One final check rejects source, APK or archive changes during assembly.
        refreshed = self.evidence_archive.select(folder, selected_test)
        if manifest != self.evidence_archive.manifest(refreshed, target):
            raise ValueError("Exact execution evidence changed while assembling the review input")
        if self.current_evidence() != evidence:
            raise ValueError(
                "Repository sources, APK or run changed while assembling the review input"
            )

        encoded = encode_review_packet(packet)
        (self.output_dir / "review-packet.json").write_bytes(encoded + b"\n")
        return packet, image_blocks

    def _format_api_tests(self, wrapper: str, folder: Path, selected_path: str):
        """Capture module formatting in the review diff and restore out-of-scope edits."""

        def sources():
            return {
                file.relative_to(self.root).as_posix(): file.read_bytes()
                for file in self.files(self.test_module)
                if file.suffix in {".kt", ".kts"}
            }

        before = sources()
        permitted = self.changed_files | {selected_path}
        run = self._run(
            [wrapper, "--no-daemon", f":{self.test_module}:ktlintFormat"], folder / "format.log"
        )
        after = sources()
        changed = sorted(
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        )
        rejected = []
        for name in changed:
            try:
                self.writable_path(name)
            except ValueError:
                file = self.path_for(name)
                if name in before:
                    file.write_bytes(before[name])
                elif file.exists():
                    file.unlink()
                rejected.append(name)
                continue
            if name not in permitted:
                file = self.path_for(name)
                if name in before:
                    file.write_bytes(before[name])
                elif file.exists():
                    file.unlink()
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

    def _restore_previous_results(self, folder: Path, target: str) -> None:
        """Restore queue inputs after POST; keep displaced originals in the run archive."""
        fresh_target = False
        for result in (folder / "allure").glob("*-result.json"):
            with suppress(ValueError):
                report = json.loads(result.read_text(encoding="utf-8"))
                if isinstance(report, dict) and report.get("fullName") == target:
                    fresh_target = True

        for area, relative in self.result_directories.items():
            previous = folder / "previous" / area
            destination = self.path_for(relative)
            for file in sorted(previous.rglob("*")):
                self.ensure_safe(file)
                if not file.is_file():
                    continue
                if area == "allure" and fresh_target and file.name.endswith("-result.json"):
                    with suppress(ValueError):
                        report = json.loads(file.read_text(encoding="utf-8"))
                        if isinstance(report, dict) and report.get("fullName") == target:
                            continue
                restored = self.ensure_safe(destination / file.relative_to(previous))
                if not restored.exists():
                    restored.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, restored)

    def run_api_test(self, target: str, *, format_sources: bool = True) -> dict:
        if self.layer != "api":
            raise ValueError("run_api_test requires an API case")
        return self._run_test(target, format_sources=format_sources)

    def run_mobile_test(
        self, target: str, *, format_sources: bool = True, repair: bool = False
    ) -> dict:
        """Use the OS runner for generation and its exact Gradle command for repair."""
        if self.layer != "mobile":
            raise ValueError("run_mobile_test requires a mobile case")
        return self._run_test(target, format_sources=format_sources, repair=repair)

    def _mobile_context(self, folder: Path, repair: bool) -> dict:
        sdk = os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT")
        if not sdk and os.name == "nt":
            sdk = str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
        if not sdk:
            raise ValueError("Set ANDROID_HOME to the SDK used by the mobile runner")
        adb = Path(sdk) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        result = self._run([str(adb), "devices"], folder / "devices.log", timeout=20)
        devices = re.findall(r"(?m)^([^\s]+)\s+device\s*$", result.stdout)
        if result.returncode or len(devices) != 1:
            raise ValueError("Mobile workflow requires exactly one connected device")
        context = {
            "device": devices[0],
            "flavor": "stable",
            "apk": "app/build/outputs/apk/stable/debug/app-stable-debug.apk",
        }
        if repair:
            previous = (self.last_run or {}).get("mobile", {})
            if any(previous.get(key) != value for key, value in context.items()):
                raise ValueError(
                    "Mobile repair requires the same device, flavor and APK as generation"
                )
            apk = self.path_for(context["apk"])
            if not apk.is_file() or hashlib.sha256(apk.read_bytes()).hexdigest() != previous.get(
                "apk_sha256"
            ):
                raise ValueError(
                    "Mobile repair requires the APK from the original runner execution"
                )
        return context

    def _run_test(self, target: str, *, format_sources: bool = True, repair: bool = False) -> dict:
        selected_test = self._selected_test(target)
        folder = self.output_dir / ("run-" + uuid.uuid4().hex)
        folder.mkdir()
        (folder / "suite.log").write_text("Test command has not run.\n", encoding="utf-8")
        wrapper = ".\\gradlew.bat" if os.name == "nt" else "./gradlew"
        command = [
            wrapper,
            "--no-daemon",
            f":{self.test_module}:test",
            "--rerun",
            "--tests",
            target,
        ]
        if self.layer == "api":
            command.insert(4, "-Dapi.url=http://127.0.0.1:8080")
        command_text = " ".join(command)

        def relative(name: str) -> str:
            return (folder / name).relative_to(self.root).as_posix()

        summary = {
            "status": "VERIFICATION_INCOMPLETE",
            "target_status": "VERIFICATION_INCOMPLETE",
            "target": target,
            "case_id": self.case_id,
            "command": command_text,
            "log": relative("suite.log"),
            "junit_dir": relative("junit"),
            "allure_dir": relative("allure"),
            "reasons": [],
        }

        def invalidate(reason: str) -> None:
            summary["status"] = "VERIFICATION_INCOMPLETE"
            summary["target_status"] = "VERIFICATION_INCOMPLETE"
            summary["reasons"].append(reason)

        approved = False
        execution_stopped = True
        executed_source_digest = None
        run = subprocess.CompletedProcess(command, 127, "Execution did not start")
        try:
            mobile = None
            runner_env = None
            if self.layer == "mobile":
                mobile = self._mobile_context(folder, repair)
                apk = str(self.path_for(mobile["apk"]))
                command[4:4] = [
                    f"-Dapp.apk={apk}",
                    f"-Dui.variant={mobile['flavor']}",
                    f"-Dappium.devices={mobile['device']}",
                ]
                command_text = (
                    subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
                )
                summary["mobile"] = mobile
                if not repair:
                    # The scripts use these flags through the Gradle JVM. A
                    # daemon outside the owned process tree must not survive a timeout.
                    runner_env = {
                        "GRADLE_OPTS": os.getenv("GRADLE_OPTS", "") + " -Dorg.gradle.daemon=false",
                        "FLAVOR": mobile["flavor"],
                        "DEVICE": mobile["device"],
                    }
                    guarded = [part for part in command if part != "--no-daemon"]
                    command_text = (
                        subprocess.list2cmdline(guarded) if os.name == "nt" else shlex.join(guarded)
                    )
                    if os.name == "nt":
                        command = [
                            "powershell.exe",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(self.path_for("scripts/run-suite.ps1")),
                            "-Flavor",
                            mobile["flavor"],
                            "-Device",
                            mobile["device"],
                            "-TestFilter",
                            target,
                        ]
                    else:
                        command = [
                            "bash",
                            str(self.path_for("scripts/run-suite.sh")),
                            "--tests",
                            target,
                        ]
                summary["command"] = (
                    subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
                )
                summary["execution_mode"] = (
                    "exact_mobile_repair" if repair else "mobile_suite_runner"
                )
                summary["guarded_command"] = command_text
                summary["gradle_daemon"] = False
            # Formatting is a non-test command. Complete it before PRE starts the
            # guarded exact-target run, so a formatter failure consumes no run budget.
            if format_sources:
                run = self._format_api_tests(wrapper, folder, selected_test["path"])
                summary["format_log"] = relative("format.log")
                summary["format_exit_code"] = run.returncode
                if run.returncode:
                    raise RuntimeError(
                        "Test formatting did not complete within the permitted test layers; "
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
                previous_root = folder / "previous"
                previous_root.mkdir()
                summary["previous_results"] = relative("previous")
                for name, path in self.result_directories.items():
                    source = self.path_for(path)
                    if source.exists():
                        shutil.move(source, previous_root / name)
                executed_source_digest = self.source_fingerprint()
                run = self._run(
                    command, folder / "suite.log", **({"env": runner_env} if runner_env else {})
                )
                if mobile is not None:
                    apk = self.path_for(mobile["apk"])
                    if apk.is_file():
                        mobile["apk_sha256"] = hashlib.sha256(apk.read_bytes()).hexdigest()
                for name, path in self.result_directories.items():
                    source = self.path_for(path)
                    destination = folder / name
                    destination.mkdir()
                    for file in source.rglob("*") if source.exists() else ():
                        self.ensure_safe(file)
                        if file.is_file():
                            copy = destination / file.relative_to(source)
                            copy.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(file, copy)
                summary.update(
                    self.evidence_archive.summarize(folder, selected_test, run.returncode)
                )
                if mobile is not None and not repair and os.name == "nt":
                    marker = re.search(
                        r"Appium result: (\d+) passed, (\d+) failed, (\d+) errors, (\d+) skipped "
                        r"\((\d+) total\) on ([^\s]+)",
                        run.stdout,
                    )
                    if marker is None or int(marker[5]) != 1 or marker[6] != mobile["device"]:
                        invalidate("Missing or mismatched fresh OS mobile runner result")
                    else:
                        summary["runner_result"] = marker[0]
        except ProcessCleanupError as error:
            execution_stopped = False
            summary["execution_stopped"] = False
            invalidate(f"{error}; POST and result restoration were skipped")
        except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
            invalidate(str(error))
        finally:
            if approved and execution_stopped:
                try:
                    summary["post_hook"] = self._hook(
                        "after", command_text, folder, run.stdout, run.returncode
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    invalidate(str(error))
                finally:
                    try:
                        self._restore_previous_results(folder, target)
                        if self.layer == "mobile" and not repair:
                            # The runner refreshes while older results are archived.
                            # Restore its complete input before refreshing again.
                            refreshed = self._run(
                                self._hook_command("refresh"),
                                folder / "queue-refresh.log",
                                timeout=30,
                            )
                            if refreshed.returncode:
                                invalidate("Repair queue refresh failed after result restoration")
                    except (OSError, ValueError) as error:
                        invalidate(f"Could not restore previous results: {error}")
        current_source_digest = self.source_fingerprint()
        if executed_source_digest is not None and executed_source_digest != current_source_digest:
            invalidate("Sources changed during or after execution")
        summary["exit_code"] = run.returncode
        summary["source_digest"] = executed_source_digest or current_source_digest
        summary["report"] = relative("result.json")
        (folder / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        self.last_run = deepcopy(summary)
        return deepcopy(summary)
