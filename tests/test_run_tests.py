from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_tests import failed_test_ids, report_flaky


def _run(*tests: unittest.TestCase | unittest.TestSuite) -> unittest.TestResult:
    result = unittest.TestResult()
    unittest.TestSuite(tests).run(result)
    return result


class _Fixtures:
    """Holds deliberately failing cases out of reach of test discovery."""

    class Sample(unittest.TestCase):
        def test_fails(self):
            self.fail("boom")

        def test_errors(self):
            raise RuntimeError("boom")

        def test_subtest_fails(self):
            for value in (1, 2):
                with self.subTest(value=value):
                    self.assertEqual(value, 0)

        @unittest.expectedFailure
        def test_unexpected_success(self):
            pass

    class BrokenSetUpClass(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError("boom")

        def test_never_runs(self):
            pass


class FailedTestIdsTests(unittest.TestCase):
    def test_collects_failures_and_errors(self):
        sample = _Fixtures.Sample
        result = _run(sample("test_fails"), sample("test_errors"))

        self.assertEqual(failed_test_ids(result), [sample("test_errors").id(), sample("test_fails").id()])

    def test_failed_subtests_retry_their_parent_test_once(self):
        result = _run(_Fixtures.Sample("test_subtest_fails"))

        self.assertEqual(failed_test_ids(result), [_Fixtures.Sample("test_subtest_fails").id()])

    def test_class_setup_failure_is_not_retried(self):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_Fixtures.BrokenSetUpClass)

        self.assertIsNone(failed_test_ids(_run(suite)))

    def test_import_failure_is_not_retried(self):
        suite = unittest.defaultTestLoader.loadTestsFromName("tests_that_do_not_exist_run_tests")

        self.assertIsNone(failed_test_ids(_run(suite)))

    def test_unexpected_success_is_not_retried(self):
        self.assertIsNone(failed_test_ids(_run(_Fixtures.Sample("test_unexpected_success"))))


class ReportFlakyTests(unittest.TestCase):
    def test_github_actions_gets_warning_and_job_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = Path(temp_dir) / "summary.md"
            env = {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}
            with patch.dict(os.environ, env), patch("builtins.print") as fake_print:
                report_flaky(["pkg.Case.test_a"])

            printed = [call.args[0] for call in fake_print.call_args_list if call.args]
            self.assertIn("::warning title=Flaky test::pkg.Case.test_a failed, then passed on retry", printed)
            self.assertIn("- `pkg.Case.test_a`", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
