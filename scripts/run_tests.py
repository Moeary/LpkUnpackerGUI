"""Run the unit test suite, retrying a few failed tests once in a fresh process.

Tests that only pass on the retry are reported as flaky (and as GitHub Actions
warnings plus a job summary entry on CI) so they stay visible.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
DEFAULT_MAX_RETRIES = 3

# Match `python -m unittest discover -s tests`: the repository root is importable.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def failed_test_ids(result: unittest.TestResult) -> list[str] | None:
    """Return the ids of failed tests, or None when a failure cannot be retried by id."""

    if result.unexpectedSuccesses:
        return None
    ids: set[str] = set()
    for test, _ in (*result.failures, *result.errors):
        # A failed subTest is retried through its parent test method.
        test = getattr(test, "test_case", test)
        # Import errors and setUpClass/setUpModule failures have no runnable test id.
        if not isinstance(test, unittest.TestCase) or type(test).__name__ == "_FailedTest":
            return None
        ids.add(test.id())
    return sorted(ids)


def retry_tests(test_ids: list[str]) -> bool:
    """Run the given tests once in a new interpreter and return whether they all passed."""

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(TESTS_DIR), env.get("PYTHONPATH"))))
    command = [sys.executable, "-m", "unittest", "-v", *test_ids]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode == 0


def report_flaky(test_ids: list[str]) -> None:
    print("\nFlaky tests (failed, then passed on retry):")
    for test_id in test_ids:
        print(f"  {test_id}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for test_id in test_ids:
            print(f"::warning title=Flaky test::{test_id} failed, then passed on retry")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write("### Flaky tests\n\nFailed on the first run, passed on retry:\n\n")
                summary.writelines(f"- `{test_id}`\n" for test_id in test_ids)
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
                        help="retry only when at most this many tests failed (0 disables retries)")
    args = parser.parse_args(argv)

    suite = unittest.defaultTestLoader.discover(str(TESTS_DIR))
    result = unittest.TextTestRunner().run(suite)
    sys.stderr.flush()
    if result.wasSuccessful():
        return 0

    test_ids = failed_test_ids(result)
    if test_ids is None:
        print("\nNot retrying: a failure is not tied to a single test method.")
        return 1
    if len(test_ids) > args.max_retries:
        print(f"\nNot retrying: {len(test_ids)} tests failed (limit {args.max_retries}).")
        return 1

    print(f"\nRetrying {len(test_ids)} failed test(s) in a new process:", flush=True)
    if not retry_tests(test_ids):
        return 1
    report_flaky(test_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
