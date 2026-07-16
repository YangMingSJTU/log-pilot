from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .models import AnalysisTarget, LogCall, ParseFailure, relative_path


NATIVE_PARSE_TIMEOUT_SECONDS = 30.0
_ERROR_KINDS = {"parse_error", "native_crash", "timeout", "protocol_error", "worker_start_failed"}
ProcessFactory = Callable[[list[str]], Any]


@dataclass(slots=True)
class NativeParseResult:
    logs: list[LogCall] = field(default_factory=list)
    targets: list[AnalysisTarget] = field(default_factory=list)
    failure: ParseFailure | None = None


class NativeParserClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = NATIVE_PARSE_TIMEOUT_SECONDS,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._process_factory = process_factory or _launch_process
        self._process: Any | None = None
        self._responses: queue.Queue[tuple[str, str]] | None = None
        self._reader: threading.Thread | None = None

    def parse_file(
        self,
        path: Path,
        repo_root: Path,
        language: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> NativeParseResult:
        repo_root = repo_root.resolve()
        path = path.resolve()
        file_path = relative_path(path, repo_root)
        start_error = self._ensure_worker()
        if start_error:
            return NativeParseResult(
                failure=ParseFailure(file_path, language, "worker_start_failed", start_error)
            )

        request_id = uuid.uuid4().hex
        request = {
            "request_id": request_id,
            "repo_root": str(repo_root),
            "file_path": str(path),
            "language": language,
        }
        try:
            self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            exit_code = self._worker_exit_code()
            self._discard_worker()
            return NativeParseResult(
                failure=self._crash_failure(file_path, language, exit_code, f"写入工作进程失败：{exc}")
            )

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if should_cancel and should_cancel():
                self._discard_worker()
                raise InterruptedError("分析已取消。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._discard_worker()
                return NativeParseResult(
                    failure=ParseFailure(
                        file_path,
                        language,
                        "timeout",
                        f"单文件解析超过 {self.timeout_seconds:g} 秒，工作进程已终止。",
                    )
                )
            try:
                kind, value = self._responses.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if kind == "eof":
                exit_code = self._worker_exit_code()
                self._discard_worker()
                if exit_code not in {None, 0}:
                    failure = self._crash_failure(file_path, language, exit_code)
                else:
                    failure = ParseFailure(file_path, language, "protocol_error", "原生解析通信意外断开。")
                return NativeParseResult(failure=failure)
            if kind == "reader_error":
                self._discard_worker()
                return NativeParseResult(
                    failure=ParseFailure(file_path, language, "protocol_error", value)
                )
            return self._decode_response(value, request_id, file_path, language)

    def close(self) -> None:
        self._discard_worker(graceful=True)

    def _ensure_worker(self) -> str:
        if self._process is not None and self._process.poll() is None:
            return ""
        self._clear_worker()
        command = [sys.executable, "-m", "logpilot.native_parser_worker"]
        try:
            process = self._process_factory(command)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"无法启动原生解析工作进程：{exc}"
        if process.stdin is None or process.stdout is None:
            try:
                process.terminate()
            except OSError:
                pass
            return "原生解析工作进程缺少标准输入或输出管道。"
        responses: queue.Queue[tuple[str, str]] = queue.Queue()
        reader = threading.Thread(
            target=_read_responses,
            args=(process.stdout, responses),
            name="logpilot-native-parser-reader",
            daemon=True,
        )
        self._process = process
        self._responses = responses
        self._reader = reader
        reader.start()
        return ""

    def _decode_response(
        self,
        raw: str,
        request_id: str,
        file_path: str,
        language: str,
    ) -> NativeParseResult:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._protocol_failure(file_path, language, f"工作进程返回无效 JSON：{exc}")
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            return self._protocol_failure(file_path, language, "工作进程响应编号与请求不匹配。")
        if payload.get("status") == "error":
            error_kind = str(payload.get("error_kind", "parse_error"))
            if error_kind not in _ERROR_KINDS:
                error_kind = "protocol_error"
            if error_kind == "protocol_error":
                return self._protocol_failure(file_path, language, str(payload.get("message", "协议错误")))
            return NativeParseResult(
                failure=ParseFailure(
                    file_path,
                    language,
                    error_kind,
                    str(payload.get("message", "原生解析失败。")),
                )
            )
        if payload.get("status") != "ok" or not isinstance(payload.get("logs"), list) or not isinstance(payload.get("targets"), list):
            return self._protocol_failure(file_path, language, "工作进程响应结构无效。")
        try:
            logs = [LogCall(**item) for item in payload["logs"] if isinstance(item, dict)]
            targets = [AnalysisTarget(**item) for item in payload["targets"] if isinstance(item, dict)]
        except (TypeError, ValueError) as exc:
            return self._protocol_failure(file_path, language, f"工作进程响应字段无效：{exc}")
        return NativeParseResult(logs=logs, targets=targets)

    def _protocol_failure(self, file_path: str, language: str, message: str) -> NativeParseResult:
        self._discard_worker()
        return NativeParseResult(
            failure=ParseFailure(file_path, language, "protocol_error", message)
        )

    def _crash_failure(
        self,
        file_path: str,
        language: str,
        exit_code: int | None,
        detail: str = "",
    ) -> ParseFailure:
        message = "原生解析工作进程异常退出"
        if exit_code is not None:
            message += f"（{_format_exit_code(exit_code)}）"
        if detail:
            message += f"：{detail}"
        return ParseFailure(file_path, language, "native_crash", message, exit_code)

    def _worker_exit_code(self) -> int | None:
        if self._process is None:
            return None
        code = self._process.poll()
        if code is not None:
            return code
        try:
            return self._process.wait(timeout=0.2)
        except (subprocess.TimeoutExpired, TimeoutError):
            return self._process.poll()

    def _discard_worker(self, graceful: bool = False) -> None:
        process = self._process
        if process is None:
            return
        if graceful:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=0.5)
            except (subprocess.TimeoutExpired, TimeoutError):
                _terminate_process(process)
            else:
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass
        else:
            _terminate_process(process)
        self._clear_worker()

    def _clear_worker(self) -> None:
        self._process = None
        self._responses = None
        self._reader = None


def _launch_process(command: list[str]):
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _read_responses(stream, responses: queue.Queue[tuple[str, str]]) -> None:
    try:
        while True:
            line = stream.readline()
            if line == "":
                responses.put(("eof", ""))
                return
            responses.put(("line", line))
    except (OSError, ValueError) as exc:
        responses.put(("reader_error", f"读取工作进程响应失败：{exc}"))


def _terminate_process(process) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.kill()
                process.wait(timeout=0.5)
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        pass
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.stdout.close()
    except (OSError, ValueError):
        pass


def _format_exit_code(exit_code: int) -> str:
    unsigned = exit_code & 0xFFFFFFFF
    if os.name == "nt" or unsigned >= 0x80000000:
        return f"exit code {exit_code} / 0x{unsigned:08x}"
    return f"exit code {exit_code}"
