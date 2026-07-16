from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.config import load_config
from logpilot.history import list_history_runs, load_history_run
from logpilot.locking import repository_operation_lock
from logpilot.pipeline import run_scan
from logpilot.runtime import RuntimeExecution, RuntimeInfo
from logpilot.settings import save_repository_settings
from logpilot.storage import DATA_DIR_ENV, repository_data_dir
import logpilot.web as web_module
from logpilot.web import _html, build_server


class FakeRuntimeRegistry:
    def __init__(self) -> None:
        self.runtime = RuntimeInfo(
            "codex",
            "Codex",
            "codex",
            "online",
            "C:/tools/codex.cmd",
            "codex-cli test",
        )

    def list(self):
        return [self.runtime]

    def refresh(self):
        return self.list()

    def resolve(self, runtime_id):
        return self.runtime


class FakeRuntimeExecutor:
    def execute(self, runtime, prompt, repo_root, schema, model="", timeout_seconds=180):
        logs = json.loads(prompt)["logs"]
        findings = [
            {
                "log_call_id": log["log_call_id"],
                "has_issue": False,
                "severity": "low",
                "title": "无需调整",
                "reason": "测试运行时未发现新增问题。",
                "suggestion": "保持现状。",
            }
            for log in logs
        ]
        return RuntimeExecution("codex", "ok", json.dumps({"findings": findings}, ensure_ascii=False), duration_ms=1)


class BlockingRuntimeExecutor(FakeRuntimeExecutor):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, *args, **kwargs):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试运行时等待超时")
        return super().execute(*args, **kwargs)


def _start_scan_job(host: str, port: int, repo: Path) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://{host}:{port}/api/scans",
        data=json.dumps({"path": str(repo), "runtime": "codex"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_for_scan_job(host: str, port: int, job_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"http://{host}:{port}/api/scans/{job_id}", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        job = payload["job"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("分析任务未在测试时限内结束")


class PipelineTests(unittest.TestCase):
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
            data_dir = repository_data_dir(repo)
            report_json = data_dir / "report.json"
            report_md = data_dir / "report.md"
            patch = data_dir / "changes.diff"
            history = list_history_runs(data_dir)

            self.assertFalse((repo / ".logpilot").exists())
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
            first_log_issue = next(issue for issue in payload["issues"] if issue["log_call_id"])
            self.assertIn("logger.info", first_log_issue["context"])
            exception_issue = next(issue for issue in payload["issues"] if issue["kind"] == "missing_exception_log")
            self.assertIn("except Exception", exception_issue["context"])
            historical = load_history_run(data_dir, history[0]["run_id"])
            self.assertEqual(historical["metadata"]["issue_count"], len(report.issues))
            self.assertIn("report", historical)
            self.assertIn("changes.diff", [path.name for path in (data_dir / "runs" / history[0]["run_id"]).iterdir()])

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
                        "  runtime: claude",
                        "  timeout_seconds: 45",
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
            self.assertEqual(config.ai.runtime, "claude")
            self.assertEqual(config.ai.timeout_seconds, 45)
            self.assertEqual(config.rules.forbidden_logs, ["print", "console.log"])
            self.assertIn("vendor", config.scan.exclude)

    def test_custom_language_selection_limits_scanned_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text("print('python')\n", encoding="utf-8")
            (repo / "Worker.java").write_text(
                'class Worker { void run() { System.out.println("java"); } }\n',
                encoding="utf-8",
            )
            save_repository_settings(
                repo,
                {
                    "language_mode": "custom",
                    "selected_languages": ["java"],
                    "templates": {},
                },
            )

            report = run_scan(repo)

            self.assertEqual(report.summary.files_scanned, 1)
            self.assertTrue(report.logs)
            self.assertEqual({log.language for log in report.logs}, {"java"})

    def test_web_shell_contains_analysis_controls(self) -> None:
        html = _html()
        self.assertIn("LogPilot 本地分析工作台", html)
        self.assertIn("repoPath", html)
        self.assertIn("browseButton", html)
        self.assertIn("开始分析", html)
        self.assertIn('class="sidebar"', html)
        self.assertIn("仓库分析", html)
        self.assertNotIn("分析概览", html)
        self.assertIn('class="analysis-launch"', html)
        self.assertNotIn('class="topbar"', html)
        self.assertLess(html.index('id="currentPanel"'), html.index('class="analysis-launch"'))
        self.assertIn('id="analysisLanguagePreset"', html)
        self.assertIn('id="analysisTemplatePreset"', html)
        self.assertIn('id="addLanguagePreset"', html)
        self.assertIn('id="addTemplatePreset"', html)
        self.assertIn('id="resultSearch"', html)
        self.assertIn('id="severityFilters"', html)
        self.assertIn('id="resultStream"', html)
        self.assertIn('id="expandAllButton"', html)
        self.assertIn('id="collapseAllButton"', html)
        self.assertIn('data-file-expand-all=', html)
        self.assertIn('data-file-collapse-all=', html)
        self.assertIn("state.expandedGroups = new Set(currentGroups.map", html)
        self.assertIn("setVisibleGroupsExpanded", html)
        self.assertIn("setFileGroupsExpanded", html)
        self.assertIn('id="batchBar"', html)
        self.assertIn("搜索文件、问题或规则", html)
        self.assertIn("按文件分组", html)
        self.assertIn("选择可采纳项", html)
        self.assertNotIn("当前仓库", html)
        self.assertNotIn('class="breadcrumb"', html)
        self.assertNotIn('class="page-header"', html)
        self.assertNotIn('id="issueCountLabel"', html)
        self.assertNotIn('class="nav-count"', html)
        self.assertNotIn('id="historyCount"', html)
        self.assertIn('id="toastRegion"', html)
        self.assertIn("showToast", html)
        self.assertIn("--accent: #8b5cf6", html)
        self.assertIn('class="icon"', html)
        self.assertNotIn("--accent: #3ecf8e", html)
        self.assertNotIn("本地日志治理", html)
        self.assertNotIn('id="status"', html)
        self.assertIn("历史记录", html)
        self.assertIn('id="settingsTab"', html)
        self.assertIn("设置", html)
        self.assertIn('id="runtimeSelect"', html)
        self.assertIn('id="settingsPanel"', html)
        self.assertNotIn('id="runtimeCount"', html)
        self.assertIn("/api/runtimes", html)
        self.assertIn("相关代码", html)
        self.assertIn("修改预览", html)
        self.assertIn('class="risk-panel"', html)
        self.assertIn('class="diff-line ${type}"', html)
        self.assertIn('type = "add"', html)
        self.assertIn('type = "remove"', html)
        self.assertIn("codeContextMarkup", html)
        self.assertIn("完整修改", html)
        self.assertIn("批量采纳", html)
        self.assertIn("采纳此修改", html)
        self.assertIn("撤销上次采纳", html)
        self.assertIn("/api/apply", html)
        self.assertIn("分析诊断", html)
        self.assertIn('id="languageOptions"', html)
        self.assertIn('id="templateInput"', html)
        self.assertIn('id="settingsLanguagePreset"', html)
        self.assertIn('id="settingsTemplatePreset"', html)
        self.assertIn('id="analysisDepth"', html)
        self.assertIn("AI 分析深度", html)
        self.assertIn("解析失败", html)
        self.assertIn("本地 LogPilot 服务已退出，请重新启动服务", html)
        self.assertNotIn("Failed to fetch", html)
        self.assertIn('id="saveLanguagePreset"', html)
        self.assertIn('id="saveTemplatePreset"', html)
        self.assertIn('id="presetDialog"', html)
        self.assertIn("扫描并推荐", html)
        self.assertIn("支持自动补充", html)
        self.assertIn("issue.fix?.id", html)
        self.assertNotIn('id="logsTab"', html)
        self.assertNotIn('id="aiTab"', html)
        self.assertNotIn("issueDetail", html)
        self.assertNotIn("analysis-workspace", html)
        self.assertNotIn("sourceTab", html)
        self.assertNotIn("patchTab", html)
        self.assertIn("/api/scans", html)
        self.assertIn('id="scanProgress"', html)
        self.assertIn('id="cancelScanButton"', html)
        self.assertIn("pollScanJob", html)
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
            server = build_server(
                repo,
                port=0,
                runtime_registry=FakeRuntimeRegistry(),
                runtime_executor=FakeRuntimeExecutor(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                status, started = _start_scan_job(host, port, repo)
                self.assertEqual(status, 202)
                completed = _wait_for_scan_job(host, port, started["job"]["job_id"])
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["repository"], str(repo.resolve()))
                self.assertTrue((repository_data_dir(repo) / "report.json").exists())
                self.assertFalse((repo / ".logpilot").exists())

                with urllib.request.urlopen(f"http://{host}:{port}/api/report", timeout=10) as report_response:
                    report_payload = json.loads(report_response.read().decode("utf-8"))
                self.assertGreaterEqual(report_payload["summary"]["log_count"], 1)

                with urllib.request.urlopen(f"http://{host}:{port}/api/history", timeout=10) as history_response:
                    history_payload = json.loads(history_response.read().decode("utf-8"))
                self.assertEqual(history_response.status, 200)
                self.assertEqual(len(history_payload["runs"]), 1)
                self.assertEqual(history_payload["runs"][0]["runtime_id"], "codex")

                run_id = history_payload["runs"][0]["run_id"]
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/history/run?run_id={run_id}",
                    timeout=10,
                ) as run_response:
                    run_payload = json.loads(run_response.read().decode("utf-8"))
                self.assertEqual(run_response.status, 200)
                self.assertEqual(run_payload["metadata"]["run_id"], run_id)
                self.assertIn("patch", run_payload)

                issue_id = next(
                    issue["id"]
                    for issue in report_payload["issues"]
                    if issue.get("patch_action") == "delete"
                )
                apply_request = urllib.request.Request(
                    f"http://{host}:{port}/api/apply",
                    data=json.dumps({"run_id": run_id, "issue_ids": [issue_id]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(apply_request, timeout=10) as apply_response:
                    apply_payload = json.loads(apply_response.read().decode("utf-8"))
                self.assertEqual(apply_response.status, 200)
                self.assertNotIn("print('debug')", (repo / "service.py").read_text(encoding="utf-8"))

                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/applies?run_id={run_id}",
                    timeout=10,
                ) as applies_response:
                    applies_payload = json.loads(applies_response.read().decode("utf-8"))
                self.assertIn(issue_id, applies_payload["applied_issue_ids"])
                self.assertTrue(applies_payload["can_rollback"])

                rollback_request = urllib.request.Request(
                    f"http://{host}:{port}/api/apply/rollback",
                    data=json.dumps({"apply_id": apply_payload["record"]["apply_id"]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(rollback_request, timeout=10) as rollback_response:
                    rollback_payload = json.loads(rollback_response.read().decode("utf-8"))
                self.assertEqual(rollback_payload["record"]["status"], "rolled_back")
                self.assertIn("print('debug')", (repo / "service.py").read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()

    def test_web_scan_job_reports_repository_lock_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text("print('debug')\n", encoding="utf-8")
            server = build_server(
                repo,
                port=0,
                runtime_registry=FakeRuntimeRegistry(),
                runtime_executor=FakeRuntimeExecutor(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with repository_operation_lock(repo):
                    status, started = _start_scan_job(host, port, repo)
                    self.assertEqual(status, 202)
                    time.sleep(0.05)
                failed = _wait_for_scan_job(host, port, started["job"]["job_id"])
                self.assertEqual(failed["status"], "failed")
                self.assertIn("其他操作", failed["error"])
            finally:
                server.shutdown()
                server.server_close()

    def test_web_scan_streams_partial_report_and_can_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text("print('debug')\n", encoding="utf-8")
            executor = BlockingRuntimeExecutor()
            server = build_server(
                repo,
                port=0,
                runtime_registry=FakeRuntimeRegistry(),
                runtime_executor=executor,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                status, started = _start_scan_job(host, port, repo)
                self.assertEqual(status, 202)
                job_id = started["job"]["job_id"]
                self.assertTrue(executor.started.wait(timeout=5))

                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/scans/{job_id}?report_version=-1",
                    timeout=10,
                ) as response:
                    running = json.loads(response.read().decode("utf-8"))["job"]
                self.assertEqual(running["stage"], "runtime")
                self.assertEqual(running["status"], "running")
                self.assertGreater(running["partial_report"]["summary"]["issue_count"], 0)

                with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=10) as response:
                    active_scan = json.loads(response.read().decode("utf-8"))["active_scan"]
                self.assertEqual(active_scan["job_id"], job_id)
                self.assertIn("partial_report", active_scan)

                with self.assertRaises(urllib.error.HTTPError) as raised:
                    _start_scan_job(host, port, repo)
                self.assertEqual(raised.exception.code, 409)
                conflict = json.loads(raised.exception.read().decode("utf-8"))
                raised.exception.close()
                self.assertEqual(conflict["job"]["job_id"], job_id)

                cancel_request = urllib.request.Request(
                    f"http://{host}:{port}/api/scans/{job_id}/cancel",
                    data=b"",
                    method="POST",
                )
                with urllib.request.urlopen(cancel_request, timeout=10) as response:
                    cancelling = json.loads(response.read().decode("utf-8"))["job"]
                self.assertEqual(cancelling["status"], "cancelling")
                executor.release.set()
                cancelled = _wait_for_scan_job(host, port, job_id)
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertFalse((repository_data_dir(repo) / "report.json").exists())
            finally:
                executor.release.set()
                server.shutdown()
                server.server_close()

    def test_web_repository_settings_profile_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text(
                "import logging\nlogger = logging.getLogger(__name__)\nlogger.exception('failed')\n",
                encoding="utf-8",
            )
            (repo / "Worker.java").write_text("class Worker {}\n", encoding="utf-8")
            server = build_server(repo, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                profile_request = urllib.request.Request(
                    f"http://{host}:{port}/api/settings/profile",
                    data=json.dumps({"path": str(repo)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(profile_request, timeout=10) as response:
                    profile_payload = json.loads(response.read().decode("utf-8"))

                detected = {item["id"]: item for item in profile_payload["profile"]["detected_languages"]}
                self.assertEqual(detected["python"]["file_count"], 1)
                self.assertEqual(detected["java"]["file_count"], 1)
                self.assertEqual(
                    profile_payload["profile"]["template_recommendations"]["python"]["source"],
                    "repository",
                )

                save_request = urllib.request.Request(
                    f"http://{host}:{port}/api/settings",
                    data=json.dumps(
                        {
                            "path": str(repo),
                            "settings": {
                                "language_mode": "custom",
                                "selected_languages": ["python", "java"],
                                "templates": {"python": 'logger.exception("{event}")'},
                                "language_presets": [
                                    {
                                        "id": "backend-languages",
                                        "name": "后端服务",
                                        "languages": ["python", "java"],
                                    }
                                ],
                                "template_presets": [
                                    {
                                        "id": "service-templates",
                                        "name": "服务模板",
                                        "templates": {
                                            "python": 'logger.exception("{event}")',
                                            "java": 'logger.error("{event}", {exception})',
                                        },
                                    }
                                ],
                                "active_language_preset": "backend-languages",
                                "active_template_preset": "service-templates",
                                "analysis_depth": "deep",
                            },
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(save_request, timeout=10) as response:
                    saved = json.loads(response.read().decode("utf-8"))

                self.assertEqual(saved["settings"]["language_mode"], "custom")
                self.assertEqual(saved["settings"]["selected_languages"], ["python", "java"])
                self.assertEqual(saved["settings"]["language_presets"][0]["name"], "后端服务")
                self.assertEqual(saved["settings"]["template_presets"][0]["name"], "服务模板")
                self.assertEqual(saved["settings"]["active_language_preset"], "backend-languages")
                self.assertEqual(saved["settings"]["active_template_preset"], "service-templates")
                self.assertEqual(saved["settings"]["analysis_depth"], "deep")
                self.assertTrue((repository_data_dir(repo) / "settings.json").is_file())
                self.assertTrue((repository_data_dir(repo) / "language-profile.json").is_file())
                self.assertFalse((repo / ".logpilot").exists())
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
