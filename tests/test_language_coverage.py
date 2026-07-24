from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.config import ScanConfig
from logpilot.history import list_history_runs
from logpilot.pipeline import run_scan
from logpilot.remediation import apply_suggestions, rollback_apply
from logpilot.runtime import RuntimeExecution, RuntimeInfo
from logpilot.scanner import scan_repository_detailed
from logpilot.settings import load_repository_settings, save_repository_settings
from logpilot.storage import DATA_DIR_ENV, repository_data_dir


class RuntimeRegistry:
    def __init__(self) -> None:
        self.runtime = RuntimeInfo("codex", "Codex", "codex", "online", "codex.cmd", "test")

    def resolve(self, _runtime_id):
        return self.runtime


class LayeredExecutor:
    def execute(self, _runtime, prompt, *_args, **_kwargs):
        payload = json.loads(prompt)
        if "unsupported_samples" in payload:
            insights = [
                {
                    "target_id": item["target_id"],
                    "detected_language": item["declared_language"],
                    "logging_apis": ["tracing::info!", "tracing::error!"],
                    "notes": "检测到 tracing 日志宏。",
                    "confidence": 0.92,
                }
                for item in payload["unsupported_samples"]
            ]
            return RuntimeExecution("codex", "ok", json.dumps({"insights": insights}, ensure_ascii=False), duration_ms=1)
        if "candidates" in payload:
            apis = [
                {
                    "candidate_id": item["candidate_id"],
                    "is_logging_api": True,
                    "callee": item["callee"],
                    "level": "info",
                    "framework": "custom",
                    "confidence": 0.96,
                }
                for item in payload["candidates"]
            ]
            return RuntimeExecution("codex", "ok", json.dumps({"apis": apis}), duration_ms=1)
        if "targets" in payload:
            findings = [
                {
                    "target_id": item["target_id"],
                    "has_issue": True,
                    "severity": "high",
                    "title": "异常路径缺少错误日志",
                    "reason": "捕获异常后仅执行恢复操作。",
                    "suggestion": "记录异常对象和当前操作。",
                    "event_name": "operation_failed",
                }
                for item in payload["targets"]
            ]
            return RuntimeExecution("codex", "ok", json.dumps({"findings": findings}, ensure_ascii=False), duration_ms=1)
        findings = [
            {
                "log_call_id": item["log_call_id"],
                "has_issue": False,
                "severity": "low",
                "title": "无需调整",
                "reason": "日志上下文完整。",
                "suggestion": "保持现状。",
            }
            for item in payload["logs"]
        ]
        return RuntimeExecution("codex", "ok", json.dumps({"findings": findings}, ensure_ascii=False), duration_ms=1)


class FailingExecutor:
    def execute(self, *_args, **_kwargs):
        return RuntimeExecution("codex", "timeout", error="simulated timeout", duration_ms=1)


class LanguageCoverageTests(unittest.TestCase):
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

    def test_cpp_parser_recognizes_qt_glog_and_standard_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.cpp").write_text(
                """void run(bool failed) {
    qDebug() << "debug";
    qWarning() << "warning";
    LOG(INFO) << "started";
    LOG_IF(ERROR, failed) << "failed";
    std::cout << "console";
    printf("value=%d", 1);
    APP_TRACE("custom event");
    try { work(); } catch (const std::exception& error) { recover(); }
}
""",
                encoding="utf-8",
            )

            scan = scan_repository_detailed(repo, ScanConfig())

            self.assertEqual(scan.discovered_language_counts, {"cpp": 1})
            self.assertEqual(scan.language_file_counts, {"cpp": 1})
            self.assertEqual(len(scan.logs), 6)
            self.assertEqual(
                {log.callee for log in scan.logs},
                {"qDebug", "qWarning", "LOG", "std::cout", "printf"},
            )
            self.assertTrue(any(target.kind == "framework_candidate" and target.symbol == "APP_TRACE" for target in scan.analysis_targets))
            self.assertTrue(any(target.kind == "error_path" for target in scan.analysis_targets))

    def test_cpp_parser_ignores_comments_and_string_literals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "quiet.cpp").write_text(
                '// qDebug() << "not a call";\nconst char* sample = "LOG(ERROR) << fake";\n',
                encoding="utf-8",
            )

            scan = scan_repository_detailed(repo, ScanConfig())

            self.assertEqual(scan.files_scanned, 1)
            self.assertEqual(scan.logs, [])

    def test_cpp_inline_log_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "inline.cpp").write_text(
                'void run() { work(); qDebug() << "temporary"; finish(); }\n',
                encoding="utf-8",
            )

            scan = scan_repository_detailed(repo, ScanConfig())

            self.assertEqual(len(scan.logs), 1)
            self.assertFalse(scan.logs[0].safe_to_delete)

    def test_stderr_log_is_not_offered_as_automatic_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "error.c").write_text(
                'void fail() {\n    fprintf(stderr, "operation failed");\n}\n',
                encoding="utf-8",
            )

            report = run_scan(repo)
            stderr_issues = [issue for issue in report.issues if issue.log_call_id]

            self.assertTrue(stderr_issues)
            self.assertFalse(any(issue.patch_action for issue in stderr_issues))

    def test_cpp_exact_debug_deletion_can_be_applied_and_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "service.cpp"
            original = 'void run() {\n    qDebug() << "temporary";\n}\n'
            source.write_text(original, encoding="utf-8")

            report = run_scan(repo)
            run_id = list_history_runs(repository_data_dir(repo))[0]["run_id"]
            issue_ids = [
                issue.id
                for issue in report.issues
                if issue.log_call_id and issue.patch_action == "delete"
            ]

            self.assertTrue(issue_ids)
            record = apply_suggestions(repo, run_id, issue_ids)
            self.assertNotIn("qDebug", source.read_text(encoding="utf-8"))
            rollback_apply(repo, record["apply_id"])
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_analysis_depth_is_persisted_and_invalid_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            saved = save_repository_settings(repo, {"analysis_depth": "deep"})
            self.assertEqual(saved.analysis_depth, "deep")
            self.assertEqual(load_repository_settings(repo).analysis_depth, "deep")
            fallback = save_repository_settings(repo, {"analysis_depth": "unexpected"})
            self.assertEqual(fallback.analysis_depth, "standard")

    def test_unsupported_files_are_reported_without_reducing_analyzable_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "main.rs").write_text('fn main() { println!("hello"); }\n', encoding="utf-8")
            (repo / "tool.py").write_text("print('debug')\n", encoding="utf-8")

            report = run_scan(repo)

            self.assertEqual(report.summary.discovered_files, 1)
            self.assertEqual(report.summary.files_scanned, 1)
            self.assertEqual(report.summary.coverage_status, "complete")
            self.assertEqual(report.summary.coverage_ratio, 1.0)
            self.assertEqual(report.summary.score_status, "local_only")
            self.assertIsNotNone(report.summary.score)
            self.assertEqual(report.summary.unsupported_files, 1)

    def test_zero_log_repository_is_not_scored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

            report = run_scan(repo)

            self.assertEqual(report.summary.coverage_status, "complete")
            self.assertEqual(report.summary.score_status, "no_log_samples")
            self.assertIsNone(report.summary.score)

    def test_ai_samples_unsupported_language_without_claiming_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "main.rs").write_text(
                'fn main() { tracing::info!("started"); }\n',
                encoding="utf-8",
            )

            report = run_scan(
                repo,
                runtime_id="codex",
                runtime_registry=RuntimeRegistry(),
                runtime_executor=LayeredExecutor(),
            )

            self.assertEqual(report.summary.coverage_status, "unsupported")
            self.assertEqual(report.summary.files_scanned, 0)
            self.assertIsNone(report.summary.score)
            self.assertEqual(report.language_insights[0]["logging_apis"], ["tracing::info!", "tracing::error!"])
            self.assertTrue(report.language_insights[0]["advisory_only"])
            self.assertIn("unsupported_language", {trace.task for trace in report.ai_traces})

    def test_unknown_code_extension_is_reported_as_unrecognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.zigx").write_text(
                "pub fn run() void {\n    const value = 1;\n}\n",
                encoding="utf-8",
            )

            report = run_scan(repo)

            self.assertEqual(report.summary.discovered_files, 0)
            self.assertEqual(report.summary.unrecognized_files, 1)
            self.assertEqual(report.summary.unrecognized_extensions, {".zigx": 1})
            self.assertEqual(report.summary.coverage_status, "unsupported")
            self.assertIsNone(report.summary.score)

    def test_ai_promotes_custom_api_and_reviews_missing_cpp_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.cpp").write_text(
                'void run() { APP_TRACE("custom event"); try { work(); } catch (...) { recover(); } }\n',
                encoding="utf-8",
            )

            report = run_scan(
                repo,
                runtime_id="codex",
                runtime_registry=RuntimeRegistry(),
                runtime_executor=LayeredExecutor(),
            )

            self.assertTrue(any(log.callee == "APP_TRACE" for log in report.logs))
            self.assertTrue(any(issue.kind == "ai_missing_log" for issue in report.issues))
            self.assertEqual(report.summary.ai_status, "complete")
            self.assertEqual({trace.task for trace in report.ai_traces}, {"framework_discovery", "log_quality", "missing_log"})
            self.assertFalse((repo / ".logpilot").exists())
            self.assertTrue((repository_data_dir(repo) / "report.json").is_file())

    def test_ai_failure_preserves_local_results_and_marks_report_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.cpp").write_text('void run() { qWarning() << "failed"; }\n', encoding="utf-8")

            report = run_scan(
                repo,
                runtime_id="codex",
                runtime_registry=RuntimeRegistry(),
                runtime_executor=FailingExecutor(),
            )

            self.assertEqual(report.summary.log_count, 1)
            self.assertEqual(report.summary.ai_status, "partial")
            self.assertEqual(report.summary.score_status, "ai_incomplete")
            self.assertIsNone(report.summary.score)
            self.assertTrue((repository_data_dir(repo) / "report.json").is_file())


if __name__ == "__main__":
    unittest.main()
