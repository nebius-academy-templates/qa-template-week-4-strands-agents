"""Synthetic adapter checks; no Gradle, backend, or model execution."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repository import Repository
from review_packet import MAX_HTTP_ATTACHMENT_BYTES, MAX_PACKET_BYTES

TARGET = "tests.SampleApiTest.testScenario"
TEST_PATH = "api-tests/src/test/kotlin/tests/SampleApiTest.kt"
SOURCE = """package tests
class SampleApiTest {
    @Test
    @DisplayName("Scenario preserves its expected result")
    @AllureId("9001")
    fun testScenario() {}
}
"""


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, content in {
            "scripts/protected-paths.txt": "app/\nfake-api/\nfixtures/\n",
            ".agents/hooks/test_repair.py": "# synthetic hook fixture\n",
            "AGENTS.md": "Repository policy\n",
            TEST_PATH: SOURCE,
            "fake-api/openapi.yaml": "openapi: 3.0.3\n",
        }.items():
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8")
        self.repo = Repository(self.root, self.root / ".agent-state/qa-workflow/test", "API-9001")
        self.calls = []

    def process(self, command, **kwargs):
        self.calls.append(command)
        if ":api-tests:test" in command:
            self.evidence()
            return subprocess.CompletedProcess(command, 0, "Synthetic process result")
        return subprocess.CompletedProcess(command, 0, "{}")

    @unittest.skipUnless(os.name == "nt", "Windows resolves executables before applying cwd")
    def test_windows_wrapper_runs_from_repository_outside_process_directory(self):
        (self.root / "gradlew.bat").write_text(
            "@echo off\necho wrapper-started\n", encoding="utf-8"
        )
        result = self.repo._run([".\\gradlew.bat"], self.repo.output_dir / "wrapper.log")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("wrapper-started", result.stdout)

    def evidence(self, status="passed", other_failure=False, skip=False, target=TARGET):
        junit = self.root / "api-tests/build/test-results/test"
        allure = self.root / "api-tests/build/allure-results"
        junit.mkdir(parents=True, exist_ok=True)
        allure.mkdir(parents=True, exist_ok=True)
        marker = (
            '<failure message="synthetic failure"/>'
            if status == "failed"
            else "<skipped/>"
            if skip
            else ""
        )
        extra = (
            '<testcase classname="tests.OtherApiTest" name="testOther"><failure/></testcase>'
            if other_failure
            else ""
        )
        (junit / "TEST-sample.xml").write_text(
            '<testsuite><testcase classname="tests.SampleApiTest" name="testScenario">'
            f"{marker}</testcase>{extra}</testsuite>",
            encoding="utf-8",
        )
        report = {
            "fullName": target,
            "status": "skipped" if skip else status,
            "steps": [
                {
                    "name": "Request",
                    "attachments": [
                        {"name": "Request", "source": "request.txt"},
                        {"name": "Response", "source": "response.txt"},
                    ],
                }
            ],
        }
        (allure / "sample-result.json").write_text(json.dumps(report), encoding="utf-8")
        for name in ("request.txt", "response.txt"):
            (allure / name).write_text("Synthetic HTTP fixture", encoding="utf-8")
        if other_failure:
            (allure / "other-result.json").write_text(
                json.dumps({"fullName": "tests.OtherApiTest.testOther", "status": "failed"}),
                encoding="utf-8",
            )

    def test_source_access_and_write_boundaries(self):
        self.assertIn("openapi", self.repo.read_file("fake-api/openapi.yaml")["content"])
        for name in (
            "../outside.md",
            "C:/outside.md",
            "NUL.txt",
            ".env",
            "answers/answer.md",
            ".git/config",
            "local.properties",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.repo.read_file(name)
        for name in (
            "fake-api/openapi.yaml",
            "api-tests/src/test/kotlin/rule/ApiTestCase.kt",
            "scripts/protected-paths.txt",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.repo.write_file(name, "replacement")
        (self.root / "scripts/protected-paths.txt").write_text(TEST_PATH + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.repo.write_file(TEST_PATH, SOURCE)

    def test_markdown_plan_template_is_readable_searchable_and_not_writable(self):
        path = "agent_docs/templates/automation_plan.api.workflow.md.template"
        template = self.root / path
        template.parent.mkdir(parents=True)
        template.write_text("# Automation plan\n## Test contract\n", encoding="utf-8")
        self.assertIn("## Test contract", self.repo.read_file(path)["content"])
        self.assertIn(path, self.repo.list_files("agent_docs")["files"])
        self.assertEqual(
            self.repo.search_text("Test contract", "agent_docs")["matches"][0]["path"], path
        )
        with self.assertRaises(ValueError):
            self.repo.write_file(path, "replacement")

    def test_existing_unrelated_plan_is_preserved_and_edit_is_exact(self):
        plan = self.root / "agent_docs/automation-plans/API-9001.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("API-7777 existing work", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.repo.write_file("agent_docs/automation-plans/API-9001.md", "API-9001 replacement")
        self.assertEqual(plan.read_text(), "API-7777 existing work")
        with self.assertRaises(ValueError):
            self.repo.edit_file(TEST_PATH, "missing", "new")
        self.repo.edit_file(TEST_PATH, "testScenario", "testChangedScenario")
        self.assertIn("+    fun testChangedScenario()", self.repo.diff())

    def test_plan_write_is_scoped_to_selected_case(self):
        self.repo.write_file("agent_docs/automation-plans/API-9001.md", "API-9001 validated plan")
        for path in (
            "automation_plan.md",
            "agent_docs/automation-plans/API-9002.md",
            "agent_docs/AI_POLICY.md",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.repo.write_file(path, "API-9001 replacement")

    def test_sequential_cases_keep_separate_plans(self):
        first_path = "agent_docs/automation-plans/API-9001.md"
        second_path = "agent_docs/automation-plans/API-9002.md"
        self.repo.write_file(first_path, "API-9001 validated plan")
        second = Repository(
            self.root,
            self.root / ".agent-state/qa-workflow/test/cases/002-API-9002",
            "API-9002",
        )
        second.write_file(second_path, "API-9002 validated plan")

        self.assertEqual((self.root / first_path).read_text(), "API-9001 validated plan\n")
        self.assertEqual((self.root / second_path).read_text(), "API-9002 validated plan\n")

    def test_pre_denial_prevents_gradle_and_leaves_old_reports(self):
        self.evidence()
        denial = {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "another repair is active",
            }
        }
        with patch(
            "repository.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(denial)),
        ) as process:
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(process.call_count, 2)
        self.assertFalse(any(":api-tests:test" in call.args[0] for call in process.call_args_list))
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertTrue((self.root / "api-tests/build/test-results/test/TEST-sample.xml").exists())

    def test_verified_run_has_fresh_artifacts_and_invalidates_after_edit(self):
        self.evidence(status="failed")
        with patch("repository.subprocess.run", side_effect=self.process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual((result["status"], result["target_status"]), ("VERIFIED", "VERIFIED"))
        self.assertEqual(len(self.calls), 4)
        self.assertIn(":api-tests:ktlintFormat", self.calls[0])
        self.assertIn("before", self.calls[1])
        self.assertIn("--rerun", self.calls[2])
        self.assertEqual(self.calls[2][-2:], ["--tests", TARGET])
        self.assertEqual(self.repo.current_evidence()["status"], "VERIFIED")
        result["counts"]["passed"] = 99
        self.assertEqual(self.repo.current_evidence()["counts"]["passed"], 1)
        self.repo.edit_file(
            TEST_PATH, "fun testScenario() {}", "fun testScenario() { check(true) }"
        )
        self.assertEqual(self.repo.current_evidence()["status"], "NOT_VERIFIED")

    def test_junit_status_uses_namespaced_outcomes_with_failure_priority(self):
        passed = ET.fromstring("<testcase />")
        skipped = ET.fromstring('<testcase xmlns:j="urn:junit"><j:skipped /></testcase>')
        failed = ET.fromstring('<testcase xmlns:j="urn:junit"><j:skipped /><j:error /></testcase>')

        self.assertEqual(self.repo._junit_status(passed), "passed")
        self.assertEqual(self.repo._junit_status(skipped), "skipped")
        self.assertEqual(self.repo._junit_status(failed), "failed")

    def test_post_hook_source_change_invalidates_the_executed_digest(self):
        executed_digest = self.repo.source_fingerprint()

        def process(command, **kwargs):
            result = self.process(command, **kwargs)
            if "after" in command:
                (self.root / TEST_PATH).write_text(
                    SOURCE + "// changed by post hook\n", encoding="utf-8"
                )
            return result

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)

        self.assertEqual(
            (result["status"], result["target_status"]),
            ("NOT_VERIFIED", "NOT_VERIFIED"),
        )
        self.assertEqual(result["source_digest"], executed_digest)
        self.assertNotEqual(result["source_digest"], self.repo.source_fingerprint())
        self.assertIn("Sources changed during or after execution", result["reasons"])

    def test_coverage_run_skips_formatter_and_keeps_source_fingerprint(self):
        before = self.repo.source_fingerprint()

        with patch("repository.subprocess.run", side_effect=self.process):
            result = self.repo.run_api_test(TARGET, format_sources=False)

        self.assertEqual((result["status"], result["target_status"]), ("VERIFIED", "VERIFIED"))
        self.assertFalse(any(":api-tests:ktlintFormat" in command for command in self.calls))
        self.assertEqual(len(self.calls), 3)
        self.assertIn("before", self.calls[0])
        self.assertIn(":api-tests:test", self.calls[1])
        self.assertIn("after", self.calls[2])
        self.assertNotIn("format_log", result)
        self.assertEqual(self.repo.source_fingerprint(), before)

    def test_zero_tests_cannot_reuse_previous_reports(self):
        self.evidence()

        def no_reports(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 0, "{}" if ":api-tests:test" not in command else "No tests"
            )

        with patch("repository.subprocess.run", side_effect=no_reports):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertEqual(result["counts"]["total"], 0)

    def test_rest_assured_response_name_and_missing_response(self):
        for response_name, expected in [
            ("HTTP/1.1 200 OK", "VERIFIED"),
            ("Unrelated attachment", "NOT_VERIFIED"),
        ]:
            with self.subTest(response_name=response_name):

                def process(command, response_name=response_name, **kwargs):
                    result = self.process(command, **kwargs)
                    if ":api-tests:test" in command:
                        path = self.root / "api-tests/build/allure-results/sample-result.json"
                        report = json.loads(path.read_text())
                        report["steps"][0]["attachments"][1]["name"] = response_name
                        path.write_text(json.dumps(report), encoding="utf-8")
                    return result

                with patch("repository.subprocess.run", side_effect=process):
                    result = self.repo.run_api_test(TARGET)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["target_status"], expected)

    def test_wrong_case_id_cannot_select_a_green_test(self):
        self.repo.case_id = "API-9002"
        with patch("repository.subprocess.run") as process, self.assertRaises(ValueError):
            self.repo.run_api_test(TARGET)
        process.assert_not_called()

    def test_target_failure_skip_and_mismatch_remain_distinct(self):
        for status, skip, target, expected in [
            ("failed", False, TARGET, "FAILED"),
            ("passed", True, TARGET, "NOT_VERIFIED"),
            ("passed", False, "tests.Wrong.testOther", "NOT_VERIFIED"),
        ]:
            with self.subTest(expected=expected, target=target):

                def process(command, status=status, skip=skip, target=target, **kwargs):
                    if ":api-tests:test" in command:
                        self.evidence(status=status, skip=skip, target=target)
                    return subprocess.CompletedProcess(command, 0, "{}")

                with patch("repository.subprocess.run", side_effect=process):
                    result = self.repo.run_api_test(TARGET)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["target_status"], expected)

    def test_extra_test_results_do_not_verify_the_exact_run(self):
        other = self.root / "api-tests/src/test/kotlin/tests/OtherApiTest.kt"
        other.write_text(
            SOURCE.replace("SampleApiTest", "OtherApiTest")
            .replace("testScenario", "testOther")
            .replace('"9001"', '"9002"'),
            encoding="utf-8",
        )

        def process(command, **kwargs):
            if ":api-tests:test" in command:
                self.evidence(other_failure=True)
            return subprocess.CompletedProcess(
                command, 1 if ":api-tests:test" in command else 0, "{}"
            )

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual((result["status"], result["target_status"]), ("NOT_VERIFIED", "VERIFIED"))
        self.assertTrue(
            any("counts do not match the selected target" in reason for reason in result["reasons"])
        )

    def test_target_failure_with_extra_results_is_not_verified(self):
        other = self.root / "api-tests/src/test/kotlin/tests/OtherApiTest.kt"
        other.write_text(
            SOURCE.replace("SampleApiTest", "OtherApiTest")
            .replace("testScenario", "testOther")
            .replace('"9001"', '"9002"'),
            encoding="utf-8",
        )

        def process(command, **kwargs):
            if ":api-tests:test" in command:
                self.evidence(status="failed", other_failure=True)
            return subprocess.CompletedProcess(
                command, 1 if ":api-tests:test" in command else 0, "{}"
            )

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual((result["status"], result["target_status"]), ("NOT_VERIFIED", "FAILED"))

    def test_process_failure_preserves_logs_and_posts_to_hook(self):
        def process(command, **kwargs):
            self.calls.append(command)
            if ":api-tests:test" in command:
                raise OSError("synthetic process startup failure")
            return subprocess.CompletedProcess(command, 0, "{}")

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertEqual(len(self.calls), 4)
        self.assertIn("after", self.calls[-1])
        self.assertIn("synthetic process startup failure", (self.root / result["log"]).read_text())

    def test_repair_completion_uses_canonical_cli_and_preserves_rejection(self):
        with patch(
            "repository.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "Fresh verified JUnit required"),
        ) as process:
            result = self.repo.repair_action("complete", item_id="queue-item", outcome="fixed")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("complete", process.call_args.args[0])
        self.assertEqual(
            process.call_args.args[0][-4:], ["--id", "queue-item", "--outcome", "fixed"]
        )

    def test_formatter_changes_are_included_in_review_diff(self):
        def process(command, **kwargs):
            if ":api-tests:ktlintFormat" in command:
                file = self.root / TEST_PATH
                file.write_text(
                    SOURCE.replace("testScenario() {}", "testScenario() { }"), encoding="utf-8"
                )
            return self.process(command, **kwargs)

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertIn(TEST_PATH, self.repo.changed_files)
        self.assertIn("+    fun testScenario() { }", self.repo.diff())

    def test_formatter_out_of_scope_changes_are_restored_without_test_run(self):
        rule = self.root / "api-tests/src/test/kotlin/rule/ApiTestCase.kt"
        rule.parent.mkdir()
        rule.write_text("original\n", encoding="utf-8")

        def process(command, **kwargs):
            if ":api-tests:ktlintFormat" in command:
                rule.write_text("formatter changed rule\n", encoding="utf-8")
            return self.process(command, **kwargs)

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertEqual(rule.read_text(), "original\n")
        self.assertFalse(any(":api-tests:test" in command for command in self.calls))
        self.assertEqual(len(self.calls), 1)

    def test_malformed_completed_allure_is_not_verified_and_still_posts(self):
        def process(command, **kwargs):
            result = self.process(command, **kwargs)
            if ":api-tests:test" in command:
                (self.root / "api-tests/build/allure-results/sample-result.json").write_text(
                    "[]", encoding="utf-8"
                )
            return result

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(
            (result["status"], result["target_status"]), ("NOT_VERIFIED", "NOT_VERIFIED")
        )
        self.assertIn("Allure result must be a JSON object", result["reasons"])
        self.assertIn("after", self.calls[-1])

    def test_review_packet_contains_only_exact_bounded_redacted_inputs(self):
        (self.root / TEST_PATH).write_text(
            SOURCE.replace(
                "package tests\n",
                "package tests\n\nimport client.DirectHelper\nimport rule.*\n"
                "import tests.support.SupportHelper\n",
            ).replace(
                "fun testScenario() {}",
                "fun testScenario() { DirectHelper(); RuleHelper(); SupportHelper() }",
            ),
            encoding="utf-8",
        )
        helpers = {
            "client/DirectHelper.kt": """package client
import model.Payload
class DirectHelper {
    val payload = Payload()
    val samePackage = SamePackageHelper()
    val values = testdata.ApiValues
}
""",
            "client/SamePackageHelper.kt": "package client\nclass SamePackageHelper\n",
            "client/UnrelatedHelper.kt": "package client\nclass UnrelatedHelper\n",
            "model/Payload.kt": "package model\nclass Payload\n",
            "model/UnrelatedModel.kt": "package model\nclass UnrelatedModel\n",
            "rule/RuleHelper.kt": "package rule\nclass RuleHelper\n",
            "testdata/ApiValues.kt": "package testdata\nobject ApiValues\n",
            "tests/support/SupportHelper.kt": "package tests.support\nclass SupportHelper\n",
            "tests/support/UnrelatedSupport.kt": "package tests.support\nclass UnrelatedSupport\n",
        }
        for relative, content in helpers.items():
            helper = self.root / "api-tests/src/test/kotlin" / relative
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text(content, encoding="utf-8")
        plan = self.root / "agent_docs/automation-plans/API-9001.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# API-9001 plan\n", encoding="utf-8")

        def process(command, **kwargs):
            result = self.process(command, **kwargs)
            if ":api-tests:test" in command:
                junit_path = self.root / "api-tests/build/test-results/test/TEST-sample.xml"
                junit_tree = ET.parse(junit_path)
                junit_case = next(junit_tree.getroot().iter("testcase"))
                junit_case.set("time", "0.125")
                junit_case.set("debug", "junit-attribute-secret")
                ET.SubElement(junit_case, "system-out").text = "junit-system-out-secret"
                junit_tree.write(junit_path, encoding="utf-8")
                allure = self.root / "api-tests/build/allure-results"
                report_path = allure / "sample-result.json"
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["name"] = "token=allure-report-name-secret"
                report["parameters"] = [{"name": "token", "value": "allure-name-value-secret"}]
                report["statusDetails"] = {"message": "allure-status-secret"}
                report["labels"] = [
                    {"name": "AS_ID", "value": "9001"},
                    {"name": "feature", "value": "allure-feature-secret"},
                    {"name": "host", "value": "allure-host-secret"},
                    {"name": "thread", "value": "allure-thread-secret"},
                ]
                report["steps"][0]["name"] = "token=allure-step-name-secret"
                report["steps"][0]["attachments"] = [
                    {
                        "name": "Request",
                        "source": "request.html",
                        "type": "text/html; charset=utf-8",
                    },
                    {
                        "name": "HTTP/1.1 200 OK",
                        "source": "response.txt",
                        "type": "text/plain",
                    },
                    {
                        "name": "Unrelated binary",
                        "source": "debug.bin",
                        "type": "application/octet-stream",
                    },
                ]
                report_path.write_text(json.dumps(report), encoding="utf-8")
                (allure / "request.html").write_text(
                    "<html><body><pre>Authorization: Bearer secret-auth\n"
                    "Cookie: session=secret-cookie\nX-API-Key: secret-key\n"
                    "X-Sandbox-Session: secret-session\n"
                    "curl -H 'Authorization: Bearer curl-secret' http://localhost\n"
                    "http://localhost/orders?access_token=query-secret&api_key=form-secret\n"
                    "refresh_token=refresh-secret token=plain-token-secret\n"
                    '{"accessToken":"secret-token"}</pre></body></html>',
                    encoding="utf-8",
                )
                (allure / "response.txt").write_text(
                    'Set-Cookie: secret-response\n{"token":"secret-json-token"}',
                    encoding="utf-8",
                )
                (allure / "debug.bin").write_bytes(b"unrelated-secret")
            return result

        with patch("repository.subprocess.run", side_effect=process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(result["status"], "VERIFIED")
        junit_archive = self.root / result["junit_dir"]
        (junit_archive / "TEST-unrelated.xml").write_text(
            '<testsuite><testcase classname="tests.OtherTest" '
            'name="unrelated-target-secret"/></testsuite>',
            encoding="utf-8",
        )
        allure_archive = self.root / result["allure_dir"]
        (allure_archive / "unrelated-result.json").write_text(
            json.dumps(
                {
                    "fullName": "tests.OtherTest.unrelated-target-secret",
                    "status": "passed",
                }
            ),
            encoding="utf-8",
        )

        case = "API-9001 complete case with expected response behavior."
        packet = self.repo.prepare_review_packet(case, TARGET)

        self.assertEqual(packet["schema"], "ai-for-qa/exact-api-review-packet")
        self.assertEqual(packet["version"], 1)
        self.assertEqual(packet["case"], {"case_id": "API-9001", "text": case})
        self.assertEqual(packet["target"]["name"], TARGET)
        self.assertEqual(packet["target"]["source_digest"], result["source_digest"])
        self.assertEqual(packet["evidence"]["summary"]["status"], "VERIFIED")
        self.assertEqual(packet["evidence"]["summary"]["target_status"], "VERIFIED")
        self.assertEqual(packet["evidence"]["summary"]["target"], TARGET)
        self.assertEqual(packet["evidence"]["summary"]["counts"]["total"], 1)
        self.assertIn(f"--tests {TARGET}", packet["evidence"]["summary"]["command"])
        self.assertIn("1: package tests", packet["sources"]["test"]["content"])
        self.assertEqual(
            [helper["path"] for helper in packet["sources"]["helpers"]],
            [
                "api-tests/src/test/kotlin/client/DirectHelper.kt",
                "api-tests/src/test/kotlin/client/SamePackageHelper.kt",
                "api-tests/src/test/kotlin/model/Payload.kt",
                "api-tests/src/test/kotlin/rule/RuleHelper.kt",
                "api-tests/src/test/kotlin/testdata/ApiValues.kt",
                "api-tests/src/test/kotlin/tests/support/SupportHelper.kt",
            ],
        )
        self.assertNotIn("Unrelated", json.dumps(packet["sources"]["helpers"]))
        self.assertIn("1: # API-9001 plan", packet["sources"]["plan"]["content"])
        self.assertIn("testScenario", packet["evidence"]["junit"]["content"])
        self.assertNotIn("testsuite", packet["evidence"]["junit"]["content"])
        self.assertEqual(packet["evidence"]["junit"]["scope"], "matching-testcase")
        self.assertEqual(packet["evidence"]["junit"]["content_scope"], "whitelisted-summary")
        self.assertEqual(packet["evidence"]["junit"]["content_mime_type"], "application/json")
        junit_summary = json.loads(packet["evidence"]["junit"]["content"])
        self.assertEqual(junit_summary, {"status": "passed", "target": TARGET})
        archived_junit = next(
            ET.parse(junit_archive / "TEST-sample.xml").getroot().iter("testcase")
        )
        archived_junit_raw = ET.tostring(archived_junit, encoding="utf-8")
        self.assertEqual(packet["evidence"]["junit"]["size_bytes"], len(archived_junit_raw))
        self.assertEqual(
            packet["evidence"]["junit"]["sha256"],
            hashlib.sha256(archived_junit_raw).hexdigest(),
        )
        self.assertIn(TARGET, packet["evidence"]["allure"]["content"])
        self.assertEqual(packet["evidence"]["allure"]["scope"], "full-result")
        self.assertEqual(packet["evidence"]["allure"]["content_scope"], "whitelisted-summary")
        allure_summary = json.loads(packet["evidence"]["allure"]["content"])
        self.assertEqual(allure_summary["allure_ids"], ["9001"])
        self.assertEqual(allure_summary["steps"][0]["ordinal"], 1)
        self.assertEqual(
            [item["name"] for item in allure_summary["steps"][0]["http_attachments"]],
            ["HTTP request", "HTTP response 200"],
        )
        archived_allure = (self.root / result["allure_dir"] / "sample-result.json").read_bytes()
        self.assertEqual(
            packet["evidence"]["allure"]["sha256"],
            hashlib.sha256(archived_allure).hexdigest(),
        )
        self.assertEqual(
            [attachment["kind"] for attachment in packet["evidence"]["http"]],
            ["request", "response"],
        )
        serialized = json.dumps(packet)
        for secret in (
            "secret-auth",
            "secret-cookie",
            "secret-key",
            "secret-session",
            "curl-secret",
            "secret-token",
            "secret-response",
            "secret-json-token",
            "query-secret",
            "form-secret",
            "refresh-secret",
            "plain-token-secret",
            "allure-report-name-secret",
            "allure-name-value-secret",
            "allure-status-secret",
            "allure-feature-secret",
            "allure-host-secret",
            "allure-thread-secret",
            "allure-step-name-secret",
            "junit-attribute-secret",
            "junit-system-out-secret",
            "unrelated-secret",
            "unrelated-target-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn("<html>", serialized)
        for artifact in [
            packet["sources"]["test"],
            *packet["sources"]["helpers"],
            packet["sources"]["plan"],
            packet["evidence"]["junit"],
            packet["evidence"]["allure"],
            *packet["evidence"]["http"],
        ]:
            self.assertTrue(artifact["path"])
            self.assertTrue(artifact["mime_type"])
            self.assertGreater(artifact["size_bytes"], 0)
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
        saved = json.loads((self.repo.output_dir / "review-packet.json").read_text())
        self.assertEqual(saved, packet)
        self.assertLess(
            len(json.dumps(packet, ensure_ascii=False).encode("utf-8")), MAX_PACKET_BYTES
        )

    def test_review_packet_rejects_post_run_exact_evidence_mutation(self):
        for artifact in ("junit", "allure", "http"):
            with self.subTest(artifact=artifact):
                self.setUp()
                with patch("repository.subprocess.run", side_effect=self.process):
                    result = self.repo.run_api_test(TARGET)
                if artifact == "junit":
                    path = self.root / result["junit_dir"] / "TEST-sample.xml"
                    path.write_text(
                        path.read_text(encoding="utf-8").replace(
                            'name="testScenario"', 'name="testScenario" time="9"'
                        ),
                        encoding="utf-8",
                    )
                elif artifact == "allure":
                    path = self.root / result["allure_dir"] / "sample-result.json"
                    report = json.loads(path.read_text(encoding="utf-8"))
                    report["mutated"] = True
                    path.write_text(json.dumps(report), encoding="utf-8")
                else:
                    path = self.root / result["allure_dir"] / "response.txt"
                    path.write_text("mutated body", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed after its verified run"):
                    self.repo.prepare_review_packet("API-9001 case", TARGET)

    def test_review_packet_rejects_ambiguous_local_helper_symbol(self):
        (self.root / TEST_PATH).write_text(
            SOURCE.replace("package tests\n", "package tests\n\nimport client.Duplicate\n").replace(
                "fun testScenario() {}", "fun testScenario() { Duplicate() }"
            ),
            encoding="utf-8",
        )
        for name in ("First.kt", "Second.kt"):
            helper = self.root / "api-tests/src/test/kotlin/client" / name
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("package client\nclass Duplicate\n", encoding="utf-8")
        with patch("repository.subprocess.run", side_effect=self.process):
            result = self.repo.run_api_test(TARGET)
        self.assertEqual(result["status"], "VERIFIED")
        with self.assertRaisesRegex(ValueError, "Ambiguous local Kotlin symbol"):
            self.repo.prepare_review_packet("API-9001 case", TARGET)

    def test_review_packet_rejects_stale_missing_unsafe_and_non_text_evidence(self):
        mutations = {
            "stale source": lambda result: (self.root / TEST_PATH).write_text(
                SOURCE + "// changed\n", encoding="utf-8"
            ),
            "missing attachment": lambda result: (
                self.root / result["allure_dir"] / "response.txt"
            ).unlink(),
            "unsafe attachment": lambda result: self._rewrite_archived_attachment(
                result, "response.txt", "../response.txt"
            ),
            "non-text attachment": lambda result: self._rewrite_archived_attachment(
                result, "response.txt", "response.txt", "application/octet-stream"
            ),
            "outside current run": lambda result: self.repo.last_run.__setitem__(
                "allure_dir", "api-tests/build/allure-results"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                with patch("repository.subprocess.run", side_effect=self.process):
                    result = self.repo.run_api_test(TARGET)
                mutate(result)
                with self.assertRaises(ValueError):
                    self.repo.prepare_review_packet("API-9001 case", TARGET)

    def _rewrite_archived_attachment(self, result, old_source, new_source, mime_type=None):
        result_path = self.root / result["allure_dir"] / "sample-result.json"
        report = json.loads(result_path.read_text(encoding="utf-8"))
        attachment = next(
            item for item in report["steps"][0]["attachments"] if item["source"] == old_source
        )
        attachment["source"] = new_source
        if mime_type:
            attachment["type"] = mime_type
        result_path.write_text(json.dumps(report), encoding="utf-8")

    def test_review_packet_rejects_attachment_and_total_size_overflow(self):
        def large_attachment(command, **kwargs):
            result = self.process(command, **kwargs)
            if ":api-tests:test" in command:
                attachment = self.root / "api-tests/build/allure-results/request.txt"
                attachment.write_text("x" * (MAX_HTTP_ATTACHMENT_BYTES + 1), encoding="utf-8")
            return result

        with patch("repository.subprocess.run", side_effect=large_attachment):
            self.repo.run_api_test(TARGET)
        with self.assertRaisesRegex(ValueError, "HTTP attachment exceeds"):
            self.repo.prepare_review_packet("API-9001 case", TARGET)

        self.setUp()
        imports = []
        calls = []
        for index, layer in enumerate(("client", "model", "rule"), 1):
            imports.append(f"import {layer}.Large{index}")
            calls.append(f"Large{index}()")
            helper = self.root / f"api-tests/src/test/kotlin/{layer}/Large{index}.kt"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text(
                f"package {layer}\nclass Large{index}\n/*" + "x" * 45_000 + "*/\n",
                encoding="utf-8",
            )
        (self.root / TEST_PATH).write_text(
            SOURCE.replace(
                "package tests\n", "package tests\n" + "\n".join(imports) + "\n"
            ).replace("fun testScenario() {}", f"fun testScenario() {{ {'; '.join(calls)} }}"),
            encoding="utf-8",
        )
        with patch("repository.subprocess.run", side_effect=self.process):
            total_result = self.repo.run_api_test(TARGET)
        self.assertEqual(total_result["status"], "VERIFIED")
        with self.assertRaisesRegex(ValueError, "total size"):
            self.repo.prepare_review_packet("API-9001 case", TARGET)


if __name__ == "__main__":
    unittest.main()
