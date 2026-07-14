from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class RuntimeInfo:
    id: str
    name: str
    command: str
    status: str
    executable_path: str = ""
    version: str = ""
    error: str = ""
    built_in: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class RuntimeExecution:
    runtime_id: str
    status: str
    raw_response: str = ""
    error: str = ""
    duration_ms: int = 0


class RuntimeUnavailableError(RuntimeError):
    pass


class RuntimeRegistry:
    _SPECS = (
        ("codex", "Codex", "codex", "LOGPILOT_CODEX_PATH"),
        ("claude", "Claude", "claude", "LOGPILOT_CLAUDE_PATH"),
    )

    def __init__(
        self,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._which = which
        self._runner = runner
        self._runtimes: dict[str, RuntimeInfo] = {}

    def refresh(self) -> list[RuntimeInfo]:
        self._runtimes = {runtime.id: runtime for runtime in (self._probe(*spec) for spec in self._SPECS)}
        return list(self._runtimes.values())

    def list(self) -> list[RuntimeInfo]:
        if not self._runtimes:
            return self.refresh()
        return list(self._runtimes.values())

    def resolve(self, runtime_id: str) -> RuntimeInfo:
        runtimes = {runtime.id: runtime for runtime in self.list()}
        selected = runtime_id.strip().lower() or "auto"
        if selected == "auto":
            for candidate in ("codex", "claude"):
                runtime = runtimes.get(candidate)
                if runtime and runtime.status == "online":
                    return runtime
            raise RuntimeUnavailableError("未发现可用运行时，请先安装 Codex 或 Claude CLI。")

        runtime = runtimes.get(selected)
        if not runtime:
            raise RuntimeUnavailableError(f"不支持的运行时：{runtime_id}")
        if runtime.status != "online":
            detail = f"：{runtime.error}" if runtime.error else ""
            raise RuntimeUnavailableError(f"{runtime.name} 运行时不可用{detail}")
        return runtime

    def _probe(self, runtime_id: str, name: str, command: str, env_name: str) -> RuntimeInfo:
        configured = os.getenv(env_name, "").strip()
        executable = configured or self._which(command) or ""
        if configured and not Path(configured).exists():
            executable = self._which(configured) or ""
        if not executable:
            return RuntimeInfo(
                id=runtime_id,
                name=name,
                command=command,
                status="offline",
                error=f"未在 PATH 中找到 {command}，可通过 {env_name} 指定路径。",
            )

        executable_path = str(Path(executable).expanduser().resolve())
        try:
            result = self._runner(
                [*_launch_prefix(runtime_id, executable_path), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RuntimeInfo(
                id=runtime_id,
                name=name,
                command=command,
                status="offline",
                executable_path=executable_path,
                error=str(exc),
            )

        version = (result.stdout or result.stderr).strip().splitlines()
        if result.returncode != 0:
            return RuntimeInfo(
                id=runtime_id,
                name=name,
                command=command,
                status="offline",
                executable_path=executable_path,
                error=_bounded_text(result.stderr or result.stdout or "版本检测失败。"),
            )
        return RuntimeInfo(
            id=runtime_id,
            name=name,
            command=command,
            status="online",
            executable_path=executable_path,
            version=version[0] if version else "未知版本",
        )


class RuntimeExecutor:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> None:
        self._runner = runner or _run_process

    def execute(
        self,
        runtime: RuntimeInfo,
        prompt: str,
        repo_root: Path,
        schema: dict[str, Any],
        model: str = "",
        timeout_seconds: int = 180,
    ) -> RuntimeExecution:
        started = time.monotonic()
        try:
            if runtime.id == "codex":
                raw = self._execute_codex(runtime, prompt, repo_root, schema, model, timeout_seconds)
            elif runtime.id == "claude":
                raw = self._execute_claude(runtime, prompt, repo_root, schema, model, timeout_seconds)
            else:
                raise RuntimeError(f"不支持的运行时：{runtime.id}")
            return RuntimeExecution(
                runtime_id=runtime.id,
                status="ok",
                raw_response=raw,
                duration_ms=_elapsed_ms(started),
            )
        except subprocess.TimeoutExpired:
            return RuntimeExecution(
                runtime_id=runtime.id,
                status="timeout",
                error=f"运行时执行超过 {timeout_seconds} 秒。",
                duration_ms=_elapsed_ms(started),
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return RuntimeExecution(
                runtime_id=runtime.id,
                status="error",
                error=_bounded_text(str(exc)),
                duration_ms=_elapsed_ms(started),
            )

    def _execute_codex(
        self,
        runtime: RuntimeInfo,
        prompt: str,
        repo_root: Path,
        schema: dict[str, Any],
        model: str,
        timeout_seconds: int,
    ) -> str:
        with tempfile.TemporaryDirectory(prefix="logpilot-runtime-") as tmp:
            temp_dir = Path(tmp)
            schema_path = temp_dir / "schema.json"
            output_path = temp_dir / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = [
                *_launch_prefix(runtime.id, runtime.executable_path),
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if model:
                command.extend(["--model", model])
            command.append("-")
            result = self._run(command, prompt, repo_root, timeout_seconds)
            if result.returncode != 0:
                raise RuntimeError(_command_error("Codex", result))
            raw = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else ""
            if not raw:
                raw = _extract_json_text(result.stdout)
            return _normalize_json(raw)

    def _execute_claude(
        self,
        runtime: RuntimeInfo,
        prompt: str,
        repo_root: Path,
        schema: dict[str, Any],
        model: str,
        timeout_seconds: int,
    ) -> str:
        command = [
            *_launch_prefix(runtime.id, runtime.executable_path),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--tools",
            "",
            "--permission-mode",
            "plan",
            "--no-session-persistence",
        ]
        if model:
            command.extend(["--model", model])
        result = self._run(command, prompt, repo_root, timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError(_command_error("Claude", result))
        payload = json.loads(result.stdout)
        content: Any = payload.get("structured_output") if isinstance(payload, dict) else payload
        if content is None and isinstance(payload, dict):
            content = payload.get("result")
        if isinstance(content, str):
            return _normalize_json(content)
        return json.dumps(content, ensure_ascii=False)

    def _run(
        self,
        command: list[str],
        prompt: str,
        repo_root: Path,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            command,
            input=prompt,
            cwd=str(repo_root.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )


def _normalize_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    parsed = json.loads(text)
    return json.dumps(parsed, ensure_ascii=False)


def _extract_json_text(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("text")
            item = payload.get("item")
            if not message and isinstance(item, dict):
                message = item.get("text") or item.get("message")
            if isinstance(message, str):
                return message
            if "findings" in payload:
                return candidate
    raise RuntimeError("Codex 未返回可解析的 JSON 结果。")


def _command_error(name: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
    return f"{name} 执行失败（退出码 {result.returncode}）：{_bounded_text(detail)}"


def _bounded_text(value: str, limit: int = 2000) -> str:
    text = value.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _run_process(
    command: list[str],
    *,
    input: str,
    cwd: str,
    capture_output: bool,
    text: bool,
    encoding: str,
    errors: str,
    check: bool,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    del capture_output, text, check
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        errors=errors,
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return
    try:
        os.killpg(process.pid, 15)
    except ProcessLookupError:
        return


def _launch_prefix(runtime_id: str, executable_path: str) -> list[str]:
    executable = Path(executable_path)
    if os.name != "nt" or executable.suffix.lower() not in {".cmd", ".bat"}:
        return [executable_path]

    relative_scripts = {
        "codex": Path("node_modules/@openai/codex/bin/codex.js"),
        "claude": Path("node_modules/@anthropic-ai/claude-code/cli.js"),
    }
    script = executable.parent / relative_scripts.get(runtime_id, Path())
    node = executable.parent / "node.exe"
    node_path = str(node) if node.exists() else shutil.which("node")
    if node_path and script.is_file():
        return [str(Path(node_path).resolve()), str(script.resolve())]
    return [executable_path]
