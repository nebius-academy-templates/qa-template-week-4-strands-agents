"""Synthetic adapter checks; no Gradle, backend, or model execution."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repository import Repository

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


if __name__ == "__main__":
    unittest.main()
