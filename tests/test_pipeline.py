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
from logpilot.pipeline import run_scan
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

            self.assertTrue(report_json.exists())
            self.assertTrue(report_md.exists())
            self.assertTrue(patch.exists())
            self.assertGreaterEqual(report.summary.log_count, 4)
            kinds = {issue.kind for issue in report.issues}
            self.assertIn("low_value_log", kinds)
            self.assertIn("debug_log", kinds)
            self.assertIn("sensitive_log", kinds)
            self.assertIn("missing_exception_log", kinds)
            self.assertIn("-    print('debug')", patch.read_text(encoding="utf-8"))

            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["issue_count"], len(report.issues))

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
        self.assertIn("LogPilot Analysis Workbench", html)
        self.assertIn("repoPath", html)
        self.assertIn("开始分析", html)
        self.assertIn("Patch 预览", html)
        self.assertIn("/api/scan", html)

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
                self.assertTrue((repo / ".logpilot" / "report.json").exists())
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
