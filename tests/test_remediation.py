from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.cli import main
from logpilot.history import list_history_runs
from logpilot.pipeline import run_scan
from logpilot.remediation import (
    ApplyConflictError,
    RemediationError,
    _safe_source_path,
    apply_status,
    apply_suggestions,
    rollback_apply,
)
import logpilot.remediation as remediation_module
from logpilot.storage import DATA_DIR_ENV, repository_data_dir, repository_id


class RemediationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get(DATA_DIR_ENV)
        os.environ[DATA_DIR_ENV] = self.data_tmp.name

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = self.previous_data_dir
        self.data_tmp.cleanup()

    def test_repository_storage_is_isolated_and_never_writes_target_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = Path(first_tmp)
            second = Path(second_tmp)
            (first / "one.py").write_text("print('debug')\n", encoding="utf-8")
            (second / "two.py").write_text("print('debug')\n", encoding="utf-8")

            run_scan(first)
            run_scan(second)

            self.assertNotEqual(repository_id(first), repository_id(second))
            self.assertTrue((repository_data_dir(first) / "report.json").is_file())
            self.assertTrue((repository_data_dir(second) / "report.json").is_file())
            self.assertFalse((first / ".logpilot").exists())
            self.assertFalse((second / ".logpilot").exists())

    def test_inline_and_multiline_calls_are_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text(
                "value = 1; print('inline')\nlogger.info(\n    'multiline',\n)\n",
                encoding="utf-8",
            )
            (repo / "web.js").write_text(
                "function boot() { console.log('inline'); }\n",
                encoding="utf-8",
            )

            report = run_scan(repo)

            self.assertGreaterEqual(report.summary.log_count, 3)
            self.assertFalse(any(issue.patch_action for issue in report.issues))

    def test_source_path_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            with self.assertRaises(RemediationError):
                _safe_source_path(repo, "../outside.py")
            with self.assertRaises(RemediationError):
                _safe_source_path(repo, str((repo.parent / "outside.py").resolve()))

    def test_apply_deduplicates_same_log_and_rollback_restores_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "service.py"
            original = "def run():\n    print('debug')\n"
            source.write_text(original, encoding="utf-8")
            report = run_scan(repo)
            run_id = list_history_runs(repository_data_dir(repo))[0]["run_id"]
            matching = [issue.id for issue in report.issues if issue.log_call_id and issue.patch_action == "delete"]
            self.assertGreaterEqual(len(matching), 2)

            record = apply_suggestions(repo, run_id, matching)

            self.assertEqual(len(record["operations"]), 1)
            self.assertEqual(record["operations"][0]["line_numbers"], [2])
            self.assertEqual(source.read_text(encoding="utf-8"), "def run():\n")
            self.assertEqual(set(apply_status(repo, run_id)["applied_issue_ids"]), set(record["issue_ids"]))
            rollback_apply(repo, record["apply_id"])
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_apply_rejects_changed_context_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first = repo / "first.py"
            second = repo / "second.py"
            first_original = "print('debug')\n"
            second_original = "print('debug')\n"
            first.write_text(first_original, encoding="utf-8")
            second.write_text(second_original, encoding="utf-8")
            report = run_scan(repo)
            run_id = list_history_runs(repository_data_dir(repo))[0]["run_id"]
            issue_ids = [issue.id for issue in report.issues if issue.patch_action == "delete"]
            second.write_text("# changed\n" + second_original, encoding="utf-8")

            with self.assertRaises(ApplyConflictError):
                apply_suggestions(repo, run_id, issue_ids)

            self.assertEqual(first.read_text(encoding="utf-8"), first_original)
            self.assertEqual(second.read_text(encoding="utf-8"), "# changed\n" + second_original)

    def test_apply_restores_written_files_when_a_later_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first = repo / "first.py"
            second = repo / "second.py"
            first.write_text("print('first')\n", encoding="utf-8")
            second.write_text("print('second')\n", encoding="utf-8")
            report = run_scan(repo)
            run_id = list_history_runs(repository_data_dir(repo))[0]["run_id"]
            issue_ids = [issue.id for issue in report.issues if issue.patch_action == "delete"]
            original_write = remediation_module._atomic_write
            calls = 0

            def flaky_write(path: Path, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated write failure")
                original_write(path, content)

            with mock.patch("logpilot.remediation._atomic_write", side_effect=flaky_write):
                with self.assertRaises(RemediationError):
                    apply_suggestions(repo, run_id, issue_ids)

            self.assertEqual(first.read_text(encoding="utf-8"), "print('first')\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "print('second')\n")

    def test_sequential_applies_from_same_run_account_for_known_line_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "service.py"
            original = "def run():\n    print('first')\n    value = 1\n    print('second')\n    return value\n"
            source.write_text(original, encoding="utf-8")
            report = run_scan(repo)
            run_id = list_history_runs(repository_data_dir(repo))[0]["run_id"]
            patchable = [issue for issue in report.issues if issue.patch_action == "delete"]
            first_issue = next(issue for issue in patchable if issue.line == 2)
            second_issue = next(issue for issue in patchable if issue.line == 4)

            first_record = apply_suggestions(repo, run_id, [first_issue.id])
            second_record = apply_suggestions(repo, run_id, [second_issue.id])

            self.assertEqual(source.read_text(encoding="utf-8"), "def run():\n    value = 1\n    return value\n")
            rollback_apply(repo, second_record["apply_id"])
            self.assertEqual(source.read_text(encoding="utf-8"), "def run():\n    value = 1\n    print('second')\n    return value\n")
            rollback_apply(repo, first_record["apply_id"])
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_rollback_rejects_source_changed_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "service.py"
            source.write_text("print('debug')\n", encoding="utf-8")
            report = run_scan(repo)
            run_id = list_history_runs(repository_data_dir(repo))[0]["run_id"]
            issue_id = next(issue.id for issue in report.issues if issue.patch_action == "delete")
            record = apply_suggestions(repo, run_id, [issue_id])
            source.write_text("print('new work')\n", encoding="utf-8")

            with self.assertRaises(ApplyConflictError):
                rollback_apply(repo, record["apply_id"])

            self.assertEqual(source.read_text(encoding="utf-8"), "print('new work')\n")

    def test_cli_report_apply_and_rollback_use_user_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "service.py"
            source.write_text("print('debug')\n", encoding="utf-8")
            report = run_scan(repo)
            issue_id = next(issue.id for issue in report.issues if issue.patch_action == "delete")
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                main(["report", str(repo)])
                main(["apply", str(repo), "--issue", issue_id, "--yes"])
                main(["rollback", str(repo)])

            self.assertIn("Repository:", output.getvalue())
            self.assertIn("Applied:", output.getvalue())
            self.assertIn("Rolled back:", output.getvalue())
            self.assertEqual(source.read_text(encoding="utf-8"), "print('debug')\n")


if __name__ == "__main__":
    unittest.main()
