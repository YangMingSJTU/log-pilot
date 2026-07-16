from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.config import ScanConfig
from logpilot.history import load_history_run, write_history_run
from logpilot.models import LogCall, ParseFailure
from logpilot.native_parser_client import NativeParseResult, NativeParserClient, _format_exit_code
from logpilot.reporting import build_report, render_markdown, write_report
from logpilot.scanner import scan_repository_detailed


def _log_payload(file_path: str) -> dict[str, object]:
    return {
        "id": f"{file_path}:1:0",
        "file_path": file_path,
        "line": 1,
        "column": 0,
        "language": "cpp",
        "level": "info",
        "callee": "LOG",
        "message": "ready",
        "context": '1: LOG(INFO) << "ready";',
        "source_line": 'LOG(INFO) << "ready";',
        "end_line": 1,
        "safe_to_delete": True,
    }


class _QueueStdout:
    def __init__(self) -> None:
        self.values: queue.Queue[str] = queue.Queue()

    def readline(self) -> str:
        return self.values.get()

    def close(self) -> None:
        self.values.put("")


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process

    def write(self, value: str) -> int:
        self.process.handle_request(json.loads(value))
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, action: str) -> None:
        self.action = action
        self.stdout = _QueueStdout()
        self.stdin = _FakeStdin(self)
        self.returncode: int | None = None
        self.terminated = False

    def handle_request(self, request: dict[str, object]) -> None:
        if self.action == "crash":
            self.returncode = 0xC0000374
            self.stdout.values.put("")
            return
        if self.action == "timeout":
            return
        if self.action == "invalid_json":
            self.stdout.values.put("not-json\n")
            return
        request_id = str(request["request_id"])
        if self.action == "mismatch":
            request_id = "wrong-request"
        if self.action == "parse_error":
            self.stdout.values.put(
                json.dumps(
                    {
                        "request_id": request_id,
                        "status": "error",
                        "error_kind": "parse_error",
                        "message": "synthetic parse failure",
                    }
                )
                + "\n"
            )
            return
        response = {
            "request_id": request_id,
            "status": "ok",
            "logs": [_log_payload(Path(str(request["file_path"])).name)],
            "targets": [],
        }
        self.stdout.values.put(json.dumps(response) + "\n")

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15
        self.stdout.values.put("")

    def kill(self) -> None:
        self.terminate()


class _ProcessFactory:
    def __init__(self, actions: list[str]) -> None:
        self.actions = list(actions)
        self.commands: list[list[str]] = []
        self.processes: list[_FakeProcess] = []

    def __call__(self, command: list[str]):
        self.commands.append(command)
        process = _FakeProcess(self.actions.pop(0))
        self.processes.append(process)
        return process


class NativeParserClientTests(unittest.TestCase):
    def test_worker_crash_is_recorded_and_next_file_restarts_worker(self) -> None:
        factory = _ProcessFactory(["crash", "ok"])
        client = NativeParserClient(process_factory=factory, timeout_seconds=0.2)

        first = client.parse_file(Path("C:/repo/first.cpp"), Path("C:/repo"), "cpp")
        second = client.parse_file(Path("C:/repo/second.cpp"), Path("C:/repo"), "cpp")
        client.close()

        self.assertEqual(first.failure.error_kind, "native_crash")
        self.assertEqual(first.failure.worker_exit_code, 0xC0000374)
        self.assertIn("0xc0000374", first.failure.message.lower())
        self.assertEqual(len(second.logs), 1)
        self.assertEqual(len(factory.commands), 2)
        self.assertEqual(factory.commands[0], [sys.executable, "-m", "logpilot.native_parser_worker"])

    def test_timeout_terminates_worker(self) -> None:
        factory = _ProcessFactory(["timeout"])
        client = NativeParserClient(process_factory=factory, timeout_seconds=0.03)

        result = client.parse_file(Path("C:/repo/slow.cpp"), Path("C:/repo"), "cpp")

        self.assertEqual(result.failure.error_kind, "timeout")
        self.assertTrue(factory.processes[0].terminated)

    def test_invalid_json_and_mismatched_request_are_protocol_errors(self) -> None:
        for action in ("invalid_json", "mismatch"):
            with self.subTest(action=action):
                factory = _ProcessFactory([action])
                client = NativeParserClient(process_factory=factory, timeout_seconds=0.2)
                result = client.parse_file(Path("C:/repo/file.cpp"), Path("C:/repo"), "cpp")
                self.assertEqual(result.failure.error_kind, "protocol_error")
                self.assertTrue(factory.processes[0].terminated)

    def test_worker_start_failure_is_recoverable(self) -> None:
        def fail_start(_command):
            raise OSError("cannot start")

        client = NativeParserClient(process_factory=fail_start, timeout_seconds=0.2)
        result = client.parse_file(Path("C:/repo/file.cpp"), Path("C:/repo"), "cpp")

        self.assertEqual(result.failure.error_kind, "worker_start_failed")
        self.assertTrue(result.failure.recoverable)

    def test_parse_error_keeps_worker_available_for_the_next_file(self) -> None:
        factory = _ProcessFactory(["parse_error"])
        client = NativeParserClient(process_factory=factory, timeout_seconds=0.2)

        result = client.parse_file(Path("C:/repo/file.cpp"), Path("C:/repo"), "cpp")

        self.assertEqual(result.failure.error_kind, "parse_error")
        self.assertFalse(factory.processes[0].terminated)
        client.close()

    def test_cancellation_terminates_worker(self) -> None:
        factory = _ProcessFactory(["timeout"])
        client = NativeParserClient(process_factory=factory, timeout_seconds=5)
        checks = 0

        def should_cancel() -> bool:
            nonlocal checks
            checks += 1
            return checks > 1

        with self.assertRaises(InterruptedError):
            client.parse_file(Path("C:/repo/file.cpp"), Path("C:/repo"), "cpp", should_cancel)
        self.assertTrue(factory.processes[0].terminated)

    def test_windows_exit_code_format_includes_hex(self) -> None:
        self.assertIn("0xc0000374", _format_exit_code(0xC0000374).lower())

    def test_importing_scanner_does_not_load_native_tree_sitter_modules(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import logpilot.scanner; "
                "print(any(name.startswith('tree_sitter') for name in sys.modules))",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "False")


class _ScannerClient:
    def __init__(self, cancel: bool = False) -> None:
        self.cancel = cancel
        self.closed = False

    def parse_file(self, path: Path, repo_root: Path, language: str, should_cancel=None) -> NativeParseResult:
        if self.cancel:
            raise InterruptedError("cancelled")
        if path.name == "first.cpp":
            return NativeParseResult(
                failure=ParseFailure(path.name, language, "native_crash", "worker exited", 87),
            )
        return NativeParseResult(logs=[LogCall(**_log_payload(path.name))])

    def close(self) -> None:
        self.closed = True


class NativeScannerIntegrationTests(unittest.TestCase):
    def test_failed_cpp_file_does_not_stop_following_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "first.cpp").write_text("broken", encoding="utf-8")
            (repo / "second.cpp").write_text('LOG(INFO) << "ready";', encoding="utf-8")
            client = _ScannerClient()

            scan = scan_repository_detailed(
                repo,
                ScanConfig(),
                native_client_factory=lambda: client,
            )

            self.assertEqual(scan.files_scanned, 1)
            self.assertEqual(scan.failed_files, 1)
            self.assertEqual(scan.parse_failures[0].error_kind, "native_crash")
            self.assertEqual(len(scan.logs), 1)
            self.assertTrue(client.closed)

    def test_scanner_closes_worker_when_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "file.cpp").write_text("broken", encoding="utf-8")
            client = _ScannerClient(cancel=True)

            with self.assertRaises(InterruptedError):
                scan_repository_detailed(
                    repo,
                    ScanConfig(),
                    native_client_factory=lambda: client,
                )
            self.assertTrue(client.closed)

    def test_real_worker_parses_redacted_qt_fixture(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "jkqtpbasicimagetools_minimal.h"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            target = repo / fixture.name
            target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
            client = NativeParserClient(timeout_seconds=10)

            result = client.parse_file(target, repo, "cpp")
            client.close()

            self.assertIsNone(result.failure)
            self.assertEqual(
                {log.callee for log in result.logs},
                {"qWarning", "LOG", "std::cerr"},
            )
            self.assertTrue(any(target.kind == "framework_candidate" for target in result.targets))


class ParseFailureReportingTests(unittest.TestCase):
    def test_partial_report_persists_parse_failure_without_score(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as output_tmp:
            repo = Path(repo_tmp)
            output = Path(output_tmp)
            failure = ParseFailure(
                file_path="first.cpp",
                language="cpp",
                error_kind="native_crash",
                message="worker exited (0xc0000374)",
                worker_exit_code=0xC0000374,
            )
            log = LogCall(**_log_payload("second.cpp"))

            report = build_report(
                repo,
                1,
                [log],
                [],
                [],
                discovered_language_counts={"cpp": 2},
                analyzed_language_counts={"cpp": 1},
                failed_language_counts={"cpp": 1},
                parse_failures=[failure],
            )
            write_report(report, output)
            metadata = write_history_run(report, "", output)
            history = load_history_run(output, metadata["run_id"])

            self.assertEqual(report.summary.coverage_status, "partial")
            self.assertEqual(report.summary.score_status, "insufficient_coverage")
            self.assertIsNone(report.summary.score)
            self.assertEqual(report.parse_failures[0].file_path, "first.cpp")
            self.assertIn("first.cpp", render_markdown(report))
            saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["parse_failures"][0]["error_kind"], "native_crash")
            self.assertEqual(history["report"]["parse_failures"][0]["file_path"], "first.cpp")


if __name__ == "__main__":
    unittest.main()
