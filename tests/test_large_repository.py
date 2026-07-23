from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot import planning
from logpilot.config import ScanConfig, load_config
from logpilot.models import Issue, LogCall, Severity
from logpilot.pipeline import run_scan
from logpilot.planning import build_scan_plan, save_scan_plan
from logpilot.result_store import RunResultStore
from logpilot.storage import DATA_DIR_ENV, repository_data_dir
from logpilot.web import build_server


class LargeRepositoryTests(unittest.TestCase):
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

    def test_module_markers_and_ids_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for module in ("services/api", "packages/web"):
                root = repo / module
                root.mkdir(parents=True)
                marker = "pyproject.toml" if module.startswith("services") else "package.json"
                (root / marker).write_text("{}", encoding="utf-8")
                extension = ".py" if module.startswith("services") else ".ts"
                (root / f"main{extension}").write_text("print('debug')\n", encoding="utf-8")

            first = build_scan_plan(repo, ScanConfig())
            second = build_scan_plan(repo, ScanConfig())

            self.assertEqual([module.path for module in first.modules], ["packages/web", "services/api"])
            self.assertEqual([module.id for module in first.modules], [module.id for module in second.modules])

    @unittest.skipUnless(shutil.which("git"), "Git is required for nested repository discovery")
    def test_untracked_directory_with_nested_git_repository_falls_back_to_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            subprocess.run(["git", "init", str(parent)], check=True, capture_output=True)
            selected = parent / "DTView"
            nested = selected / "vendor"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", str(nested)], check=True, capture_output=True)
            (selected / "main.cpp").write_text("qDebug() << 1;\n", encoding="utf-8")
            (nested / "library.cpp").write_text("LOG(INFO) << 1;\n", encoding="utf-8")

            plan = build_scan_plan(selected, ScanConfig())

            self.assertEqual(plan.discovery_method, "walk")
            self.assertEqual(plan.source_files, 2)
            self.assertEqual(plan.selected_files, 2)
            self.assertEqual(plan.discovered_languages, {"cpp": 2})
            self.assertEqual(
                [item.path for module in plan.modules for chunk in module.chunks for item in chunk.files],
                ["main.cpp", "vendor/library.cpp"],
            )

    def test_git_directory_entry_falls_back_and_discovers_cpp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            nested = repo / "nested"
            nested.mkdir()
            source = nested / "service.cpp"
            source.write_text("qWarning() << 1;\n", encoding="utf-8")
            completed = mock.Mock(returncode=0, stdout=b"nested\0")

            with mock.patch.object(planning.subprocess, "run", return_value=completed):
                plan = build_scan_plan(repo, ScanConfig())

            self.assertEqual(plan.discovery_method, "walk")
            self.assertEqual(plan.source_files, 1)
            self.assertEqual(plan.modules[0].chunks[0].files[0].path, "nested/service.cpp")

    def test_empty_git_result_falls_back_to_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.cpp").write_text("printf(\"ready\");\n", encoding="utf-8")
            completed = mock.Mock(returncode=0, stdout=b"")

            with mock.patch.object(planning.subprocess, "run", return_value=completed):
                plan = build_scan_plan(repo, ScanConfig())

            self.assertEqual(plan.discovery_method, "walk")
            self.assertEqual(plan.source_files, 1)
            self.assertEqual(plan.selected_files, 1)

    def test_walk_fallback_respects_repository_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "vendor").mkdir()
            (repo / ".git").mkdir()
            (repo / "src" / "main.cpp").write_text("qDebug() << 1;\n", encoding="utf-8")
            (repo / "vendor" / "ignored.cpp").write_text("qDebug() << 2;\n", encoding="utf-8")
            (repo / ".git" / "ignored.cpp").write_text("qDebug() << 3;\n", encoding="utf-8")
            (repo / ".logpilot.yaml").write_text("scan:\n  exclude:\n    - .git\n    - vendor\n", encoding="utf-8")
            completed = mock.Mock(returncode=0, stdout=b"")

            with mock.patch.object(planning.subprocess, "run", return_value=completed):
                plan = build_scan_plan(repo, load_config(repo).scan)

            self.assertEqual(plan.source_files, 1)
            self.assertEqual(
                [item.path for module in plan.modules for chunk in module.chunks for item in chunk.files],
                ["src/main.cpp"],
            )

    def test_truly_empty_directory_remains_empty_after_git_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            completed = mock.Mock(returncode=0, stdout=b"")

            with mock.patch.object(planning.subprocess, "run", return_value=completed):
                plan = build_scan_plan(repo, ScanConfig())

            self.assertEqual(plan.discovery_method, "walk")
            self.assertEqual(plan.source_files, 0)
            self.assertEqual(plan.selected_files, 0)
            self.assertEqual(plan.modules, [])

    def test_web_rejects_starting_an_empty_scan_plan(self) -> None:
        class RuntimeRegistry:
            def resolve(self, _runtime_id):
                return mock.Mock(id="test")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            plan = build_scan_plan(repo, ScanConfig())
            save_scan_plan(plan)
            server = build_server(repo, port=0, runtime_registry=RuntimeRegistry())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = urllib.request.Request(
                    f"http://{host}:{port}/api/scans",
                    data=json.dumps({"path": str(repo), "runtime": "test", "plan_id": plan.id}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=10)
                try:
                    payload = json.loads(caught.exception.read().decode("utf-8"))
                    self.assertEqual(caught.exception.code, 422)
                    self.assertIn("未发现源码文件", payload["error"])
                finally:
                    caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_chunk_boundaries_are_stable_by_file_count_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for index in range(8):
                (repo / f"file_{index:02d}.py").write_text("x" * 9, encoding="utf-8")
            original_files = planning.CHUNK_MAX_FILES
            original_bytes = planning.CHUNK_MAX_BYTES
            planning.CHUNK_MAX_FILES = 3
            planning.CHUNK_MAX_BYTES = 20
            try:
                plan = build_scan_plan(repo, ScanConfig())
            finally:
                planning.CHUNK_MAX_FILES = original_files
                planning.CHUNK_MAX_BYTES = original_bytes

            chunks = plan.modules[0].chunks
            self.assertEqual([chunk.file_count for chunk in chunks], [2, 2, 2, 2])
            self.assertEqual(
                [item.path for chunk in chunks for item in chunk.files],
                [f"file_{index:02d}.py" for index in range(8)],
            )

    def test_large_file_is_skipped_unless_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "large.py"
            source.write_text("x" * 32, encoding="utf-8")
            original = planning.DEFAULT_MAX_FILE_BYTES
            planning.DEFAULT_MAX_FILE_BYTES = 16
            try:
                default_plan = build_scan_plan(repo, ScanConfig())
                enabled_plan = build_scan_plan(repo, ScanConfig(), include_large_files=True)
            finally:
                planning.DEFAULT_MAX_FILE_BYTES = original

            self.assertEqual([item.path for item in default_plan.skipped_large_files], ["large.py"])
            self.assertEqual(default_plan.modules, [])
            self.assertEqual(enabled_plan.modules[0].file_count, 1)

    def test_ten_thousand_files_form_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "src"
            source.mkdir()
            for index in range(10_000):
                (source / f"f{index:05d}.py").write_text("value = 1\n", encoding="utf-8")

            plan = build_scan_plan(repo, ScanConfig())

            self.assertTrue(plan.large_repository)
            self.assertEqual(plan.source_files, 10_000)
            self.assertEqual(sum(chunk.file_count for module in plan.modules for chunk in module.chunks), 10_000)
            self.assertTrue(all(chunk.file_count <= 1_000 for module in plan.modules for chunk in module.chunks))

    def test_sqlite_query_never_returns_more_than_two_hundred_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text("print('debug')\n", encoding="utf-8")
            plan = build_scan_plan(repo, ScanConfig())
            save_scan_plan(plan)
            data_dir = repository_data_dir(repo)
            store = RunResultStore.for_run(data_dir, "run-100k")
            store.initialize_run(plan, plan.modules, "codex", "standard")
            payload = json.dumps(
                asdict(
                    Issue(
                        id="placeholder",
                        file_path="service.py",
                        line=1,
                        severity=Severity.HIGH,
                        kind="debug_log",
                        title="调试日志",
                        reason="原因",
                        suggestion="建议",
                        source="rule",
                    )
                ),
                ensure_ascii=False,
            )
            with store.transaction() as connection:
                connection.executemany(
                    """
                    INSERT INTO issues(
                        run_id,module_id,chunk_id,issue_id,file_path,line,severity,action,
                        kind,title,search_text,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        (
                            store.run_id,
                            plan.modules[0].id,
                            plan.modules[0].chunks[0].id,
                            f"issue-{index}",
                            f"src/file-{index // 10}.py",
                            index % 500 + 1,
                            "high" if index % 2 else "low",
                            "delete" if index % 3 else "add",
                            "debug_log",
                            "调试日志",
                            f"src/file-{index // 10}.py 调试日志",
                            payload.replace("placeholder", f"issue-{index}"),
                        )
                        for index in range(100_000)
                    ),
                )

            page = store.query_issues(limit=1_000, severity="high", action="delete")

            self.assertEqual(page["limit"], 200)
            self.assertEqual(len(page["items"]), 200)
            self.assertGreater(page["total"], 200)
            self.assertTrue(page["has_more"])

    def test_web_plan_and_paginated_issue_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text("print('debug')\n", encoding="utf-8")
            report = run_scan(repo)
            self.assertIsNotNone(report)
            run_id = next((repository_data_dir(repo) / "runs").iterdir()).name
            server = build_server(repo, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = urllib.request.Request(
                    f"http://{host}:{port}/api/scan/plans",
                    data=json.dumps({"path": str(repo)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    plan_payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(plan_payload["plan"]["source_files"], 1)

                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/runs/{run_id}/issues?limit=500&action=delete",
                    timeout=10,
                ) as response:
                    issues = json.loads(response.read().decode("utf-8"))
                self.assertLessEqual(issues["limit"], 200)
                self.assertTrue(issues["items"])
            finally:
                server.shutdown()
                server.server_close()

    def test_scan_outputs_stay_outside_target_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "service.py").write_text("print('debug')\n", encoding="utf-8")

            run_scan(repo)

            self.assertFalse((repo / ".logpilot").exists())
            self.assertFalse(any(path.name == "results.sqlite3" for path in repo.rglob("*")))
            self.assertTrue(any((repository_data_dir(repo) / "runs").glob("*/results.sqlite3")))


if __name__ == "__main__":
    unittest.main()
