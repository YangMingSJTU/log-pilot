from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.config import load_config
from logpilot.history import list_history_runs, load_history_run
from logpilot.pipeline import run_scan
import logpilot.web as web_module
from logpilot.web import _html, build_server


class PipelineTests(unittest.TestCase):
    def test_scan_writes_report_and_patch_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text(
                "\n".join(
                    [
                        "import logging",
                        "logger = logging.getLogger(__name__)",
                        "def run(user):",
                        "    logger.info('start')",
                        "    print('debug')",
                        "    logger.info(user.password)",
                        "    try:",
                        "        user.save()",
                        "    except Exception:",
                        "        pass",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "web.js").write_text(
                "function boot() { console.log('debug'); }",
                encoding="utf-8",
            )

            report = run_scan(repo)
            report_json = repo / ".logpilot" / "report.json"
            report_md = repo / ".logpilot" / "report.md"
            patch = repo / ".logpilot" / "changes.diff"
            history = list_history_runs(repo / ".logpilot")

            self.assertTrue(report_json.exists())
            self.assertTrue(report_md.exists())
            self.assertTrue(patch.exists())
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["score"], report.summary.score)
            self.assertGreaterEqual(report.summary.log_count, 4)
            kinds = {issue.kind for issue in report.issues}
            self.assertIn("low_value_log", kinds)
            self.assertIn("debug_log", kinds)
            self.assertIn("sensitive_log", kinds)
            self.assertIn("missing_exception_log", kinds)
            self.assertIn("日志信息价值过低", {issue.title for issue in report.issues})
            self.assertIn("-    print('debug')", patch.read_text(encoding="utf-8"))

            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["issue_count"], len(report.issues))
            historical = load_history_run(repo / ".logpilot", history[0]["run_id"])
            self.assertEqual(historical["metadata"]["issue_count"], len(report.issues))
            self.assertIn("report", historical)
            self.assertIn("changes.diff", [path.name for path in (repo / ".logpilot" / "runs" / history[0]["run_id"]).iterdir()])

    def test_config_loads_yaml_like_file_without_pyyaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".logpilot.yaml").write_text(
                "\n".join(
                    [
                        "rules:",
                        "  forbidden_logs:",
                        "    - print",
                        "    - console.log",
                        "ai:",
                        "  enabled: true",
                        "scan:",
                        "  exclude:",
                        "    - .git",
                        "    - vendor",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(repo)
            self.assertTrue(config.ai.enabled)
            self.assertEqual(config.rules.forbidden_logs, ["print", "console.log"])
            self.assertIn("vendor", config.scan.exclude)

    def test_web_shell_contains_analysis_controls(self) -> None:
        html = _html()
        self.assertIn("LogPilot 本地分析工作台", html)
        self.assertIn("repoPath", html)
        self.assertIn("browseButton", html)
        self.assertIn("开始分析", html)
        self.assertIn('class="sidebar"', html)
        self.assertIn("分析概览", html)
        self.assertIn("问题清单", html)
        self.assertIn('id="historyCount"', html)
        self.assertIn('id="toastRegion"', html)
        self.assertIn("showToast", html)
        self.assertNotIn("本地日志治理", html)
        self.assertNotIn('id="status"', html)
        self.assertIn("历史记录", html)
        self.assertIn("补丁预览", html)
        self.assertIn("issueDetail", html)
        self.assertIn("/api/scan", html)
        self.assertIn("/api/browse", html)

    def test_web_scan_endpoint_runs_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text(
                "\n".join(
                    [
                        "import logging",
                        "logger = logging.getLogger(__name__)",
                        "def run():",
                        "    print('debug')",
                    ]
                ),
                encoding="utf-8",
            )
            server = build_server(repo, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = urllib.request.Request(
                    f"http://{host}:{port}/api/scan",
                    data=json.dumps({"path": str(repo)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["repository"], str(repo.resolve()))
                self.assertGreaterEqual(payload["report"]["summary"]["log_count"], 1)
                self.assertEqual(len(payload["history"]), 1)
                self.assertTrue((repo / ".logpilot" / "report.json").exists())

                with urllib.request.urlopen(f"http://{host}:{port}/api/history", timeout=10) as history_response:
                    history_payload = json.loads(history_response.read().decode("utf-8"))
                self.assertEqual(history_response.status, 200)
                self.assertEqual(len(history_payload["runs"]), 1)

                run_id = history_payload["runs"][0]["run_id"]
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/history/run?run_id={run_id}",
                    timeout=10,
                ) as run_response:
                    run_payload = json.loads(run_response.read().decode("utf-8"))
                self.assertEqual(run_response.status, 200)
                self.assertEqual(run_payload["metadata"]["run_id"], run_id)
                self.assertIn("patch", run_payload)
            finally:
                server.shutdown()
                server.server_close()

    def test_web_browse_endpoint_returns_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            original_choose_directory = web_module.choose_directory
            web_module.choose_directory = lambda initial_dir: repo
            server = build_server(repo, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = urllib.request.Request(
                    f"http://{host}:{port}/api/browse",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertFalse(payload["cancelled"])
                self.assertEqual(payload["path"], str(repo))
            finally:
                web_module.choose_directory = original_choose_directory
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
