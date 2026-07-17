from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .history import list_history_runs, load_history_run
from .config import load_config
from .locking import RepositoryBusyError, repository_operation_lock
from .parsers import LANGUAGE_BY_SUFFIX
from .pipeline import run_scan
from .remediation import (
    ApplyConflictError,
    ApplyNotFoundError,
    RemediationError,
    apply_status,
    apply_suggestions,
    rollback_apply,
)
from .runtime import RuntimeExecutor, RuntimeRegistry
from .scanner import scan_repository_detailed
from .scan_jobs import ScanJob, TERMINAL_SCAN_STATUSES
from .settings import (
    SettingsError,
    build_language_profile,
    save_repository_settings,
    settings_payload,
)
from .storage import load_last_repository, repository_data_dir, save_last_repository


def build_server(
    repo_root: Path | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
) -> ThreadingHTTPServer:
    initial_root = _initial_repository(repo_root)
    save_last_repository(initial_root)
    state: dict[str, Any] = {
        "repo_root": initial_root,
        "artifacts": repository_data_dir(initial_root),
        "runtime_id": "auto",
    }
    runtimes = runtime_registry or RuntimeRegistry()
    executor = runtime_executor or RuntimeExecutor()
    browse_lock = threading.Lock()
    scan_jobs: dict[str, ScanJob] = {}
    scan_jobs_lock = threading.Lock()

    def activate_repository(target: Path, runtime_id: str | None = None) -> None:
        resolved = save_last_repository(target)
        state["repo_root"] = resolved
        state["artifacts"] = repository_data_dir(resolved)
        if runtime_id is not None:
            state["runtime_id"] = runtime_id

    def execute_scan_job(job: ScanJob, target: Path, runtime) -> None:
        try:
            report = run_scan(
                target,
                runtime_id=runtime.id,
                runtime_registry=runtimes,
                runtime_executor=executor,
                progress=job.update,
                should_cancel=job.should_cancel,
            )
            activate_repository(target, runtime.id)
            history = list_history_runs(state["artifacts"])
            run_id = str(history[0].get("run_id", "")) if history else ""
            job.complete(report, run_id)
        except InterruptedError:
            job.cancel()
        except Exception as exc:
            job.fail(str(exc))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send("text/html; charset=utf-8", _html())
            elif parsed.path == "/api/state":
                payload = _state_payload(state)
                with scan_jobs_lock:
                    active_jobs = [job for job in scan_jobs.values() if not job.is_terminal]
                payload["active_scan"] = active_jobs[-1].snapshot() if active_jobs else None
                self._send_json(payload)
            elif parsed.path == "/api/report":
                self._send_file(state["artifacts"] / "report.json", "application/json; charset=utf-8")
            elif parsed.path == "/api/patch":
                self._send_file(state["artifacts"] / "changes.diff", "text/plain; charset=utf-8")
            elif parsed.path == "/api/history":
                self._send_json({"repository": str(state["repo_root"]), "runs": list_history_runs(state["artifacts"])})
            elif parsed.path == "/api/history/run":
                self._send_history_run(parsed.query)
            elif parsed.path == "/api/applies":
                run_ids = parse_qs(parsed.query).get("run_id", [])
                self._send_json(apply_status(state["repo_root"], run_ids[0] if run_ids else None))
            elif parsed.path == "/api/runtimes":
                self._send_json(_runtime_payload(runtimes, state, refresh=False))
            elif parsed.path == "/api/settings":
                paths = parse_qs(parsed.query).get("path", [])
                target = _resolve_repo_path(paths[0]) if paths else state["repo_root"]
                if not target.is_dir():
                    self._send_json({"error": f"仓库路径不存在：{target}"}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"repository": str(target), **settings_payload(target)})
            elif parsed.path.startswith("/api/scans/"):
                self._send_scan_job(parsed.path, parsed.query)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/scans":
                self._handle_scan_start()
            elif parsed.path.startswith("/api/scans/") and parsed.path.endswith("/cancel"):
                self._handle_scan_cancel(parsed.path)
            elif parsed.path == "/api/browse":
                self._handle_browse()
            elif parsed.path == "/api/runtimes/refresh":
                self._send_json(_runtime_payload(runtimes, state, refresh=True))
            elif parsed.path == "/api/apply":
                self._handle_apply()
            elif parsed.path == "/api/apply/rollback":
                self._handle_rollback()
            elif parsed.path == "/api/settings":
                self._handle_settings_save()
            elif parsed.path == "/api/settings/profile":
                self._handle_settings_profile()
            elif parsed.path == "/api/repository":
                self._handle_repository_select()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args) -> None:
            return

        def _handle_scan_start(self) -> None:
            try:
                payload = self._read_json()
                target = _resolve_repo_path(str(payload.get("path", "")))
                if not target.exists() or not target.is_dir():
                    self._send_json({"error": f"仓库路径不存在：{target}"}, HTTPStatus.BAD_REQUEST)
                    return

                requested_runtime = str(payload.get("runtime", "auto")).strip() or "auto"
                selected_runtime = runtimes.resolve(requested_runtime)
                repository = str(target.resolve())
                with scan_jobs_lock:
                    active = next(
                        (
                            job
                            for job in scan_jobs.values()
                            if job.repository == repository and not job.is_terminal
                        ),
                        None,
                    )
                    if active:
                        self._send_json(
                            {"error": "该仓库已有分析任务正在运行。", "job": active.snapshot()},
                            HTTPStatus.CONFLICT,
                        )
                        return
                    job = ScanJob(repository, selected_runtime.id)
                    scan_jobs[job.id] = job
                    completed_jobs = [
                        item for item in scan_jobs.values() if item.status in TERMINAL_SCAN_STATUSES
                    ]
                    for old_job in completed_jobs[:-20]:
                        scan_jobs.pop(old_job.id, None)
                worker = threading.Thread(
                    target=execute_scan_job,
                    args=(job, target, selected_runtime),
                    daemon=True,
                    name=f"logpilot-scan-{job.id[:8]}",
                )
                worker.start()
                self._send_json(
                    {"job": job.snapshot(), "runtime": selected_runtime.to_dict()},
                    HTTPStatus.ACCEPTED,
                )
            except Exception as exc:  # Keep the local UI useful during early scanner work.
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _send_scan_job(self, path: str, query: str) -> None:
            job = self._scan_job(path)
            if not job:
                self._send_json({"error": "分析任务不存在或已过期。"}, HTTPStatus.NOT_FOUND)
                return
            versions = parse_qs(query).get("report_version", ["-1"])
            try:
                known_version = int(versions[0])
            except ValueError:
                known_version = -1
            self._send_json({"job": job.snapshot(known_version)})

        def _handle_scan_cancel(self, path: str) -> None:
            job = self._scan_job(path, cancel=True)
            if not job:
                self._send_json({"error": "分析任务不存在或已过期。"}, HTTPStatus.NOT_FOUND)
                return
            if not job.request_cancel():
                self._send_json({"error": "分析任务已经结束。", "job": job.snapshot()}, HTTPStatus.CONFLICT)
                return
            self._send_json({"job": job.snapshot()})

        def _scan_job(self, path: str, cancel: bool = False) -> ScanJob | None:
            parts = [part for part in path.split("/") if part]
            expected = 4 if cancel else 3
            if len(parts) != expected or parts[:2] != ["api", "scans"]:
                return None
            if cancel and parts[-1] != "cancel":
                return None
            with scan_jobs_lock:
                return scan_jobs.get(parts[2])

        def _handle_apply(self) -> None:
            try:
                payload = self._read_json()
                run_id = str(payload.get("run_id", "")).strip()
                raw_issue_ids = payload.get("issue_ids", [])
                if not run_id:
                    raise RemediationError("缺少分析记录 ID。")
                if not isinstance(raw_issue_ids, list):
                    raise RemediationError("问题 ID 必须使用数组传递。")
                issue_ids = [str(issue_id) for issue_id in raw_issue_ids]
                record = apply_suggestions(state["repo_root"], run_id, issue_ids)
                self._send_json(
                    {
                        "record": record,
                        "applies": apply_status(state["repo_root"], run_id),
                    }
                )
            except RepositoryBusyError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except ApplyConflictError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except ApplyNotFoundError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except RemediationError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_rollback(self) -> None:
            try:
                payload = self._read_json()
                apply_id = str(payload.get("apply_id", "")).strip() or None
                record = rollback_apply(state["repo_root"], apply_id)
                self._send_json(
                    {
                        "record": record,
                        "applies": apply_status(state["repo_root"], str(record.get("run_id", ""))),
                    }
                )
            except RepositoryBusyError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except ApplyConflictError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except ApplyNotFoundError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except RemediationError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_browse(self) -> None:
            if not browse_lock.acquire(blocking=False):
                self._send_json(
                    {"error": "已有选择窗口打开，请先关闭或稍后再试。"},
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                payload = self._read_json()
                initial_dir = _browse_initial_directory(
                    str(payload.get("path", "")),
                    state["repo_root"],
                )
                selected = choose_directory(initial_dir)
                if not selected:
                    self._send_json({"cancelled": True, "path": ""})
                    return
                resolved = _validate_repository(selected)
                activate_repository(resolved)
                self._send_json({"cancelled": False, "path": str(resolved), **_state_payload(state)})
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            finally:
                browse_lock.release()

        def _handle_repository_select(self) -> None:
            try:
                payload = self._read_json()
                target = _validate_repository(_resolve_repo_path(str(payload.get("path", ""))))
                activate_repository(target)
                self._send_json(_state_payload(state))
            except (OSError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_settings_save(self) -> None:
            try:
                payload = self._read_json()
                target = _resolve_repo_path(str(payload.get("path", state["repo_root"])))
                if not target.is_dir():
                    raise SettingsError(f"仓库路径不存在：{target}")
                raw_settings = payload.get("settings", {})
                if not isinstance(raw_settings, dict):
                    raise SettingsError("设置内容必须是对象。")
                save_repository_settings(target, raw_settings)
                self._send_json({"repository": str(target), **settings_payload(target)})
            except SettingsError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_settings_profile(self) -> None:
            try:
                payload = self._read_json()
                target = _resolve_repo_path(str(payload.get("path", state["repo_root"])))
                if not target.is_dir():
                    raise SettingsError(f"仓库路径不存在：{target}")
                with repository_operation_lock(target):
                    config = load_config(target)
                    config.scan.include_extensions = sorted(LANGUAGE_BY_SUFFIX)
                    scan = scan_repository_detailed(target, config.scan)
                    build_language_profile(
                        target,
                        scan.logs,
                        config.scan.exclude,
                        scan.discovered_language_counts,
                        scan.unrecognized_extension_counts,
                    )
                self._send_json({"repository": str(target), **settings_payload(target)})
            except RepositoryBusyError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except SettingsError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _send_history_run(self, query: str) -> None:
            run_ids = parse_qs(query).get("run_id", [])
            if not run_ids:
                self._send_json({"error": "缺少历史记录 ID。"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self._send_json(load_history_run(state["artifacts"], run_ids[0]))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)

        def _read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError("请求内容必须是 JSON 对象。")
            return loaded

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.exists():
                self._send_json(
                    {"error": f"产物不存在：{path.name}", "repository": str(state["repo_root"])},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send(content_type, path.read_text(encoding="utf-8", errors="ignore"))

        def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send("application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False), status)

        def _send(self, content_type: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((host, port), Handler)


def serve(repo_root: Path | None, host: str = "127.0.0.1", port: int = 8765) -> None:
    initial_root = _initial_repository(repo_root)
    server = build_server(initial_root, host, port)
    artifacts = repository_data_dir(initial_root)
    print(f"LogPilot UI: http://{host}:{port}")
    print(f"Default repository: {initial_root}")
    print(f"Reading artifacts from: {artifacts}")
    server.serve_forever()


def choose_directory(initial_dir: Path) -> Path | None:
    return _choose_directory_tk_subprocess(initial_dir)


def _initial_repository(repo_root: Path | None) -> Path:
    fallback = Path.cwd() if repo_root is None else repo_root
    if repo_root is None:
        return load_last_repository(fallback)
    return _validate_repository(repo_root)


def _validate_repository(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"仓库路径不存在：{resolved}")
    return resolved


def _browse_initial_directory(raw_path: str, fallback: Path) -> Path:
    candidate = Path(raw_path.strip()).expanduser() if raw_path.strip() else fallback
    candidate = candidate.resolve()
    if candidate.is_file():
        return candidate.parent
    current = candidate
    while not current.is_dir() and current != current.parent:
        current = current.parent
    return current if current.is_dir() else _validate_repository(fallback)


def _choose_directory_tk_subprocess(initial_dir: Path, timeout_seconds: int = 120) -> Path | None:
    script = r'''
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

initial_dir = sys.argv[1]
root = tk.Tk()
root.withdraw()
root.update()
root.attributes("-topmost", True)
try:
    selected = filedialog.askdirectory(
        parent=root,
        initialdir=initial_dir,
        title="选择 LogPilot 要分析的仓库",
        mustexist=True,
    )
    if selected:
        print(Path(selected).resolve())
finally:
    root.destroy()
'''
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(initial_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("选择窗口超时，请手动输入路径或重试。") from exc

    if result.returncode != 0:
        message = result.stderr.strip() or "选择窗口打开失败，请手动输入路径或重试。"
        raise RuntimeError(message)
    selected = result.stdout.strip()
    return Path(selected).resolve() if selected else None


def _resolve_repo_path(raw_path: str) -> Path:
    if not raw_path.strip():
        raise ValueError("请先输入仓库路径。")
    return Path(raw_path).expanduser().resolve()


def _state_payload(state: dict[str, Any]) -> dict[str, object]:
    artifacts = state["artifacts"]
    return {
        "repository": str(state["repo_root"]),
        "has_report": (artifacts / "report.json").exists(),
        "history": list_history_runs(artifacts),
        "runtime_id": state.get("runtime_id", "auto"),
    }


def _runtime_payload(
    registry: RuntimeRegistry,
    state: dict[str, Any],
    refresh: bool,
) -> dict[str, object]:
    available = registry.refresh() if refresh else registry.list()
    return {
        "runtimes": [runtime.to_dict() for runtime in available],
        "selected": state.get("runtime_id", "auto"),
    }


def _html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LogPilot 本地分析工作台</title>
  <style>
    /* Compact developer console with a shared component language. */
    :root {
      color-scheme: dark;
      --bg: #09090a;
      --surface: #0f0f11;
      --surface-2: #141416;
      --surface-3: #19191c;
      --elevated: #1d1d21;
      --ink: #f4f4f5;
      --muted: #a1a1aa;
      --subtle: #71717a;
      --line: #2b2b30;
      --line-soft: #222226;
      --line-strong: #3d3d44;
      --accent: #8b5cf6;
      --accent-strong: #7c3aed;
      --accent-hover: #9568f7;
      --accent-soft: rgba(139, 92, 246, .12);
      --blue: #60a5fa;
      --green: #34d399;
      --amber: #fbbf24;
      --red: #fb7185;
      --code: #070708;
    }
    * { box-sizing: border-box; }
    * { scrollbar-width: thin; scrollbar-color: #3f3f46 transparent; }
    *::-webkit-scrollbar { width: 8px; height: 8px; }
    *::-webkit-scrollbar-thumb { border: 2px solid transparent; border-radius: 8px; background: #3f3f46; background-clip: padding-box; }
    *::-webkit-scrollbar-track { background: transparent; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow: hidden;
    }
    button, input, select, textarea { font: inherit; }
    button, input, .runtime-control, .nav-item, .result-item {
      transition: border-color .14s ease, background-color .14s ease, color .14s ease, box-shadow .14s ease;
    }
    button {
      height: 38px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 0 14px;
      border: 1px solid #9f7aea;
      border-radius: 6px;
      background: var(--accent-strong);
      color: #fff;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .14), 0 1px 2px rgba(0, 0, 0, .35);
      font-size: 12px;
      font-weight: 650;
      cursor: pointer;
      white-space: nowrap;
    }
    button:hover { border-color: #b69af0; background: var(--accent-hover); }
    button:active { box-shadow: inset 0 1px 2px rgba(0, 0, 0, .28); }
    button:disabled { opacity: .58; cursor: wait; }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 0;
      box-shadow: 0 0 0 2px var(--bg), 0 0 0 4px rgba(167, 139, 250, .72);
    }
    .secondary {
      background: var(--surface-3);
      border: 1px solid var(--line-strong);
      color: var(--ink);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04), 0 1px 2px rgba(0, 0, 0, .22);
    }
    .secondary:hover { border-color: #575760; background: #232328; }
    .icon {
      width: 15px;
      height: 15px;
      flex: 0 0 auto;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .app-shell {
      height: 100vh;
      height: 100dvh;
      display: grid;
      grid-template-columns: 232px minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr);
      overflow: hidden;
    }
    .sidebar {
      grid-column: 1;
      grid-row: 1 / -1;
      min-height: 0;
      height: 100vh;
      height: 100dvh;
      display: grid;
      grid-template-rows: 64px 1fr auto;
      border-right: 1px solid var(--line);
      background: #0c0c0d;
      overflow: hidden;
    }
    .brand {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 11px;
      padding: 0 20px;
      border-bottom: 1px solid var(--line);
    }
    .brand strong { font-size: 16px; font-weight: 700; }
    .brand-mark {
      width: 28px;
      height: 28px;
      display: grid;
      place-items: center;
      border: 1px solid #e4e4e7;
      border-radius: 7px;
      background: #f4f4f5;
      color: #18181b;
      box-shadow: 0 1px 2px rgba(0, 0, 0, .45);
      font-size: 11px;
      font-weight: 900;
    }
    .side-nav {
      min-height: 0;
      display: grid;
      align-content: start;
      gap: 4px;
      padding: 20px 12px 0;
      overflow-y: auto;
    }
    .nav-item {
      width: 100%;
      display: grid;
      grid-template-columns: 20px 1fr;
      gap: 10px;
      align-items: center;
      justify-items: start;
      padding: 0 12px;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 7px;
      color: var(--muted);
      text-align: left;
      box-shadow: none;
    }
    .nav-item:hover { border-color: transparent; background: #18181b; color: var(--ink); }
    .nav-item.active {
      background: var(--accent-soft);
      border-color: rgba(139, 92, 246, .28);
      color: #fff;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025);
    }
    .nav-icon { width: 18px; height: 18px; display: grid; place-items: center; color: var(--subtle); }
    .nav-icon .icon { width: 16px; height: 16px; }
    .nav-item.active .nav-icon { color: #bca7fb; }
    .sidebar-footer {
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 58px;
      padding: 0 20px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
    }
    .local-dot, .state-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 3px rgba(52, 211, 153, .12);
    }
    .analysis-launch {
      margin-bottom: 22px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025), 0 1px 2px rgba(0, 0, 0, .16);
      overflow: hidden;
    }
    .analysis-commandbar {
      min-height: 66px;
      display: grid;
      grid-template-columns: minmax(360px, 1fr) 180px auto;
      gap: 12px;
      align-items: center;
      padding: 13px 14px;
    }
    .analysis-options {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-top: 1px solid var(--line);
      background: #0f0f11;
    }
    .analysis-option {
      min-width: 0;
      min-height: 66px;
      display: grid;
      grid-template-columns: auto minmax(150px, 220px) minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
    }
    .analysis-option:not(:last-child) { border-right: 1px solid var(--line); }
    .analysis-option-label { color: var(--muted); font-size: 10px; font-weight: 650; }
    .analysis-option-summary {
      min-width: 0;
      overflow: hidden;
      color: var(--subtle);
      font-size: 10px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .scan-progress {
      margin-bottom: 22px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .scan-progress-header {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 11px 14px 10px 18px;
    }
    .scan-progress-copy { min-width: 0; }
    .scan-progress-title { display: flex; align-items: center; gap: 9px; }
    .scan-progress-title strong { font-size: 13px; }
    .scan-progress-title span { color: var(--accent-text); font-size: 11px; font-weight: 700; }
    .scan-progress-message {
      margin-top: 5px;
      overflow: hidden;
      color: var(--muted);
      font-size: 10px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .scan-progress-track { height: 3px; background: #242428; overflow: hidden; }
    .scan-progress-track i {
      display: block;
      width: var(--progress, 0%);
      height: 100%;
      background: var(--accent);
      transition: width .24s ease;
    }
    .scan-progress-track.indeterminate i {
      width: 28%;
      animation: progress-pulse 1.2s ease-in-out infinite;
    }
    .scan-progress.failed .scan-progress-title span { color: var(--red); }
    .scan-progress.failed .scan-progress-track i { background: var(--red); }
    .scan-progress.completed .scan-progress-title span { color: var(--green); }
    .scan-progress.completed .scan-progress-track i { background: var(--green); }
    .scan-steps {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      border-top: 1px solid var(--line-soft);
      background: #0f0f11;
    }
    .scan-step {
      min-width: 0;
      min-height: 48px;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 0 13px;
      border-right: 1px solid var(--line-soft);
      color: var(--subtle);
      font-size: 10px;
    }
    .scan-step:last-child { border-right: 0; }
    .scan-step::before {
      content: "";
      width: 7px;
      height: 7px;
      flex: 0 0 auto;
      border: 1px solid #55555e;
      border-radius: 50%;
      background: transparent;
    }
    .scan-step.active { color: var(--ink); }
    .scan-step.active::before { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
    .scan-step.done { color: var(--muted); }
    .scan-step.done::before { border-color: var(--green); background: var(--green); }
    .incremental-note {
      margin: -10px 0 18px;
      color: var(--muted);
      font-size: 10px;
    }
    .preset-control {
      min-width: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 34px;
      gap: 6px;
      align-items: center;
    }
    .preset-control select, .preset-library select {
      height: 34px;
      padding: 0 9px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #151518;
    }
    .preset-add { width: 34px; height: 34px; padding: 0; }
    .preset-add .icon { width: 14px; height: 14px; }
    .preset-library {
      min-height: 54px;
      display: grid;
      grid-template-columns: auto minmax(180px, 260px) auto auto auto;
      gap: 8px;
      align-items: center;
      padding: 9px 18px;
      border-bottom: 1px solid var(--line);
      background: #101012;
    }
    .preset-library-label { color: var(--muted); font-size: 10px; font-weight: 650; }
    .preset-library button { height: 34px; }
    .preset-library .icon-only { width: 34px; padding: 0; }
    .preset-dialog-body { display: grid; gap: 9px; padding: 22px; }
    .preset-dialog-body label { color: var(--muted); font-size: 11px; }
    .repo-control {
      min-width: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    input {
      width: 100%;
      height: 38px;
      padding: 0 12px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #111113;
      color: var(--ink);
      font-size: 13px;
      outline: none;
      box-shadow: inset 0 1px 2px rgba(0, 0, 0, .28);
      overflow: hidden;
      text-overflow: ellipsis;
    }
    input:hover { border-color: #505058; }
    input:focus { border-color: #8b5cf6; }
    .runtime-control {
      min-width: 0;
      height: 38px;
      display: grid;
      grid-template-columns: 8px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      padding: 0 8px 0 11px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #111113;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025);
    }
    .runtime-control .state-dot.offline {
      background: var(--red);
      box-shadow: 0 0 0 3px rgba(251, 113, 133, .12);
    }
    select {
      width: 100%;
      height: 36px;
      min-width: 0;
      border: 0;
      background: #111113;
      color: var(--ink);
      font-size: 12px;
      outline: none;
      cursor: pointer;
    }
    main {
      grid-column: 2;
      grid-row: 1;
      min-width: 0;
      min-height: 0;
      padding: 0;
      display: block;
      overflow: auto;
    }
    .view-panel {
      width: 100%;
      max-width: 1360px;
      margin: 0 auto;
      padding: 30px 48px 44px;
    }
    .summary-section {
      border: 0;
      background: transparent;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: minmax(190px, 1.25fr) repeat(3, minmax(105px, .75fr)) minmax(220px, 1.35fr);
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025), 0 1px 2px rgba(0, 0, 0, .16);
    }
    .score-panel, .metric, .risk-panel {
      min-width: 0;
      min-height: 112px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
      padding: 17px 18px;
      border-right: 1px solid var(--line);
    }
    .risk-panel { border-right: 0; }
    .metric { justify-content: center; }
    .metric-label {
      color: var(--muted);
      font-size: 10px;
      line-height: 1;
    }
    .score-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .score-status { display: flex; align-items: center; gap: 6px; color: var(--score-color); font-size: 10px; font-weight: 600; }
    .score-status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 13%, transparent); }
    .score-panel.score-danger { --score-color: var(--red); }
    .score-panel.score-warning { --score-color: var(--amber); }
    .score-panel.score-healthy { --score-color: var(--green); }
    .score-panel.score-neutral { --score-color: var(--subtle); }
    .score-line { display: flex; align-items: flex-end; gap: 7px; }
    .score-line strong, .metric strong {
      margin: 0;
      color: #fff;
      font-size: 27px;
      line-height: 1;
      font-weight: 720;
    }
    .metric strong { margin-top: 13px; }
    .score-line span { margin: 0 0 2px; color: var(--subtle); font-size: 10px; }
    .score-track { height: 4px; border-radius: 999px; background: #25252a; overflow: hidden; }
    .score-track i {
      display: block;
      width: calc(var(--score, 0) * 1%);
      height: 100%;
      background: var(--score-color, var(--accent));
      border-radius: inherit;
    }
    .metric span { margin: 0; }
    .risk-breakdown { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
    .risk-stat { min-width: 0; padding: 9px 9px 8px; border: 1px solid var(--line-soft); border-radius: 5px; background: #111113; }
    .risk-stat span { display: flex; align-items: center; gap: 5px; color: var(--muted); font-size: 9px; }
    .risk-stat span::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: var(--risk-color); }
    .risk-stat strong { display: block; margin-top: 7px; color: var(--risk-color); font-size: 18px; line-height: 1; }
    .risk-stat.high-risk { --risk-color: var(--red); }
    .risk-stat.medium-risk { --risk-color: var(--amber); }
    .risk-stat.low-risk { --risk-color: var(--green); }
    #currentPanel { max-width: 1180px; padding-top: 24px; }
    .workspace-section { margin-top: 22px; }
    .snapshot-banner, .coverage-banner {
      min-height: 48px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
      padding: 8px 12px 8px 16px;
      border: 1px solid rgba(251, 191, 36, .35);
      border-radius: 7px;
      background: rgba(251, 191, 36, .07);
    }
    .snapshot-copy { display: flex; align-items: center; gap: 9px; color: #f6d572; font-size: 11px; }
    .snapshot-actions { display: flex; gap: 7px; }
    .coverage-banner { color: #f6d572; font-size: 11px; }
    .coverage-banner strong { color: #fff; }
    .coverage-banner.complete { border-color: rgba(52, 211, 153, .28); background: rgba(52, 211, 153, .06); color: var(--green); }
    .coverage-banner.failure { display: block; border-color: rgba(251, 113, 133, .34); background: rgba(251, 113, 133, .07); color: #fecdd3; }
    .coverage-failure-list { display: grid; gap: 5px; margin-top: 8px; color: var(--muted); }
    .coverage-failure-list span { min-width: 0; overflow-wrap: anywhere; }
    .coverage-failure-list code { color: #fff; font-family: "Cascadia Code", Consolas, monospace; font-size: 10px; }
    .section-bar {
      min-height: 48px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 2px;
    }
    .section-title { display: flex; align-items: baseline; gap: 10px; }
    .section-actions { display: flex; align-items: center; gap: 16px; }
    .compact-action { height: 32px; padding: 0 10px; font-size: 11px; }
    .section-bar h2, section .section-bar h2 {
      margin: 0;
      padding: 0;
      border: 0;
      background: none;
      font-size: 16px;
      font-weight: 650;
    }
    .section-count { color: var(--muted); font-size: 11px; }
    .results-toolbar {
      position: sticky;
      top: 0;
      z-index: 20;
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto auto;
      gap: 10px;
      align-items: center;
      padding: 10px 0;
      background: rgba(9, 9, 10, .96);
      backdrop-filter: blur(10px);
    }
    .result-search {
      min-width: 0;
      height: 36px;
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      align-items: center;
      gap: 8px;
      padding: 0 10px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #111113;
    }
    .result-search .icon { color: var(--subtle); }
    .result-search input {
      height: 34px;
      padding: 0;
      border: 0;
      background: transparent;
      box-shadow: none;
      font-size: 12px;
    }
    .result-search input:focus { border: 0; }
    .filter-controls {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .filter-group {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .filter-label {
      color: var(--subtle);
      font-size: 9px;
      font-weight: 650;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .severity-filters, .action-filters {
      display: flex;
      align-items: center;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface-2);
    }
    .severity-filter, .action-filter {
      height: 28px;
      padding: 0 9px;
      border: 0;
      border-radius: 4px;
      background: transparent;
      color: var(--muted);
      font-size: 10px;
      box-shadow: none;
    }
    .severity-filter:hover, .action-filter:hover { border: 0; background: #202024; color: var(--ink); }
    .severity-filter.active, .action-filter.active { background: #2a2435; color: #fff; box-shadow: inset 0 0 0 1px rgba(167, 139, 250, .18); }
    .filter-count { margin-left: 4px; color: var(--subtle); font-variant-numeric: tabular-nums; }
    .active .filter-count { color: #c4b5fd; }
    .toolbar-actions, .file-header-actions, .file-fold-actions { display: flex; align-items: center; gap: 7px; }
    .file-header-actions { justify-content: flex-end; }
    .file-fold-actions { gap: 4px; }
    .file-fold-button { width: 30px; height: 30px; padding: 0; }
    .results-meta {
      min-height: 34px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 11px;
    }
    .results-meta strong { color: var(--ink); font-weight: 600; }
    .result-stream { display: grid; gap: 24px; }
    .file-group {
      min-width: 0;
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }
    .file-group-header {
      min-height: 48px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: center;
    }
    .file-toggle {
      width: 100%;
      height: auto;
      min-width: 0;
      display: grid;
      grid-template-columns: 18px 24px minmax(0, 1fr);
      gap: 9px;
      align-items: center;
      padding: 8px 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--ink);
      text-align: left;
      white-space: normal;
      box-shadow: none;
    }
    .file-toggle:hover { border: 0; background: transparent; color: #fff; }
    .file-caret { color: var(--subtle); transition: transform .16s ease; }
    .file-group.collapsed .file-caret { transform: rotate(-90deg); }
    .file-icon { color: #c4b5fd; }
    .file-path { display: block; overflow: hidden; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .file-count { display: block; margin-top: 4px; color: var(--muted); font-family: inherit; font-size: 10px; font-weight: 400; }
    .file-select, .issue-select { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 10px; cursor: pointer; }
    .file-select input, .issue-select input { width: 14px; height: 14px; margin: 0; padding: 0; border: 0; border-radius: 3px; box-shadow: none; accent-color: var(--accent); cursor: pointer; }
    .file-select input:disabled, .issue-select input:disabled { opacity: .28; cursor: default; }
    .file-results { display: grid; gap: 10px; }
    .file-group.collapsed .file-results { display: none; }
    .result-item {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
      box-shadow: 0 1px 2px rgba(0, 0, 0, .16);
    }
    .result-item:hover { border-color: #36363d; }
    .result-item.expanded { border-color: #3a3544; }
    .result-item.selected { border-color: rgba(139, 92, 246, .55); box-shadow: 0 0 0 1px rgba(139, 92, 246, .10); }
    .result-item-header {
      min-height: 66px;
      display: grid;
      grid-template-columns: 22px 34px minmax(0, 1fr) 24px;
      gap: 10px;
      align-items: center;
      padding: 10px 14px;
    }
    .issue-select { width: 22px; justify-content: center; }
    .result-toggle {
      min-width: 0;
      width: 100%;
      height: auto;
      display: block;
      padding: 4px 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--ink);
      text-align: left;
      justify-content: flex-start;
      white-space: normal;
      box-shadow: none;
    }
    .result-toggle:hover { border: 0; background: transparent; }
    .result-title-line { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .result-title { overflow: hidden; font-size: 13px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .action-chip {
      flex: 0 0 auto;
      padding: 2px 6px;
      border: 1px solid var(--line-strong);
      border-radius: 4px;
      color: var(--muted);
      font-size: 9px;
      font-weight: 650;
    }
    .action-chip.add { border-color: rgba(52, 211, 153, .32); color: #6ee7b7; }
    .action-chip.delete { border-color: rgba(251, 113, 133, .32); color: #fda4af; }
    .action-chip.modify { border-color: rgba(96, 165, 250, .32); color: #93c5fd; }
    .issue-status { flex: 0 0 auto; color: var(--green); font-size: 10px; font-weight: 600; }
    .issue-status.muted { color: var(--subtle); }
    .result-rules { margin-top: 6px; color: var(--muted); font-size: 10px; overflow-wrap: anywhere; }
    .result-caret { color: var(--subtle); transition: transform .16s ease; }
    .result-item.expanded .result-caret { transform: rotate(180deg); }
    .result-content { padding: 0 16px 16px 80px; border-top: 1px solid var(--line-soft); }
    .finding-copy { padding: 14px 0 2px; }
    .copy-row { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 12px; padding: 5px 0; font-size: 11px; line-height: 1.6; }
    .copy-row > span { color: var(--subtle); }
    .copy-row p { margin: 0 0 5px; }
    .copy-row p:last-child { margin-bottom: 0; }
    .inline-block { margin-top: 12px; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
    .inline-block-header { min-height: 34px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 12px; border-bottom: 1px solid var(--line); background: var(--surface-2); color: var(--muted); font-size: 10px; }
    .inline-block pre { min-height: 0; max-height: 280px; padding: 13px 14px; flex: none; background: #090a0d; white-space: pre; }
    .code-view, .diff-view {
      overflow: auto;
      background: #090a0d;
      color: #d4d4d8;
      font-family: "Cascadia Code", Consolas, monospace;
      font-size: 11px;
      line-height: 1.65;
    }
    .code-view { max-height: 280px; padding: 12px 0; }
    .code-line, .diff-line { display: block; min-width: max-content; padding: 0 14px; white-space: pre; }
    .code-line.target { background: rgba(251, 191, 36, .08); color: #fde68a; box-shadow: inset 2px 0 0 var(--amber); }
    .diff-view { min-height: 0; }
    .inline-diff { max-height: 280px; padding: 10px 0; }
    .diff-line { min-width: 100%; padding: 1px 18px; }
    .diff-line.add { background: rgba(52, 211, 153, .11); color: #86efac; box-shadow: inset 3px 0 0 #22c55e; }
    .diff-line.remove { background: rgba(251, 113, 133, .11); color: #fda4af; box-shadow: inset 3px 0 0 #f43f5e; }
    .diff-line.file { background: rgba(96, 165, 250, .07); color: #93c5fd; }
    .diff-line.hunk { background: rgba(139, 92, 246, .07); color: #c4b5fd; }
    .diff-line.note { color: var(--subtle); font-style: italic; }
    .result-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
    .meta, .muted { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .pill {
      display: inline-block;
      min-width: 32px;
      padding: 2px 6px;
      border: 1px solid currentColor;
      border-radius: 999px;
      font-size: 10px;
      line-height: 16px;
      text-align: center;
    }
    .high { background: rgba(251, 113, 133, .10); color: #fb8fa2; }
    .medium { background: rgba(251, 191, 36, .10); color: #f8ca55; }
    .low { background: rgba(52, 211, 153, .10); color: #59dca9; }
    .debug { background: rgba(96, 165, 250, .10); color: #7eb7fb; }
    pre {
      flex: 1;
      margin: 0;
      min-height: 0;
      max-height: none;
      padding: 18px 20px;
      overflow: auto;
      background: var(--code);
      color: #d4d4d8;
      font-size: 11px;
      line-height: 1.6;
    }
    .empty { padding: 24px 16px; color: var(--muted); font-size: 12px; }
    .results-empty { min-height: 220px; display: grid; place-items: center; border: 1px dashed var(--line); border-radius: 8px; color: var(--muted); font-size: 12px; }
    .batch-bar {
      position: fixed;
      left: calc(230px + (100vw - 230px) / 2);
      bottom: 20px;
      z-index: 90;
      width: min(650px, calc(100vw - 290px));
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 10px 12px 10px 18px;
      border: 1px solid #4b4457;
      border-radius: 8px;
      background: #1b191f;
      box-shadow: 0 20px 54px rgba(0, 0, 0, .58), inset 0 1px 0 rgba(255, 255, 255, .04);
      transform: translateX(-50%);
    }
    .batch-copy { display: flex; align-items: baseline; gap: 9px; min-width: 0; }
    .batch-copy strong { font-size: 12px; }
    .batch-copy span { color: var(--muted); font-size: 10px; }
    .batch-actions { display: flex; gap: 8px; }
    .history-table {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .history-header {
      display: grid;
      grid-template-columns: minmax(260px, 1.5fr) 90px 1fr 92px;
      gap: 18px;
      align-items: center;
      min-height: 38px;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .history-list { max-height: none; overflow: auto; }
    .history-row {
      display: grid;
      grid-template-columns: minmax(260px, 1.5fr) 90px 1fr 92px;
      gap: 18px;
      align-items: center;
      min-height: 74px;
      padding: 12px 18px;
      border-bottom: 1px solid var(--line-soft);
    }
    .history-row:last-child { border-bottom: 0; }
    .history-row h3 { margin: 0 0 5px; font-size: 13px; }
    .history-score strong { font-size: 17px; }
    .history-score span { color: var(--muted); font-size: 10px; }
    .history-stats { color: var(--muted); font-size: 11px; line-height: 1.7; }
    .history-row button { justify-self: end; }
    .settings-stack { display: grid; gap: 22px; }
    .settings-surface {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .settings-surface-header {
      min-height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
    }
    .settings-surface-header h2 { margin: 0 0 4px; font-size: 14px; }
    .settings-surface-header p { margin: 0; color: var(--muted); font-size: 10px; }
    .mode-switch, .settings-actions { display: flex; align-items: center; gap: 8px; }
    .mode-switch { padding: 3px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
    .mode-switch button { height: 30px; padding: 0 11px; border: 0; background: transparent; box-shadow: none; color: var(--muted); }
    .mode-switch button.active { background: var(--surface-3); color: var(--ink); }
    .language-options { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
    .language-option {
      min-height: 86px;
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      padding: 16px;
      border-right: 1px solid var(--line-soft);
      cursor: pointer;
    }
    .language-option { border-bottom: 1px solid var(--line-soft); }
    .language-option:hover { background: var(--surface-2); }
    .language-option input { margin: 3px 0 0; accent-color: var(--accent); }
    .language-option strong { display: block; margin-bottom: 6px; font-size: 12px; }
    .language-option span { color: var(--muted); font-size: 10px; line-height: 1.5; }
    .language-option em { color: var(--green); font-style: normal; }
    .template-layout { display: grid; grid-template-columns: 230px minmax(0, 1fr); min-height: 190px; }
    .template-nav { max-height: 420px; padding: 10px; border-right: 1px solid var(--line); overflow-y: auto; }
    .template-nav button {
      width: 100%;
      height: 42px;
      justify-content: space-between;
      padding: 0 11px;
      border: 0;
      background: transparent;
      box-shadow: none;
      color: var(--muted);
    }
    .template-nav button.active { background: var(--accent-soft); color: var(--ink); }
    .template-editor { display: grid; gap: 12px; align-content: start; padding: 16px 18px; }
    .template-editor-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .template-source { padding: 3px 7px; border: 1px solid var(--line-strong); border-radius: 4px; color: var(--muted); font-size: 9px; }
    .template-support { color: var(--muted); font-size: 10px; }
    .template-support.ready { color: var(--green); }
    .template-input {
      width: 100%;
      min-height: 72px;
      resize: vertical;
      padding: 12px 13px;
      border: 1px solid var(--line);
      border-radius: 6px;
      outline: 0;
      background: var(--code);
      color: var(--ink);
      font-family: "Cascadia Code", Consolas, monospace;
      font-size: 11px;
      line-height: 1.6;
    }
    .template-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
    .template-footer { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .template-vars { color: var(--subtle); font-family: "Cascadia Code", Consolas, monospace; font-size: 9px; }
    .runtime-settings { margin-top: 22px; }
    .runtime-overview {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 22px;
      align-items: center;
      min-height: 92px;
      padding: 18px 20px;
      border: 1px solid var(--line);
      border-bottom: 0;
      border-radius: 8px 8px 0 0;
      background: var(--surface);
    }
    .runtime-machine { display: flex; align-items: center; gap: 14px; min-width: 0; }
    .machine-icon {
      width: 44px;
      height: 44px;
      display: grid;
      place-items: center;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface-2);
      color: var(--ink);
      font-size: 20px;
    }
    .runtime-machine h2 { margin: 0 0 5px; font-size: 16px; }
    .runtime-machine p { margin: 0; color: var(--muted); font-size: 11px; }
    .runtime-table {
      border: 1px solid var(--line);
      border-radius: 0 0 8px 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .runtime-header, .runtime-row {
      display: grid;
      grid-template-columns: minmax(180px, .8fr) 120px minmax(180px, .8fr) minmax(260px, 1.5fr);
      gap: 18px;
      align-items: center;
      padding: 0 20px;
    }
    .runtime-header {
      min-height: 38px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
    }
    button.runtime-row {
      width: 100%;
      height: auto;
      min-height: 68px;
      border: 0;
      border-bottom: 1px solid var(--line-soft);
      border-radius: 0;
      background: transparent;
      color: var(--ink);
      text-align: left;
    }
    button.runtime-row:hover { border-color: var(--line-soft); background: #171719; }
    button.runtime-row.selected { box-shadow: inset 2px 0 0 var(--accent); background: var(--accent-soft); }
    button.runtime-row:disabled { opacity: 1; cursor: default; }
    .runtime-row:last-child { border-bottom: 0; }
    .runtime-name { display: flex; align-items: center; gap: 10px; min-width: 0; }
    .runtime-logo {
      width: 30px;
      height: 30px;
      display: grid;
      place-items: center;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: var(--surface-3);
      font-size: 11px;
      font-weight: 750;
    }
    .runtime-name strong { font-size: 13px; }
    .runtime-badge {
      padding: 2px 5px;
      border: 1px solid var(--line-strong);
      border-radius: 4px;
      color: var(--muted);
      font-size: 9px;
    }
    .health { display: flex; align-items: center; gap: 8px; font-size: 11px; }
    .health.offline { color: var(--red); }
    .health.online { color: var(--green); }
    .runtime-value {
      overflow: hidden;
      color: var(--muted);
      font-family: "Cascadia Code", Consolas, monospace;
      font-size: 10px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .diagnostics-section {
      margin-top: 22px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .diagnostics-header {
      min-height: 72px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 14px 18px;
    }
    .diagnostics-copy h2 { margin: 0 0 5px; font-size: 14px; }
    .diagnostics-copy p { margin: 0; color: var(--muted); font-size: 11px; }
    .diagnostics-output {
      max-height: 320px;
      border-top: 1px solid var(--line);
      background: var(--code);
      flex: none;
    }
    .dialog-backdrop {
      position: fixed;
      inset: 0;
      z-index: 200;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(0, 0, 0, .66);
      backdrop-filter: blur(3px);
    }
    .dialog {
      width: min(940px, 100%);
      max-height: calc(100dvh - 48px);
      display: flex;
      flex-direction: column;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: var(--surface-2);
      box-shadow: 0 28px 80px rgba(0, 0, 0, .62);
      overflow: hidden;
    }
    .dialog-header {
      flex: 0 0 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 16px 0 20px;
      border-bottom: 1px solid var(--line);
    }
    .dialog-title h2 { margin: 0 0 3px; font-size: 15px; }
    .dialog-title p { margin: 0; color: var(--muted); font-size: 10px; }
    .dialog > .diff-view { min-height: 320px; max-height: calc(100dvh - 108px); flex: 1; padding: 14px 0; }
    .dialog.compact { width: min(520px, 100%); }
    .confirm-body { padding: 22px; color: var(--muted); font-size: 12px; line-height: 1.7; }
    .confirm-body strong { color: var(--ink); }
    .dialog-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      padding: 12px 16px;
      border-top: 1px solid var(--line);
      background: var(--surface);
    }
    .icon-only { width: 34px; height: 34px; padding: 0; }
    .toast-region {
      position: fixed;
      top: 82px;
      right: 24px;
      z-index: 100;
      width: min(380px, calc(100vw - 32px));
      pointer-events: none;
    }
    .toast {
      --toast-accent: var(--blue);
      min-height: 52px;
      display: grid;
      grid-template-columns: 8px 1fr;
      gap: 12px;
      align-items: center;
      padding: 11px 14px 11px 12px;
      border: 1px solid #3f3f46;
      border-radius: 8px;
      background: var(--surface-3);
      color: var(--ink);
      box-shadow: 0 18px 46px rgba(0, 0, 0, .52), inset 0 1px 0 rgba(255, 255, 255, .04);
      pointer-events: auto;
      animation: toast-in .18s ease-out;
    }
    .toast::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--toast-accent);
    }
    .toast.success { --toast-accent: var(--green); }
    .toast.warning { --toast-accent: var(--amber); }
    .toast.error { --toast-accent: var(--red); }
    .warning-dot { background: var(--amber); box-shadow: 0 0 0 3px rgba(251, 191, 36, .12); }
    .toast.leaving { animation: toast-out .16s ease-in forwards; }
    .toast-message { min-width: 0; font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
    .danger { color: var(--red); }
    .warning { color: var(--amber); }
    .success { color: var(--green); }
    .hidden { display: none !important; }

    @keyframes toast-in {
      from { opacity: 0; transform: translateY(-8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes toast-out { to { opacity: 0; transform: translateY(-6px); } }
    @keyframes progress-pulse {
      from { transform: translateX(-110%); }
      to { transform: translateX(360%); }
    }

    @media (max-width: 1320px) {
      .results-toolbar {
        grid-template-columns: minmax(0, 1fr) auto;
        grid-template-areas: "search action" "filters filters";
      }
      .result-search { grid-area: search; }
      .filter-controls { grid-area: filters; justify-self: start; flex-wrap: wrap; }
      .toolbar-actions { grid-area: action; }
    }
    @media (max-width: 1080px) {
      .app-shell { grid-template-columns: 190px minmax(0, 1fr); }
      main { grid-column: 2; }
      .analysis-commandbar { grid-template-columns: minmax(260px, 1fr) 160px auto; }
      .analysis-option { grid-template-columns: auto minmax(130px, 1fr); }
      .analysis-option-summary { grid-column: 1 / -1; }
      .view-panel { padding-left: 28px; padding-right: 28px; }
      .batch-bar { left: calc(190px + (100vw - 190px) / 2); width: min(650px, calc(100vw - 240px)); }
    }
    @media (max-width: 820px) {
      body { overflow: auto; }
      .app-shell { height: auto; min-height: 100dvh; display: block; overflow: visible; }
      .sidebar {
        min-height: 0;
        height: auto;
        display: grid;
        grid-template-columns: auto 1fr;
        grid-template-rows: 64px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .brand { border-bottom: 0; }
      .sidebar-footer { display: none; }
      .side-nav { display: flex; justify-content: flex-end; align-items: center; padding: 0 14px; }
      .nav-item { width: auto; grid-template-columns: 18px auto; }
      main { overflow: visible; }
      .view-panel { padding: 22px 20px 32px; }
      .analysis-commandbar { grid-template-columns: minmax(0, 1fr) 160px auto; }
      .analysis-options { grid-template-columns: 1fr; }
      .analysis-option { grid-template-columns: auto minmax(150px, 220px) minmax(0, 1fr); }
      .analysis-option:not(:last-child) { border-right: 0; border-bottom: 1px solid var(--line); }
      .analysis-option-summary { grid-column: auto; }
      .scan-step { padding: 0 9px; }
      .summary-grid { grid-template-columns: minmax(160px, 1.2fr) repeat(3, minmax(92px, 1fr)); }
      .metric:nth-child(4) { border-right: 0; }
      .risk-panel { grid-column: 1 / -1; border-top: 1px solid var(--line); }
      .results-toolbar {
        grid-template-columns: minmax(0, 1fr) auto;
        grid-template-areas: "search action" "filters filters";
      }
      .result-search { grid-area: search; }
      .filter-controls { grid-area: filters; justify-self: start; flex-wrap: wrap; }
      .toolbar-actions { grid-area: action; }
      .batch-bar { left: 50%; bottom: 16px; width: calc(100vw - 32px); }
      .history-header { display: none; }
      .history-row { grid-template-columns: 1fr auto; }
      .history-score, .history-stats { display: none; }
      .toast-region { top: auto; right: 16px; bottom: 16px; }
      .runtime-header { display: none; }
      .runtime-row { grid-template-columns: minmax(150px, 1fr) auto; }
      .runtime-row .runtime-value { display: none; }
      .language-options { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .language-option:nth-child(2) { border-right: 0; }
      .language-option:nth-child(-n+2) { border-bottom: 1px solid var(--line-soft); }
      .template-layout { grid-template-columns: 1fr; }
      .preset-library { grid-template-columns: auto minmax(150px, 1fr) auto auto auto; }
      .template-nav { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-right: 0; border-bottom: 1px solid var(--line); }
      .section-actions { gap: 8px; }
      .snapshot-banner { align-items: flex-start; flex-direction: column; }
    }
    @media (max-width: 560px) {
      .sidebar { grid-template-columns: 1fr; grid-template-rows: 56px auto; }
      .brand { height: 56px; padding: 0 16px; }
      .side-nav { justify-content: stretch; padding: 8px; border-top: 1px solid var(--line-soft); }
      .nav-item { flex: 1; justify-items: center; grid-template-columns: 18px auto; }
      .analysis-commandbar { grid-template-columns: 1fr; }
      .repo-control { grid-template-columns: 1fr auto; }
      #scanButton { width: 100%; }
      .analysis-option { grid-template-columns: 1fr; gap: 7px; }
      .analysis-option-summary { grid-column: auto; }
      .scan-progress-header { align-items: flex-start; }
      .scan-progress-message { white-space: normal; }
      .scan-steps { grid-template-columns: repeat(5, minmax(0, 1fr)); }
      .scan-step { justify-content: center; gap: 5px; padding: 0 2px; font-size: 9px; white-space: nowrap; }
      .preset-library { grid-template-columns: 1fr auto auto auto; }
      .preset-library-label { grid-column: 1 / -1; }
      .summary-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }
      .score-panel { grid-column: 1 / -1; min-height: 104px; border-right: 0; border-bottom: 1px solid var(--line); }
      .metric { grid-column: span 2; min-height: 84px; padding: 14px 12px; }
      .metric strong { margin-top: 10px; font-size: 23px; }
      .risk-panel { grid-column: 1 / -1; min-height: 104px; }
      .compact-action span { display: none; }
      .results-toolbar {
        grid-template-columns: minmax(0, 1fr);
        grid-template-areas: "search" "action" "filters";
      }
      .filter-controls { width: 100%; display: grid; grid-template-columns: 1fr; }
      .filter-group { min-width: 0; }
      .filter-group > div { min-width: 0; flex: 1; }
      .severity-filter, .action-filter { flex: 1; padding: 0 5px; }
      .toolbar-actions { width: 100%; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .toolbar-actions .compact-action { width: 100%; }
      .toolbar-actions .compact-action span { display: inline; }
      .file-group-header { grid-template-columns: minmax(0, 1fr); gap: 4px; }
      .file-header-actions { justify-content: flex-start; margin-left: 51px; }
      .result-item-header { grid-template-columns: 20px 32px minmax(0, 1fr) 20px; gap: 8px; padding: 10px; }
      .result-title-line { align-items: flex-start; flex-wrap: wrap; }
      .result-title { white-space: normal; }
      .result-content { padding: 0 12px 14px; }
      .copy-row { grid-template-columns: minmax(0, 1fr); gap: 3px; }
      .batch-copy span { display: none; }
      .batch-bar { min-height: 54px; gap: 10px; padding-left: 14px; }
      .settings-surface-header, .template-footer { align-items: flex-start; flex-direction: column; }
      .settings-actions { width: 100%; }
      .settings-actions button { flex: 1; }
      .template-nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (prefers-reduced-motion: reduce) {
      .toast, .toast.leaving { animation: none; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark" aria-hidden="true">LP</span><strong>LogPilot</strong></div>
      <nav class="side-nav" aria-label="主要导航">
        <button class="nav-item active" id="currentTab" type="button"><span class="nav-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/></svg></span><span>仓库分析</span></button>
        <button class="nav-item" id="historyTab" type="button"><span class="nav-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg></span><span>历史记录</span></button>
        <button class="nav-item" id="settingsTab" type="button"><span class="nav-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.09a2 2 0 0 1-1-1.74v-.51a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg></span><span>设置</span></button>
      </nav>
      <div class="sidebar-footer"><span class="local-dot"></span><span>本地模式</span></div>
    </aside>
    <div class="toast-region" id="toastRegion" aria-live="polite" aria-atomic="true"></div>
    <main>
      <div class="view-panel" id="currentPanel">
        <section class="analysis-launch" aria-label="分析配置">
          <div class="analysis-commandbar">
            <div class="repo-control">
              <input id="repoPath" type="text" spellcheck="false" aria-label="仓库路径" placeholder="D:\\GitHub\\log-pilot">
              <button class="secondary" id="browseButton" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/></svg><span>选择仓库</span></button>
            </div>
            <label class="runtime-control" title="选择执行日志分析的本机运行时">
              <span class="state-dot offline" id="runtimeDot"></span>
              <select id="runtimeSelect" aria-label="分析运行时"><option value="">检测运行时...</option></select>
            </label>
            <button id="scanButton" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 3 14 9-14 9z"/></svg><span>开始分析</span></button>
          </div>
          <div class="analysis-options">
            <div class="analysis-option">
              <span class="analysis-option-label">分析语言</span>
              <div class="preset-control"><select id="analysisLanguagePreset" aria-label="分析语言方案"></select><button class="secondary preset-add" id="addLanguagePreset" type="button" title="新增语言方案"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg></button></div>
              <span class="analysis-option-summary" id="analysisLanguageSummary"></span>
            </div>
            <div class="analysis-option">
              <span class="analysis-option-label">日志模板</span>
              <div class="preset-control"><select id="analysisTemplatePreset" aria-label="日志模板方案"></select><button class="secondary preset-add" id="addTemplatePreset" type="button" title="新增模板方案"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg></button></div>
              <span class="analysis-option-summary" id="analysisTemplateSummary"></span>
            </div>
            <div class="analysis-option">
              <span class="analysis-option-label">AI 深度</span>
              <select id="analysisDepth" aria-label="AI 分析深度">
                <option value="quick">快速</option>
                <option value="standard">标准</option>
                <option value="deep">深度</option>
              </select>
              <span class="analysis-option-summary" id="analysisDepthSummary"></span>
            </div>
          </div>
        </section>
        <section class="scan-progress hidden" id="scanProgress" aria-live="polite">
          <div class="scan-progress-header">
            <div class="scan-progress-copy">
              <div class="scan-progress-title"><strong id="scanProgressTitle">正在准备分析</strong><span id="scanProgressPercent">0%</span></div>
              <div class="scan-progress-message" id="scanProgressMessage">正在创建后台分析任务</div>
            </div>
            <button class="secondary compact-action" id="cancelScanButton" type="button">停止分析</button>
          </div>
          <div class="scan-progress-track indeterminate" id="scanProgressTrack"><i></i></div>
          <div class="scan-steps" id="scanSteps">
            <span class="scan-step" data-scan-step="discovering">发现文件</span>
            <span class="scan-step" data-scan-step="parsing">解析源码</span>
            <span class="scan-step" data-scan-step="rules">规则检查</span>
            <span class="scan-step" data-scan-step="runtime">运行时分析</span>
            <span class="scan-step" data-scan-step="reporting">生成报告</span>
          </div>
        </section>
        <div class="incremental-note hidden" id="incrementalNote">分析仍在进行，当前展示已完成阶段的结果；完成后才可采纳修改。</div>
        <div class="snapshot-banner hidden" id="snapshotBanner">
          <div class="snapshot-copy"><span class="state-dot warning-dot"></span><span>源码已修改，当前结果为分析快照</span></div>
          <div class="snapshot-actions"><button class="secondary" id="rollbackButton" type="button">撤销上次采纳</button><button class="secondary" id="rescanButton" type="button">重新分析</button></div>
        </div>
        <div class="coverage-banner hidden" id="coverageBanner"></div>
        <section class="summary-section"><div class="summary-grid" id="metrics"></div></section>
        <section class="workspace-section">
          <div class="results-toolbar">
            <label class="result-search"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg><input id="resultSearch" type="search" aria-label="搜索分析结果" placeholder="搜索文件、问题或规则"></label>
            <div class="filter-controls">
              <div class="filter-group"><span class="filter-label">风险</span><div class="severity-filters" id="severityFilters" role="group" aria-label="风险级别筛选">
                <button class="severity-filter active" type="button" data-severity="all">全部</button>
                <button class="severity-filter" type="button" data-severity="high">高</button>
                <button class="severity-filter" type="button" data-severity="medium">中</button>
                <button class="severity-filter" type="button" data-severity="low">低</button>
              </div></div>
              <div class="filter-group"><span class="filter-label">动作</span><div class="action-filters" id="actionFilters" role="group" aria-label="建议动作筛选">
                <button class="action-filter active" type="button" data-action="all">全部<span class="filter-count" data-action-count="all">0</span></button>
                <button class="action-filter" type="button" data-action="add">增加<span class="filter-count" data-action-count="add">0</span></button>
                <button class="action-filter" type="button" data-action="delete">删除<span class="filter-count" data-action-count="delete">0</span></button>
                <button class="action-filter" type="button" data-action="modify">修改<span class="filter-count" data-action-count="modify">0</span></button>
              </div></div>
            </div>
            <div class="toolbar-actions">
              <button class="secondary compact-action" id="expandAllButton" type="button" title="展开当前筛选结果" disabled><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 5 5 5-5"/><path d="m7 13 5 5 5-5"/></svg><span>全部展开</span></button>
              <button class="secondary compact-action" id="collapseAllButton" type="button" title="折叠当前筛选结果" disabled><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 17 5-5 5 5"/><path d="m7 11 5-5 5 5"/></svg><span>全部折叠</span></button>
              <button class="secondary compact-action" id="fullPatchButton" type="button" title="查看本次分析生成的全部安全修改"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><polyline points="14 2 14 8 20 8"/><path d="m9 15 2 2 4-4"/></svg><span>完整修改</span></button>
            </div>
          </div>
          <div class="results-meta"><span id="resultsSummary">等待分析结果</span><span>按文件分组</span></div>
          <div class="result-stream" id="resultStream"></div>
        </section>
      </div>
      <section id="historyPanel" class="view-panel hidden">
        <div class="section-bar"><div class="section-title"><h2>分析历史</h2><span class="section-count">按时间倒序排列</span></div></div>
        <div class="history-table">
          <div class="history-header"><span>仓库与时间</span><span>评分</span><span>扫描结果</span><span>操作</span></div>
          <div class="history-list" id="historyList"></div>
        </div>
      </section>
      <section id="settingsPanel" class="view-panel hidden">
        <div class="section-bar"><div class="section-title"><h2>仓库设置</h2><span class="section-count" id="settingsRepository">仓库配置</span></div><button id="saveSettingsButton" type="button">保存设置</button></div>
        <div class="settings-stack">
          <section class="settings-surface">
            <header class="settings-surface-header">
              <div><h2>分析语言</h2><p>自动识别仓库语言，或固定本次分析范围</p></div>
              <div class="mode-switch" id="languageMode" role="group" aria-label="语言识别模式"><button type="button" data-language-mode="auto">自动识别</button><button type="button" data-language-mode="custom">手动选择</button></div>
            </header>
            <div class="preset-library"><span class="preset-library-label">语言方案</span><select id="settingsLanguagePreset" aria-label="已保存的语言方案"></select><button class="secondary" id="loadLanguagePreset" type="button">载入</button><button class="secondary" id="saveLanguagePreset" type="button">保存当前</button><button class="secondary icon-only" id="deleteLanguagePreset" type="button" title="删除语言方案"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/></svg></button></div>
            <div class="language-options" id="languageOptions"></div>
          </section>
          <section class="settings-surface">
            <header class="settings-surface-header">
              <div><h2>日志模板</h2><p>异常日志优先沿用仓库中的实现风格</p></div>
              <button class="secondary" id="profileRepositoryButton" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3v18h18"/><path d="m7 16 4-5 4 3 4-7"/></svg><span>扫描并推荐</span></button>
            </header>
            <div class="preset-library"><span class="preset-library-label">模板方案</span><select id="settingsTemplatePreset" aria-label="已保存的模板方案"></select><button class="secondary" id="loadTemplatePreset" type="button">载入</button><button class="secondary" id="saveTemplatePreset" type="button">保存当前</button><button class="secondary icon-only" id="deleteTemplatePreset" type="button" title="删除模板方案"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/></svg></button></div>
            <div class="template-layout">
              <nav class="template-nav" id="templateLanguageNav" aria-label="模板语言"></nav>
              <div class="template-editor">
                <div class="template-editor-meta"><span class="template-source" id="templateSource">内置模板</span><span class="template-support" id="templateSupport"></span></div>
                <textarea class="template-input" id="templateInput" spellcheck="false" aria-label="日志模板"></textarea>
                <div class="template-footer">
                  <span class="template-vars">{event} {exception} {function} {logger} {indent}</span>
                  <div class="settings-actions"><button class="secondary" id="useRecommendedTemplate" type="button">采用推荐</button><button class="secondary" id="useBuiltinTemplate" type="button">使用内置</button></div>
                </div>
              </div>
            </div>
          </section>
        </div>
        <div class="section-bar runtime-settings"><div class="section-title"><h2>运行时</h2><span class="section-count">本机分析执行环境</span></div></div>
        <div class="runtime-overview">
          <div class="runtime-machine">
            <span class="machine-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/></svg></span>
            <div><h2>本机运行环境</h2><p id="runtimeSummary">正在检测可用的命令行运行时</p></div>
          </div>
          <button class="secondary" id="refreshRuntimesButton" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/></svg><span>刷新状态</span></button>
        </div>
        <div class="runtime-table">
          <div class="runtime-header"><span>运行时</span><span>健康状态</span><span>版本</span><span>可执行文件</span></div>
          <div id="runtimeList"></div>
        </div>
        <section class="diagnostics-section">
          <div class="diagnostics-header">
            <div class="diagnostics-copy"><h2>分析诊断</h2><p id="diagnosticsSummary">用于排查运行时分析异常</p></div>
            <button class="secondary" id="diagnosticsToggle" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg><span>查看诊断</span></button>
          </div>
          <pre class="diagnostics-output hidden" id="diagnosticsPre"></pre>
        </section>
      </section>
    </main>
  </div>
  <div class="batch-bar hidden" id="batchBar" role="region" aria-label="批量采纳操作">
    <div class="batch-copy"><strong id="batchSelectionCount">已选择 0 项</strong><span id="batchSelectionFiles"></span></div>
    <div class="batch-actions"><button class="secondary" id="clearSelectionButton" type="button">清空</button><button id="batchApplyButton" type="button">批量采纳</button></div>
  </div>
  <div class="dialog-backdrop hidden" id="fullPatchDialog" role="dialog" aria-modal="true" aria-labelledby="fullPatchTitle">
    <section class="dialog">
      <header class="dialog-header">
        <div class="dialog-title"><h2 id="fullPatchTitle">完整修改</h2><p>本次分析生成的安全修改，可在结果流中勾选后采纳</p></div>
        <button class="secondary icon-only" id="closePatchDialog" type="button" title="关闭"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      </header>
      <div class="diff-view" id="fullPatchPre"><span class="diff-line note">暂无修改内容</span></div>
    </section>
  </div>
  <div class="dialog-backdrop hidden" id="applyDialog" role="dialog" aria-modal="true" aria-labelledby="applyDialogTitle">
    <section class="dialog compact">
      <header class="dialog-header">
        <div class="dialog-title"><h2 id="applyDialogTitle">确认采纳修改</h2><p>写入前会检查源码是否仍与分析快照一致</p></div>
        <button class="secondary icon-only" id="closeApplyDialog" type="button" title="关闭"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      </header>
      <div class="confirm-body" id="applySummary"></div>
      <div class="dialog-actions"><button class="secondary" id="cancelApplyButton" type="button">取消</button><button id="confirmApplyButton" type="button">确认采纳</button></div>
    </section>
  </div>
  <div class="dialog-backdrop hidden" id="presetDialog" role="dialog" aria-modal="true" aria-labelledby="presetDialogTitle">
    <section class="dialog compact">
      <header class="dialog-header">
        <div class="dialog-title"><h2 id="presetDialogTitle">新增方案</h2><p id="presetDialogDescription">保存当前配置，分析前可直接选择</p></div>
        <button class="secondary icon-only" id="closePresetDialog" type="button" title="关闭"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      </header>
      <div class="preset-dialog-body"><label for="presetNameInput">方案名称</label><input id="presetNameInput" type="text" maxlength="40" autocomplete="off" placeholder="例如：Python 后端"></div>
      <div class="dialog-actions"><button class="secondary" id="cancelPresetButton" type="button">取消</button><button id="confirmPresetButton" type="button">保存方案</button></div>
    </section>
  </div>
  <script>
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[char]));
    const state = {
      path: "",
      scanning: false,
      scanJobId: "",
      scanReportVersion: -1,
      scanCancelRequested: false,
      browsing: false,
      history: [],
      report: null,
      reportActionable: false,
      patch: "",
      activeRunId: "",
      selectedGroups: new Set(),
      expandedGroups: new Set(),
      collapsedFiles: new Set(),
      searchQuery: "",
      severityFilter: "all",
      actionFilter: "all",
      appliedIssueIds: new Set(),
      applyRecords: [],
      latestApplyId: "",
      canRollback: false,
      pendingIssueIds: [],
      applying: false,
      diagnosticsOpen: false,
      runtimes: [],
      selectedRuntime: "",
      repositorySettings: {
        language_mode: "auto",
        selected_languages: [],
        templates: {},
        language_presets: [],
        template_presets: [],
        active_language_preset: "auto",
        active_template_preset: "auto",
        analysis_depth: "standard"
      },
      languageProfile: { detected_languages: [], template_recommendations: {} },
      settingsLanguages: [],
      templateLanguage: "python",
      settingsBusy: false,
      presetDialogType: "",
      activeView: "current"
    };
    const repoPath = document.querySelector("#repoPath");
    const browseButton = document.querySelector("#browseButton");
    const scanButton = document.querySelector("#scanButton");
    const scanProgress = document.querySelector("#scanProgress");
    const scanProgressTitle = document.querySelector("#scanProgressTitle");
    const scanProgressPercent = document.querySelector("#scanProgressPercent");
    const scanProgressMessage = document.querySelector("#scanProgressMessage");
    const scanProgressTrack = document.querySelector("#scanProgressTrack");
    const scanSteps = document.querySelector("#scanSteps");
    const cancelScanButton = document.querySelector("#cancelScanButton");
    const incrementalNote = document.querySelector("#incrementalNote");
    const toastRegion = document.querySelector("#toastRegion");
    const currentTab = document.querySelector("#currentTab");
    const historyTab = document.querySelector("#historyTab");
    const settingsTab = document.querySelector("#settingsTab");
    const currentPanel = document.querySelector("#currentPanel");
    const historyPanel = document.querySelector("#historyPanel");
    const settingsPanel = document.querySelector("#settingsPanel");
    const runtimeSelect = document.querySelector("#runtimeSelect");
    const runtimeDot = document.querySelector("#runtimeDot");
    const refreshRuntimesButton = document.querySelector("#refreshRuntimesButton");
    const resultSearch = document.querySelector("#resultSearch");
    const severityFilters = document.querySelector("#severityFilters");
    const actionFilters = document.querySelector("#actionFilters");
    const resultStream = document.querySelector("#resultStream");
    const resultsSummary = document.querySelector("#resultsSummary");
    const expandAllButton = document.querySelector("#expandAllButton");
    const collapseAllButton = document.querySelector("#collapseAllButton");
    const fullPatchButton = document.querySelector("#fullPatchButton");
    const fullPatchDialog = document.querySelector("#fullPatchDialog");
    const fullPatchPre = document.querySelector("#fullPatchPre");
    const closePatchDialog = document.querySelector("#closePatchDialog");
    const diagnosticsToggle = document.querySelector("#diagnosticsToggle");
    const diagnosticsPre = document.querySelector("#diagnosticsPre");
    const batchBar = document.querySelector("#batchBar");
    const batchSelectionCount = document.querySelector("#batchSelectionCount");
    const batchSelectionFiles = document.querySelector("#batchSelectionFiles");
    const clearSelectionButton = document.querySelector("#clearSelectionButton");
    const batchApplyButton = document.querySelector("#batchApplyButton");
    const snapshotBanner = document.querySelector("#snapshotBanner");
    const coverageBanner = document.querySelector("#coverageBanner");
    const rollbackButton = document.querySelector("#rollbackButton");
    const rescanButton = document.querySelector("#rescanButton");
    const applyDialog = document.querySelector("#applyDialog");
    const applySummary = document.querySelector("#applySummary");
    const closeApplyDialog = document.querySelector("#closeApplyDialog");
    const cancelApplyButton = document.querySelector("#cancelApplyButton");
    const confirmApplyButton = document.querySelector("#confirmApplyButton");
    const saveSettingsButton = document.querySelector("#saveSettingsButton");
    const profileRepositoryButton = document.querySelector("#profileRepositoryButton");
    const languageMode = document.querySelector("#languageMode");
    const languageOptions = document.querySelector("#languageOptions");
    const templateLanguageNav = document.querySelector("#templateLanguageNav");
    const templateInput = document.querySelector("#templateInput");
    const templateSource = document.querySelector("#templateSource");
    const templateSupport = document.querySelector("#templateSupport");
    const useRecommendedTemplate = document.querySelector("#useRecommendedTemplate");
    const useBuiltinTemplate = document.querySelector("#useBuiltinTemplate");
    const analysisLanguagePreset = document.querySelector("#analysisLanguagePreset");
    const analysisTemplatePreset = document.querySelector("#analysisTemplatePreset");
    const analysisDepth = document.querySelector("#analysisDepth");
    const analysisLanguageSummary = document.querySelector("#analysisLanguageSummary");
    const analysisTemplateSummary = document.querySelector("#analysisTemplateSummary");
    const analysisDepthSummary = document.querySelector("#analysisDepthSummary");
    const addLanguagePreset = document.querySelector("#addLanguagePreset");
    const addTemplatePreset = document.querySelector("#addTemplatePreset");
    const settingsLanguagePreset = document.querySelector("#settingsLanguagePreset");
    const settingsTemplatePreset = document.querySelector("#settingsTemplatePreset");
    const loadLanguagePreset = document.querySelector("#loadLanguagePreset");
    const loadTemplatePreset = document.querySelector("#loadTemplatePreset");
    const saveLanguagePreset = document.querySelector("#saveLanguagePreset");
    const saveTemplatePreset = document.querySelector("#saveTemplatePreset");
    const deleteLanguagePreset = document.querySelector("#deleteLanguagePreset");
    const deleteTemplatePreset = document.querySelector("#deleteTemplatePreset");
    const presetDialog = document.querySelector("#presetDialog");
    const presetDialogTitle = document.querySelector("#presetDialogTitle");
    const presetDialogDescription = document.querySelector("#presetDialogDescription");
    const presetNameInput = document.querySelector("#presetNameInput");
    const closePresetDialog = document.querySelector("#closePresetDialog");
    const cancelPresetButton = document.querySelector("#cancelPresetButton");
    const confirmPresetButton = document.querySelector("#confirmPresetButton");

    scanButton.addEventListener("click", () => startScan(repoPath.value));
    cancelScanButton.addEventListener("click", cancelScan);
    browseButton.addEventListener("click", browseRepository);
    repoPath.addEventListener("keydown", event => {
      if (event.key === "Enter") startScan(repoPath.value);
    });
    repoPath.addEventListener("change", () => activateRepository(repoPath.value));
    currentTab.addEventListener("click", () => showTab("current"));
    historyTab.addEventListener("click", () => showTab("history"));
    settingsTab.addEventListener("click", () => {
      showTab("settings");
      loadRepositorySettings(repoPath.value, true);
    });
    refreshRuntimesButton.addEventListener("click", () => loadRuntimes(true));
    runtimeSelect.addEventListener("change", () => {
      state.selectedRuntime = runtimeSelect.value;
      window.localStorage.setItem("logpilot.runtime", state.selectedRuntime);
      renderRuntimes();
    });
    analysisLanguagePreset.addEventListener("change", () => selectAnalysisPreset("language", analysisLanguagePreset.value));
    analysisTemplatePreset.addEventListener("change", () => selectAnalysisPreset("template", analysisTemplatePreset.value));
    analysisDepth.addEventListener("change", async () => {
      const previous = state.repositorySettings.analysis_depth || "standard";
      state.repositorySettings.analysis_depth = analysisDepth.value;
      renderAnalysisDepth();
      if (await persistRepositorySettings(true)) showToast(`AI 分析深度已设为${analysisDepth.options[analysisDepth.selectedIndex].text}`, "success");
      else {
        state.repositorySettings.analysis_depth = previous;
        renderAnalysisDepth();
      }
    });
    addLanguagePreset.addEventListener("click", () => openPresetDialog("language"));
    addTemplatePreset.addEventListener("click", () => openPresetDialog("template"));
    loadLanguagePreset.addEventListener("click", () => loadSavedPreset("language", settingsLanguagePreset.value));
    loadTemplatePreset.addEventListener("click", () => loadSavedPreset("template", settingsTemplatePreset.value));
    saveLanguagePreset.addEventListener("click", () => openPresetDialog("language"));
    saveTemplatePreset.addEventListener("click", () => openPresetDialog("template"));
    deleteLanguagePreset.addEventListener("click", () => deleteSavedPreset("language", settingsLanguagePreset.value));
    deleteTemplatePreset.addEventListener("click", () => deleteSavedPreset("template", settingsTemplatePreset.value));
    settingsLanguagePreset.addEventListener("change", updatePresetLibraryActions);
    settingsTemplatePreset.addEventListener("change", updatePresetLibraryActions);
    closePresetDialog.addEventListener("click", closePresetEditor);
    cancelPresetButton.addEventListener("click", closePresetEditor);
    confirmPresetButton.addEventListener("click", createPreset);
    presetNameInput.addEventListener("keydown", event => {
      if (event.key === "Enter") createPreset();
    });
    presetDialog.addEventListener("click", event => {
      if (event.target === presetDialog) closePresetEditor();
    });
    resultSearch.addEventListener("input", () => {
      state.searchQuery = resultSearch.value;
      renderResultStream();
    });
    severityFilters.addEventListener("click", event => {
      const button = event.target.closest("button[data-severity]");
      if (!button) return;
      state.severityFilter = button.dataset.severity;
      renderResultStream();
    });
    actionFilters.addEventListener("click", event => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      state.actionFilter = button.dataset.action;
      renderResultStream();
    });
    resultStream.addEventListener("click", handleResultStreamClick);
    resultStream.addEventListener("change", handleResultStreamChange);
    expandAllButton.addEventListener("click", () => setVisibleGroupsExpanded(true));
    collapseAllButton.addEventListener("click", () => setVisibleGroupsExpanded(false));
    fullPatchButton.addEventListener("click", openFullPatch);
    closePatchDialog.addEventListener("click", closeFullPatch);
    fullPatchDialog.addEventListener("click", event => {
      if (event.target === fullPatchDialog) closeFullPatch();
    });
    diagnosticsToggle.addEventListener("click", toggleDiagnostics);
    batchApplyButton.addEventListener("click", () => {
      const groups = issueGroups().filter(group => state.selectedGroups.has(group.id));
      openApplyDialog(groups.flatMap(group => patchIssueIds(group)));
    });
    clearSelectionButton.addEventListener("click", () => {
      state.selectedGroups = new Set();
      renderResultStream();
    });
    rollbackButton.addEventListener("click", rollbackLatestApply);
    rescanButton.addEventListener("click", () => startScan(repoPath.value));
    closeApplyDialog.addEventListener("click", closeApplyConfirmation);
    cancelApplyButton.addEventListener("click", closeApplyConfirmation);
    confirmApplyButton.addEventListener("click", submitApply);
    saveSettingsButton.addEventListener("click", saveRepositorySettings);
    profileRepositoryButton.addEventListener("click", profileRepository);
    languageMode.addEventListener("click", event => {
      const button = event.target.closest("button[data-language-mode]");
      if (!button) return;
      state.repositorySettings.language_mode = button.dataset.languageMode;
      state.repositorySettings.active_language_preset = "auto";
      if (button.dataset.languageMode === "custom" && !state.repositorySettings.selected_languages.length) {
        const detected = state.languageProfile.detected_languages.filter(item => item.recommended).map(item => item.id);
        state.repositorySettings.selected_languages = detected.length ? detected : ["python"];
      }
      renderRepositorySettings();
    });
    languageOptions.addEventListener("change", event => {
      const input = event.target.closest("input[data-language-id]");
      if (!input) return;
      const selected = new Set(state.repositorySettings.selected_languages);
      if (input.checked) selected.add(input.dataset.languageId);
      else selected.delete(input.dataset.languageId);
      state.repositorySettings.selected_languages = [...selected];
      state.repositorySettings.active_language_preset = "auto";
      renderRepositorySettings();
    });
    templateLanguageNav.addEventListener("click", event => {
      const button = event.target.closest("button[data-template-language]");
      if (!button) return;
      state.templateLanguage = button.dataset.templateLanguage;
      renderRepositorySettings();
    });
    templateInput.addEventListener("input", () => {
      state.repositorySettings.templates[state.templateLanguage] = templateInput.value;
      state.repositorySettings.active_template_preset = "auto";
      renderTemplateMeta();
    });
    useRecommendedTemplate.addEventListener("click", () => {
      const recommendation = templateRecommendation(state.templateLanguage);
      state.repositorySettings.templates[state.templateLanguage] = recommendation.template || languageDefinition(state.templateLanguage)?.builtin_template || "";
      state.repositorySettings.active_template_preset = "auto";
      renderRepositorySettings();
    });
    useBuiltinTemplate.addEventListener("click", () => {
      state.repositorySettings.templates[state.templateLanguage] = languageDefinition(state.templateLanguage)?.builtin_template || "";
      state.repositorySettings.active_template_preset = "auto";
      renderRepositorySettings();
    });
    applyDialog.addEventListener("click", event => {
      if (event.target === applyDialog) closeApplyConfirmation();
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      if (!applyDialog.classList.contains("hidden")) closeApplyConfirmation();
      else if (!fullPatchDialog.classList.contains("hidden")) closeFullPatch();
      else if (!presetDialog.classList.contains("hidden")) closePresetEditor();
    });

    async function init() {
      try {
        const [stateResponse] = await Promise.all([fetch("/api/state"), loadRuntimes(false)]);
        const payload = await stateResponse.json();
        if (!stateResponse.ok || payload.error) throw new Error(payload.error || "状态读取失败");
        const activeScan = payload.active_scan || null;
        state.path = activeScan?.repository || payload.repository || "";
        state.history = payload.history || [];
        state.activeRunId = state.history[0]?.run_id || "";
        repoPath.value = state.path;
        updateRepositoryIdentity(state.path);
        await loadRepositorySettings(state.path, true);
        renderHistory(state.history);
        if (activeScan) {
          renderEmpty();
          await resumeScan(activeScan);
        } else if (payload.has_report) await loadReport();
        else renderEmpty();
      } catch (error) {
        showToast(await requestFailureMessage(error, "初始化失败"), "error");
        renderEmpty();
      }
    }

    async function loadRuntimes(refresh) {
      refreshRuntimesButton.disabled = true;
      try {
        const response = await fetch(refresh ? "/api/runtimes/refresh" : "/api/runtimes", {
          method: refresh ? "POST" : "GET"
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "运行时检测失败");
        state.runtimes = payload.runtimes || [];
        const online = state.runtimes.filter(runtime => runtime.status === "online");
        const remembered = window.localStorage.getItem("logpilot.runtime");
        const preferred = [remembered, payload.selected, "codex", "claude"].find(id =>
          online.some(runtime => runtime.id === id)
        );
        state.selectedRuntime = preferred || "";
        renderRuntimes();
        if (refresh) showToast(`运行时状态已刷新，${online.length} 个在线`, "success");
      } catch (error) {
        state.runtimes = [];
        state.selectedRuntime = "";
        renderRuntimes();
        if (refresh) showToast(await requestFailureMessage(error, "刷新失败"), "error");
      } finally {
        refreshRuntimesButton.disabled = false;
      }
    }

    async function activateRepository(path, quiet = false) {
      const target = String(path || "").trim();
      if (!target) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return false;
      }
      try {
        const response = await fetch("/api/repository", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库切换失败");
        await applyRepositoryState(payload);
        if (!quiet) showToast("仓库路径已记住", "success");
        return true;
      } catch (error) {
        repoPath.value = state.path;
        showToast(await requestFailureMessage(error, "仓库切换失败"), "error");
        return false;
      }
    }

    async function applyRepositoryState(payload) {
      state.path = payload.repository || payload.path || state.path;
      state.history = payload.history || [];
      state.activeRunId = state.history[0]?.run_id || "";
      repoPath.value = state.path;
      updateRepositoryIdentity(state.path);
      await loadRepositorySettings(state.path, true);
      renderHistory(state.history);
      if (payload.has_report) await loadReport();
      else renderEmpty();
    }

    async function loadRepositorySettings(path, quiet = false) {
      const target = String(path || "").trim();
      if (!target || state.settingsBusy) return;
      state.settingsBusy = true;
      updateSettingsBusy();
      try {
        const response = await fetch(`/api/settings?path=${encodeURIComponent(target)}`);
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库设置读取失败");
        state.repositorySettings = {
          ...emptyRepositorySettings(),
          ...(payload.settings || {}),
          templates: { ...(payload.settings?.templates || {}) },
          language_presets: [...(payload.settings?.language_presets || [])],
          template_presets: [...(payload.settings?.template_presets || [])]
        };
        state.languageProfile = payload.profile || { detected_languages: [], template_recommendations: {} };
        state.settingsLanguages = payload.languages || [];
        if (!state.settingsLanguages.some(item => item.id === state.templateLanguage)) {
          state.templateLanguage = state.settingsLanguages[0]?.id || "python";
        }
        renderRepositorySettings(payload.repository || target);
      } catch (error) {
        if (!quiet) showToast(await requestFailureMessage(error, "设置读取失败"), "error");
      } finally {
        state.settingsBusy = false;
        updateSettingsBusy();
      }
    }

    async function saveRepositorySettings() {
      await persistRepositorySettings(false);
    }

    async function persistRepositorySettings(quiet = false) {
      if (state.settingsBusy) {
        if (!quiet) showToast("设置正在处理中，请稍候", "warning");
        return false;
      }
      const target = repoPath.value.trim();
      if (!target) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return false;
      }
      state.settingsBusy = true;
      updateSettingsBusy();
      try {
        const response = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target, settings: state.repositorySettings })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "设置保存失败");
        state.repositorySettings = payload.settings;
        state.languageProfile = payload.profile || state.languageProfile;
        state.settingsLanguages = payload.languages || state.settingsLanguages;
        renderRepositorySettings(payload.repository);
        if (!quiet) showToast("仓库设置已保存", "success");
        return true;
      } catch (error) {
        showToast(await requestFailureMessage(error, "保存失败"), "error");
        return false;
      } finally {
        state.settingsBusy = false;
        updateSettingsBusy();
      }
    }

    async function profileRepository() {
      if (state.settingsBusy) return;
      state.settingsBusy = true;
      updateSettingsBusy();
      showToast("正在识别语言和日志风格...", "info", 0);
      try {
        const response = await fetch("/api/settings/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: repoPath.value.trim() })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库识别失败");
        state.languageProfile = payload.profile || state.languageProfile;
        state.settingsLanguages = payload.languages || state.settingsLanguages;
        renderRepositorySettings(payload.repository);
        showToast("语言与日志模板推荐已更新", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "识别失败"), "error");
      } finally {
        state.settingsBusy = false;
        updateSettingsBusy();
      }
    }

    async function browseRepository() {
      if (state.browsing) return;
      setBrowsing(true);
      showToast("正在打开仓库选择器...", "info", 0);
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 125000);
      try {
        const response = await fetch("/api/browse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: repoPath.value.trim() }),
          signal: controller.signal
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "选择仓库失败");
        if (payload.cancelled) {
          showToast("已取消选择", "info");
          return;
        }
        await applyRepositoryState(payload);
        showToast("仓库路径已更新并记住", "success");
      } catch (error) {
        const message = error.name === "AbortError"
          ? "选择失败：选择窗口超时，请手动输入路径或重试。"
          : await requestFailureMessage(error, "选择失败");
        showToast(message, "error");
      } finally {
        clearTimeout(timeoutId);
        setBrowsing(false);
      }
    }

    async function startScan(path) {
      if (state.scanning) return;
      const target = path.trim();
      if (!target) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return;
      }
      if (target !== state.path && !await activateRepository(target, true)) return;
      const runtime = selectedRuntime();
      if (!runtime) {
        showToast("没有可用运行时，请先在运行时页面检查 Codex 或 Claude", "warning");
        return;
      }
      if (!await persistRepositorySettings(true)) return;
      resetReportForScan();
      setScanning(true);
      showToast(`已通过 ${runtime.name} 启动后台分析`, "info");
      try {
        const response = await fetch("/api/scans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target, runtime: runtime.id })
        });
        const payload = await response.json();
        if (!response.ok && !(response.status === 409 && payload.job)) {
          throw new Error(payload.error || "分析任务创建失败");
        }
        const job = payload.job;
        if (!job?.job_id) throw new Error("分析任务缺少任务标识");
        state.scanJobId = job.job_id;
        state.scanReportVersion = -1;
        renderScanProgress(job);
        if (response.status === 409) showToast("已恢复该仓库正在执行的分析任务", "info");
        await pollScanJob(runtime);
      } catch (error) {
        const message = await requestFailureMessage(error, "分析失败");
        showToast(message, "error");
        setScanning(false);
        markScanProgressFailed(message);
      }
    }

    async function resumeScan(job) {
      const runtime = state.runtimes.find(item => item.id === job.runtime_id) || selectedRuntime();
      if (!runtime) {
        showToast("检测到未完成分析，但对应运行时当前不可用", "warning");
        return;
      }
      state.selectedRuntime = runtime.id;
      renderRuntimes();
      resetReportForScan();
      state.scanJobId = job.job_id;
      state.scanReportVersion = -1;
      setScanning(true);
      if (job.partial_report) {
        state.scanReportVersion = job.report_version;
        renderReport(job.partial_report, true);
      }
      renderScanProgress(job);
      showTab("current");
      showToast("已恢复正在进行的分析任务", "info");
      try {
        await pollScanJob(runtime);
      } catch (error) {
        setScanning(false);
        const message = await requestFailureMessage(error, "分析失败");
        markScanProgressFailed(message);
        showToast(message, "error");
      }
    }

    async function pollScanJob(runtime) {
      while (state.scanning && state.scanJobId) {
        const response = await fetch(
          `/api/scans/${encodeURIComponent(state.scanJobId)}?report_version=${state.scanReportVersion}`
        );
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "分析状态读取失败");
        const job = payload.job;
        renderScanProgress(job);
        if (job.partial_report) {
          state.scanReportVersion = job.report_version;
          renderReport(job.partial_report, true);
        }
        if (job.status === "completed") {
          await completeScan(job, runtime);
          return;
        }
        if (job.status === "failed") throw new Error(job.error || "后台分析失败");
        if (job.status === "cancelled") {
          setScanning(false);
          incrementalNote.classList.add("hidden");
          showToast("分析已取消，部分结果没有写入历史记录", "warning");
          return;
        }
        await new Promise(resolve => window.setTimeout(resolve, 650));
      }
    }

    async function completeScan(job, runtime) {
      state.path = job.repository;
      repoPath.value = state.path;
      updateRepositoryIdentity(state.path);
      const historyResponse = await fetch("/api/history");
      const historyPayload = await historyResponse.json();
      if (!historyResponse.ok || historyPayload.error) throw new Error(historyPayload.error || "历史记录刷新失败");
      state.history = historyPayload.runs || [];
      state.activeRunId = job.run_id || state.history[0]?.run_id || "";
      renderHistory(state.history);
      await loadReport();
      await loadRepositorySettings(state.path, true);
      showTab("current");
      setScanning(false);
      renderScanProgress(job);
      incrementalNote.classList.add("hidden");
      showToast(`${runtime.name} 分析完成，结果已更新`, "success");
      window.setTimeout(() => {
        if (!state.scanning) scanProgress.classList.add("hidden");
      }, 1800);
    }

    async function cancelScan() {
      if (!state.scanning || !state.scanJobId || state.scanCancelRequested) return;
      state.scanCancelRequested = true;
      cancelScanButton.disabled = true;
      cancelScanButton.textContent = "正在停止...";
      try {
        const response = await fetch(`/api/scans/${encodeURIComponent(state.scanJobId)}/cancel`, {
          method: "POST"
        });
        const payload = await response.json();
        if (!response.ok && response.status !== 409) throw new Error(payload.error || "停止失败");
        if (payload.job) renderScanProgress(payload.job);
      } catch (error) {
        state.scanCancelRequested = false;
        cancelScanButton.disabled = false;
        cancelScanButton.textContent = "停止分析";
        showToast(await requestFailureMessage(error, "停止失败"), "error");
      }
    }

    function resetReportForScan() {
      state.report = null;
      state.reportActionable = false;
      state.patch = "";
      state.activeRunId = "";
      state.scanJobId = "";
      state.scanReportVersion = -1;
      state.scanCancelRequested = false;
      state.selectedGroups = new Set();
      state.expandedGroups = new Set();
      state.collapsedFiles = new Set();
      state.searchQuery = "";
      state.severityFilter = "all";
      state.actionFilter = "all";
      state.appliedIssueIds = new Set();
      state.applyRecords = [];
      resultSearch.value = "";
      document.querySelector("#metrics").innerHTML = summaryMarkup(null);
      resultsSummary.textContent = "正在准备分析";
      resultStream.innerHTML = '<div class="results-empty">本地规则完成后将在这里显示第一批结果</div>';
      fullPatchButton.disabled = true;
      expandAllButton.disabled = true;
      collapseAllButton.disabled = true;
      batchBar.classList.add("hidden");
      snapshotBanner.classList.add("hidden");
      coverageBanner.classList.add("hidden");
      scanProgress.classList.remove("hidden", "failed", "completed");
      incrementalNote.classList.add("hidden");
      renderScanProgress({ status: "queued", stage: "queued", percent: 0, message: "正在创建后台分析任务", completed: 0, total: 0 });
    }

    function renderScanProgress(job) {
      const stageIndexes = { queued: 0, preparing: 0, discovering: 0, parsing: 1, framework: 2, rules: 2, runtime: 3, ai_missing: 3, fixes: 4, reporting: 4, complete: 5 };
      const titles = {
        queued: "等待开始",
        preparing: "准备分析",
        discovering: "发现源码文件",
        parsing: "解析源码",
        framework: "识别日志框架",
        rules: "执行本地规则",
        runtime: "运行时分析",
        ai_missing: "检查日志缺口",
        fixes: "生成修改建议",
        reporting: "保存分析报告",
        complete: "分析完成"
      };
      const terminal = ["completed", "failed", "cancelled"].includes(job.status);
      const currentIndex = stageIndexes[job.stage] ?? 0;
      const percent = Number(job.percent || 0);
      scanProgress.classList.remove("hidden");
      scanProgress.classList.toggle("failed", job.status === "failed");
      scanProgress.classList.toggle("completed", job.status === "completed");
      scanProgressTitle.textContent = job.status === "failed" ? "分析失败" : job.status === "cancelled" ? "分析已取消" : titles[job.stage] || "正在分析";
      scanProgressPercent.textContent = `${percent}%`;
      scanProgressMessage.textContent = job.message || "正在处理";
      scanProgressTrack.style.setProperty("--progress", `${percent}%`);
      scanProgressTrack.classList.toggle("indeterminate", !terminal && Number(job.total || 0) === 0);
      scanSteps.querySelectorAll("[data-scan-step]").forEach((step, index) => {
        step.classList.toggle("done", job.status === "completed" || index < currentIndex);
        step.classList.toggle("active", !terminal && index === currentIndex);
      });
      cancelScanButton.disabled = terminal || job.status === "cancelling" || state.scanCancelRequested;
      cancelScanButton.textContent = job.status === "cancelling" || state.scanCancelRequested ? "正在停止..." : "停止分析";
      incrementalNote.classList.toggle("hidden", !state.scanning || !state.report);
    }

    function markScanProgressFailed(message) {
      renderScanProgress({ status: "failed", stage: "failed", percent: 0, message, completed: 0, total: 1 });
      incrementalNote.classList.toggle("hidden", !state.report);
    }

    async function loadReport() {
      const reportResponse = await fetch("/api/report");
      const report = await reportResponse.json();
      if (!reportResponse.ok || report.error) throw new Error(report.error || "报告读取失败");
      state.reportActionable = true;
      renderReport(report);
      await loadPatch();
      await loadApplies();
    }

    async function loadPatch() {
      const response = await fetch("/api/patch");
      const text = await response.text();
      state.patch = response.ok ? text : "暂无补丁产物。";
      fullPatchButton.disabled = false;
      renderFullPatch(state.patch);
    }

    async function loadHistoryRun(runId) {
      showToast("正在读取历史分析...", "info", 0);
      try {
        const response = await fetch(`/api/history/run?run_id=${encodeURIComponent(runId)}`);
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "历史记录读取失败");
        state.reportActionable = true;
        renderReport(payload.report);
        state.activeRunId = runId;
        state.patch = payload.patch || "暂无补丁产物。";
        fullPatchButton.disabled = false;
        renderFullPatch(state.patch);
        if (payload.metadata && payload.metadata.repository) {
          updateRepositoryIdentity(payload.metadata.repository);
        }
        await loadApplies();
        showTab("current");
        showToast("历史分析已加载", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "历史记录读取失败"), "error");
      }
    }

    async function loadApplies() {
      if (!state.activeRunId) {
        setApplyState({});
        return;
      }
      const response = await fetch(`/api/applies?run_id=${encodeURIComponent(state.activeRunId)}`);
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || "采纳状态读取失败");
      setApplyState(payload);
    }

    function setApplyState(payload) {
      state.applyRecords = payload.records || [];
      state.appliedIssueIds = new Set(payload.applied_issue_ids || []);
      state.latestApplyId = payload.latest_apply_id || "";
      state.canRollback = Boolean(payload.can_rollback);
      state.selectedGroups = new Set(
        [...state.selectedGroups].filter(groupId => {
          const group = issueGroups().find(item => item.id === groupId);
          return group && !isGroupApplied(group);
        })
      );
      renderResultStream();
      renderSnapshotBanner();
    }

    function renderSnapshotBanner() {
      const hasApplied = state.applyRecords.some(record => record.status === "applied");
      snapshotBanner.classList.toggle("hidden", !hasApplied);
      rollbackButton.disabled = !state.canRollback || state.applying;
      rollbackButton.title = state.canRollback ? "恢复最近一次采纳前的源码" : "只能撤销该仓库最近一次有效采纳";
    }

    function openApplyDialog(issueIds) {
      if (!state.reportActionable) {
        showToast(state.scanning ? "分析完成后才能采纳修改" : "未完成的临时结果不能采纳，请重新分析", "warning");
        return;
      }
      const unique = [...new Set(issueIds)].filter(Boolean);
      if (!unique.length || !state.activeRunId) {
        showToast("当前问题没有可安全采纳的修改", "warning");
        return;
      }
      const selected = issueGroups().filter(group => patchIssueIds(group).some(id => unique.includes(id)));
      const files = new Set(selected.map(group => group.primary.file_path));
      state.pendingIssueIds = unique;
      applySummary.innerHTML = `将采纳 <strong>${selected.length}</strong> 处精确修改，涉及 <strong>${files.size}</strong> 个文件。<br>写入前会统一校验源码快照，任一修改失效时整批取消。`;
      applyDialog.classList.remove("hidden");
      confirmApplyButton.focus();
    }

    function closeApplyConfirmation() {
      if (state.applying) return;
      applyDialog.classList.add("hidden");
      state.pendingIssueIds = [];
    }

    async function submitApply() {
      if (state.applying || !state.pendingIssueIds.length) return;
      state.applying = true;
      confirmApplyButton.disabled = true;
      confirmApplyButton.textContent = "正在采纳...";
      try {
        const response = await fetch("/api/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run_id: state.activeRunId, issue_ids: state.pendingIssueIds })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "采纳失败");
        applyDialog.classList.add("hidden");
        state.pendingIssueIds = [];
        state.selectedGroups = new Set();
        setApplyState(payload.applies || {});
        showToast("修改已采纳，原文件已保存到用户数据目录", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "采纳失败"), "error");
      } finally {
        state.applying = false;
        confirmApplyButton.disabled = false;
        confirmApplyButton.textContent = "确认采纳";
        renderSnapshotBanner();
      }
    }

    async function rollbackLatestApply() {
      if (state.applying || !state.canRollback) return;
      state.applying = true;
      rollbackButton.disabled = true;
      try {
        const response = await fetch("/api/apply/rollback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ apply_id: state.latestApplyId })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "撤销失败");
        setApplyState(payload.applies || {});
        showToast("上次采纳已撤销", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "撤销失败"), "error");
      } finally {
        state.applying = false;
        renderSnapshotBanner();
      }
    }

    function showToast(message, type = "info", duration = null) {
      const current = toastRegion.firstElementChild;
      if (current) current.remove();
      const toast = document.createElement("div");
      toast.className = `toast ${type}`;
      toast.setAttribute("role", type === "error" ? "alert" : "status");
      toast.innerHTML = `<div class="toast-message">${esc(message)}</div>`;
      toastRegion.appendChild(toast);
      const hideAfter = duration ?? (type === "error" ? 5200 : type === "warning" ? 3800 : 2800);
      if (hideAfter > 0) {
        window.setTimeout(() => {
          if (!toast.isConnected) return;
          toast.classList.add("leaving");
          window.setTimeout(() => toast.remove(), 170);
        }, hideAfter);
      }
    }

    async function requestFailureMessage(error, action) {
      const detail = String(error?.message || "").trim();
      const networkFailure = error instanceof TypeError || /network|fetch/i.test(detail);
      if (!networkFailure) return `${action}：${detail || "未知错误"}`;
      try {
        const response = await fetch("/api/state", { cache: "no-store" });
        if (response.ok) return `${action}，本地服务仍在线，请重试。`;
      } catch (_stateError) {
        // The state probe is the final distinction between an endpoint failure and a stopped service.
      }
      return "本地 LogPilot 服务已退出，请重新启动服务";
    }

    function setScanning(value) {
      state.scanning = value;
      scanButton.disabled = value || !selectedRuntime();
      runtimeSelect.disabled = value;
      scanButton.querySelector("span").textContent = value ? "分析中..." : "开始分析";
      if (!value) {
        state.scanCancelRequested = false;
        cancelScanButton.disabled = true;
      }
      if (state.report) renderResultStream();
    }

    function setBrowsing(value) {
      state.browsing = value;
      browseButton.disabled = value;
      browseButton.querySelector("span").textContent = value ? "选择中..." : "选择仓库";
    }

    function openFullPatch() {
      renderFullPatch(state.patch || "本次分析没有生成安全修改。");
      fullPatchDialog.classList.remove("hidden");
      closePatchDialog.focus();
    }

    function closeFullPatch() {
      fullPatchDialog.classList.add("hidden");
      fullPatchButton.focus();
    }

    function toggleDiagnostics() {
      state.diagnosticsOpen = !state.diagnosticsOpen;
      renderDiagnostics();
    }

    function showTab(name) {
      state.activeView = name;
      currentPanel.classList.toggle("hidden", name !== "current");
      historyPanel.classList.toggle("hidden", name !== "history");
      settingsPanel.classList.toggle("hidden", name !== "settings");
      currentTab.classList.toggle("active", name === "current");
      historyTab.classList.toggle("active", name === "history");
      settingsTab.classList.toggle("active", name === "settings");
    }

    function repositoryName(path) {
      const normalized = String(path || "").replace(/[\\/]+$/, "");
      return normalized.split(/[\\/]/).filter(Boolean).pop() || "未选择仓库";
    }

    function updateRepositoryIdentity(path) {
      state.path = path || state.path;
    }

    function selectedRuntime() {
      return state.runtimes.find(runtime => runtime.id === state.selectedRuntime && runtime.status === "online") || null;
    }

    function updateRuntimeIndicator() {
      const runtime = selectedRuntime();
      runtimeDot.classList.toggle("offline", !runtime);
      scanButton.disabled = state.scanning || !runtime;
      scanButton.title = runtime ? `使用 ${runtime.name} 执行分析` : "没有可用运行时";
    }

    function renderRuntimes() {
      const online = state.runtimes.filter(runtime => runtime.status === "online");
      document.querySelector("#runtimeSummary").textContent = `${online.length} 个在线 · ${state.runtimes.length} 个已检测`;
      runtimeSelect.innerHTML = state.runtimes.length
        ? state.runtimes.map(runtime => `<option value="${esc(runtime.id)}" ${runtime.status !== "online" ? "disabled" : ""}>${esc(runtime.name)} · ${runtime.status === "online" ? "在线" : "离线"}</option>`).join("")
        : '<option value="">未发现运行时</option>';
      runtimeSelect.value = state.selectedRuntime;
      const list = document.querySelector("#runtimeList");
      list.innerHTML = state.runtimes.length ? state.runtimes.map(runtime => `
        <button class="runtime-row ${runtime.id === state.selectedRuntime ? "selected" : ""}" type="button" data-runtime-id="${esc(runtime.id)}" ${runtime.status !== "online" ? "disabled" : ""}>
          <div class="runtime-name"><span class="runtime-logo">${esc(runtime.name.slice(0, 1))}</span><strong>${esc(runtime.name)}</strong><span class="runtime-badge">内置</span></div>
          <div class="health ${esc(runtime.status)}"><span class="state-dot ${runtime.status === "online" ? "" : "offline"}"></span>${runtime.status === "online" ? "在线" : "离线"}</div>
          <div class="runtime-value" title="${esc(runtime.version || runtime.error)}">${esc(runtime.version || "未检测到")}</div>
          <div class="runtime-value" title="${esc(runtime.executable_path || runtime.error)}">${esc(runtime.executable_path || runtime.error)}</div>
        </button>
      `).join("") : '<div class="empty">未检测到 Codex 或 Claude 命令行运行时</div>';
      list.querySelectorAll("button[data-runtime-id]").forEach(button => {
        button.addEventListener("click", () => {
          state.selectedRuntime = button.dataset.runtimeId;
          runtimeSelect.value = state.selectedRuntime;
          window.localStorage.setItem("logpilot.runtime", state.selectedRuntime);
          renderRuntimes();
          showToast(`已选择 ${selectedRuntime().name} 运行时`, "success");
        });
      });
      updateRuntimeIndicator();
    }

    function emptyRepositorySettings() {
      return {
        language_mode: "auto",
        selected_languages: [],
        templates: {},
        language_presets: [],
        template_presets: [],
        active_language_preset: "auto",
        active_template_preset: "auto",
        analysis_depth: "standard"
      };
    }

    function presetCollection(type) {
      return type === "language"
        ? state.repositorySettings.language_presets || []
        : state.repositorySettings.template_presets || [];
    }

    function activePresetId(type) {
      return type === "language"
        ? state.repositorySettings.active_language_preset || "auto"
        : state.repositorySettings.active_template_preset || "auto";
    }

    function resolvedLanguageIds() {
      if (state.repositorySettings.language_mode === "custom" && state.repositorySettings.selected_languages.length) {
        return [...state.repositorySettings.selected_languages];
      }
      const detected = (state.languageProfile.detected_languages || [])
        .filter(item => item.recommended)
        .map(item => item.id);
      if (detected.length) return detected;
      return state.settingsLanguages.some(item => item.id === "python")
        ? ["python"]
        : state.settingsLanguages.slice(0, 1).map(item => item.id);
    }

    async function selectAnalysisPreset(type, identifier) {
      if (identifier === "current") return;
      const previous = cloneRepositorySettings();
      applyPreset(type, identifier);
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) {
        const selected = identifier === "auto"
          ? (type === "language" ? "自动识别语言" : "自动匹配模板")
          : presetCollection(type).find(item => item.id === identifier)?.name || "方案";
        showToast(`已启用${selected}`, "success");
      } else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    function applyPreset(type, identifier) {
      if (type === "language") {
        if (identifier === "auto") {
          state.repositorySettings.language_mode = "auto";
          state.repositorySettings.selected_languages = [];
          state.repositorySettings.active_language_preset = "auto";
          return;
        }
        const preset = presetCollection("language").find(item => item.id === identifier);
        if (!preset) return;
        state.repositorySettings.language_mode = "custom";
        state.repositorySettings.selected_languages = [...preset.languages];
        state.repositorySettings.active_language_preset = preset.id;
        return;
      }
      if (identifier === "auto") {
        state.repositorySettings.templates = {};
        state.repositorySettings.active_template_preset = "auto";
        return;
      }
      const preset = presetCollection("template").find(item => item.id === identifier);
      if (!preset) return;
      state.repositorySettings.templates = { ...preset.templates };
      state.repositorySettings.active_template_preset = preset.id;
    }

    function openPresetDialog(type) {
      if (!repoPath.value.trim()) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return;
      }
      state.presetDialogType = type;
      presetDialogTitle.textContent = type === "language" ? "新增语言方案" : "新增模板方案";
      presetDialogDescription.textContent = type === "language"
        ? "保存当前语言组合，后续分析可直接选择"
        : "保存当前日志模板，后续分析可直接选择";
      presetNameInput.value = "";
      presetDialog.classList.remove("hidden");
      setTimeout(() => presetNameInput.focus(), 0);
    }

    function closePresetEditor() {
      presetDialog.classList.add("hidden");
      state.presetDialogType = "";
      presetNameInput.value = "";
    }

    async function createPreset() {
      const type = state.presetDialogType;
      const name = presetNameInput.value.trim();
      if (!type || !name || name.length > 40) {
        showToast("请输入方案名称", "warning");
        return;
      }
      const previous = cloneRepositorySettings();
      const identifier = `${type}-${Date.now()}`;
      if (type === "language") {
        const languages = resolvedLanguageIds();
        if (!languages.length) {
          showToast("当前没有可保存的语言", "warning");
          return;
        }
        state.repositorySettings.language_presets.push({ id: identifier, name, languages });
        applyPreset("language", identifier);
      } else {
        const templates = Object.fromEntries(
          resolvedLanguageIds().map(language => [language, effectiveTemplate(language)]).filter(([, value]) => value)
        );
        if (!Object.keys(templates).length) {
          showToast("当前没有可保存的模板", "warning");
          return;
        }
        state.repositorySettings.template_presets.push({ id: identifier, name, templates });
        applyPreset("template", identifier);
      }
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) {
        closePresetEditor();
        showToast(`方案“${name}”已保存`, "success");
      } else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    async function loadSavedPreset(type, identifier) {
      if (!identifier) return;
      const previous = cloneRepositorySettings();
      applyPreset(type, identifier);
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) showToast("方案已载入", "success");
      else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    async function deleteSavedPreset(type, identifier) {
      if (!identifier) return;
      const preset = presetCollection(type).find(item => item.id === identifier);
      if (!preset) return;
      const previous = cloneRepositorySettings();
      const collectionKey = type === "language" ? "language_presets" : "template_presets";
      state.repositorySettings[collectionKey] = presetCollection(type).filter(item => item.id !== identifier);
      if (activePresetId(type) === identifier) applyPreset(type, "auto");
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) showToast(`方案“${preset.name}”已删除`, "success");
      else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    function cloneRepositorySettings() {
      return JSON.parse(JSON.stringify(state.repositorySettings));
    }

    function renderPresetSelectors() {
      const languagePresets = presetCollection("language");
      const templatePresets = presetCollection("template");
      const languageCustom = state.repositorySettings.language_mode === "custom"
        && activePresetId("language") === "auto";
      const templateCustom = Object.keys(state.repositorySettings.templates || {}).length > 0
        && activePresetId("template") === "auto";
      analysisLanguagePreset.innerHTML = [
        '<option value="auto">自动识别</option>',
        ...(languageCustom ? ['<option value="current">当前自定义</option>'] : []),
        ...languagePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`)
      ].join("");
      analysisTemplatePreset.innerHTML = [
        '<option value="auto">自动匹配</option>',
        ...(templateCustom ? ['<option value="current">当前自定义</option>'] : []),
        ...templatePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`)
      ].join("");
      analysisLanguagePreset.value = languageCustom ? "current" : activePresetId("language");
      analysisTemplatePreset.value = templateCustom ? "current" : activePresetId("template");
      const languageIds = resolvedLanguageIds();
      const languageSummary = languageIds.map(id => {
        const language = languageDefinition(id);
        return language ? `${language.label}${language.support_level === "unsupported" ? "（暂不支持）" : ""}` : id;
      }).join("、") || "等待识别";
      const unrecognizedCount = Object.values(state.languageProfile.unrecognized_extensions || {}).reduce((total, value) => total + Number(value || 0), 0);
      analysisLanguageSummary.textContent = unrecognizedCount ? `${languageSummary} · ${unrecognizedCount} 个未知源码文件` : languageSummary;
      analysisTemplateSummary.textContent = templateCustom
        ? `${Object.keys(state.repositorySettings.templates).length} 种自定义模板`
        : activePresetId("template") === "auto" ? "优先沿用仓库日志风格" : "使用已保存模板";
      renderAnalysisDepth();

      settingsLanguagePreset.innerHTML = '<option value="">选择历史方案</option>'
        + languagePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
      settingsTemplatePreset.innerHTML = '<option value="">选择历史方案</option>'
        + templatePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
      settingsLanguagePreset.value = activePresetId("language") === "auto" ? "" : activePresetId("language");
      settingsTemplatePreset.value = activePresetId("template") === "auto" ? "" : activePresetId("template");
      updatePresetLibraryActions();
    }

    function renderAnalysisDepth() {
      const depth = state.repositorySettings.analysis_depth || "standard";
      analysisDepth.value = depth;
      analysisDepthSummary.textContent = {
        quick: "优先高风险，最多 100 条日志",
        standard: "完整主流程，限制极端规模",
        deep: "不限制 AI 分析目标数量"
      }[depth] || "完整主流程，限制极端规模";
    }

    function updatePresetLibraryActions() {
      loadLanguagePreset.disabled = state.settingsBusy || !settingsLanguagePreset.value;
      deleteLanguagePreset.disabled = state.settingsBusy || !settingsLanguagePreset.value;
      loadTemplatePreset.disabled = state.settingsBusy || !settingsTemplatePreset.value;
      deleteTemplatePreset.disabled = state.settingsBusy || !settingsTemplatePreset.value;
      saveLanguagePreset.disabled = state.settingsBusy;
      saveTemplatePreset.disabled = state.settingsBusy;
    }

    function renderRepositorySettings(path = repoPath.value) {
      document.querySelector("#settingsRepository").textContent = repositoryName(path);
      const settings = state.repositorySettings || emptyRepositorySettings();
      languageMode.querySelectorAll("button[data-language-mode]").forEach(button => {
        button.classList.toggle("active", button.dataset.languageMode === settings.language_mode);
      });
      const detected = new Map((state.languageProfile.detected_languages || []).map(item => [item.id, item]));
      languageOptions.innerHTML = state.settingsLanguages.map(language => {
        const profile = detected.get(language.id) || {};
        const checked = settings.language_mode === "auto"
          ? Boolean(profile.recommended)
          : settings.selected_languages.includes(language.id);
        const support = language.support_level === "unsupported" ? "暂不支持" : language.support_level === "limited" ? "有限支持" : "完整支持";
        const stats = profile.file_count
          ? `${profile.file_count} 个文件 · ${profile.log_count || 0} 条日志 · ${support}`
          : "未在仓库中发现";
        return `<label class="language-option"><input type="checkbox" data-language-id="${esc(language.id)}" ${checked ? "checked" : ""} ${settings.language_mode === "auto" ? "disabled" : ""}><span><strong>${esc(language.label)}${profile.recommended ? " <em>推荐</em>" : ""}</strong><span>${esc(stats)}</span></span></label>`;
      }).join("");
      templateLanguageNav.innerHTML = state.settingsLanguages.map(language => `
        <button class="${language.id === state.templateLanguage ? "active" : ""}" type="button" data-template-language="${esc(language.id)}"><span>${esc(language.label)}</span><span>${esc(templateSourceText(language.id, true))}</span></button>
      `).join("");
      templateInput.value = effectiveTemplate(state.templateLanguage);
      renderTemplateMeta();
      renderPresetSelectors();
      updateSettingsBusy();
    }

    function renderTemplateMeta() {
      const language = languageDefinition(state.templateLanguage);
      templateSource.textContent = templateSourceText(state.templateLanguage, false);
      templateSupport.textContent = language?.automatic_fix
        ? "支持自动补充"
        : language?.support_level === "unsupported" ? "暂不支持解析" : "当前仅分析";
      templateSupport.classList.toggle("ready", Boolean(language?.automatic_fix));
      const recommendation = templateRecommendation(state.templateLanguage);
      useRecommendedTemplate.disabled = state.settingsBusy || !recommendation.template;
    }

    function updateSettingsBusy() {
      saveSettingsButton.disabled = state.settingsBusy;
      profileRepositoryButton.disabled = state.settingsBusy;
      analysisLanguagePreset.disabled = state.settingsBusy;
      analysisTemplatePreset.disabled = state.settingsBusy;
      analysisDepth.disabled = state.settingsBusy;
      addLanguagePreset.disabled = state.settingsBusy;
      addTemplatePreset.disabled = state.settingsBusy;
      saveSettingsButton.textContent = state.settingsBusy ? "处理中..." : "保存设置";
      updatePresetLibraryActions();
    }

    function languageDefinition(languageId) {
      return state.settingsLanguages.find(item => item.id === languageId) || null;
    }

    function templateRecommendation(languageId) {
      return state.languageProfile.template_recommendations?.[languageId] || {};
    }

    function hasFixedTemplate(languageId) {
      return Object.prototype.hasOwnProperty.call(state.repositorySettings.templates || {}, languageId)
        && Boolean(String(state.repositorySettings.templates[languageId] || "").trim());
    }

    function effectiveTemplate(languageId) {
      if (hasFixedTemplate(languageId)) return state.repositorySettings.templates[languageId];
      const recommendation = templateRecommendation(languageId);
      return recommendation.template || languageDefinition(languageId)?.builtin_template || "";
    }

    function templateSourceText(languageId, compact) {
      if (hasFixedTemplate(languageId)) return compact ? "固定" : "用户固定模板";
      const recommendation = templateRecommendation(languageId);
      if (recommendation.source === "repository") return compact ? "推荐" : "仓库推荐模板";
      return compact ? "内置" : "内置安全模板";
    }

    function renderEmpty() {
      state.report = null;
      state.reportActionable = false;
      state.patch = "";
      state.activeRunId = "";
      state.selectedGroups = new Set();
      state.expandedGroups = new Set();
      state.collapsedFiles = new Set();
      state.searchQuery = "";
      state.severityFilter = "all";
      state.actionFilter = "all";
      state.appliedIssueIds = new Set();
      state.applyRecords = [];
      resultSearch.value = "";
      document.querySelector("#metrics").innerHTML = summaryMarkup(null);
      resultsSummary.textContent = "等待分析结果";
      resultStream.innerHTML = '<div class="results-empty">选择仓库并开始分析</div>';
      fullPatchButton.disabled = true;
      expandAllButton.disabled = true;
      collapseAllButton.disabled = true;
      batchApplyButton.disabled = true;
      batchBar.classList.add("hidden");
      snapshotBanner.classList.add("hidden");
      coverageBanner.classList.add("hidden");
      scanProgress.classList.add("hidden");
      incrementalNote.classList.add("hidden");
      updateResultFilters();
      renderDiagnostics();
    }

    function renderReport(report, incremental = false) {
      const previousGroupIds = new Set(issueGroups().map(group => group.id));
      state.report = report;
      if (!incremental) {
        state.patch = "";
        state.selectedGroups = new Set();
        state.collapsedFiles = new Set();
        state.searchQuery = "";
        state.severityFilter = "all";
        state.actionFilter = "all";
        state.appliedIssueIds = new Set();
        state.applyRecords = [];
        resultSearch.value = "";
        fullPatchButton.disabled = true;
      }
      renderMetrics(report.summary);
      const currentGroups = issueGroups();
      if (incremental) {
        currentGroups.forEach(group => {
          if (!previousGroupIds.has(group.id)) state.expandedGroups.add(group.id);
        });
      } else {
        state.expandedGroups = new Set(currentGroups.map(group => group.id));
      }
      renderResultStream();
      renderDiagnostics();
    }

    function renderMetrics(summary) {
      document.querySelector("#metrics").innerHTML = summaryMarkup(summary);
      renderCoverageBanner(summary);
    }

    function summaryMarkup(summary) {
      if (!summary) {
        return `
          <div class="score-panel score-neutral"><div class="score-heading"><span class="metric-label">治理评分</span><span class="score-status">待分析</span></div><div class="score-line"><strong>-</strong><span>/ 100</span></div><div class="score-track" style="--score:0"><i></i></div></div>
          ${metricMarkup("-", "扫描文件")}
          ${metricMarkup("-", "日志调用")}
          ${metricMarkup("-", "发现问题")}
          ${riskMarkup({})}
        `;
      }
      const sev = summary.severity_counts || {};
      const hasScore = summary.score !== null && summary.score !== undefined;
      const scoreValue = hasScore ? summary.score : 0;
      const scoreDisplay = hasScore ? summary.score : "N/A";
      const discovered = summary.discovered_files || summary.files_scanned || 0;
      return `
        <div class="score-panel ${esc(scoreTone(summary))}"><div class="score-heading"><span class="metric-label">治理评分</span><span class="score-status">${esc(scoreLabel(summary))}</span></div><div class="score-line"><strong>${esc(scoreDisplay)}</strong>${hasScore ? "<span>/ 100</span>" : ""}</div><div class="score-track" style="--score:${esc(scoreValue)}"><i></i></div></div>
        ${metricMarkup(`${summary.files_scanned} / ${discovered}`, "分析覆盖")}
        ${metricMarkup(summary.log_count, "日志调用")}
        ${metricMarkup(summary.issue_count, "发现问题")}
        ${riskMarkup(sev)}
      `;
    }

    function metricMarkup(value, label) {
      return `<div class="metric"><span class="metric-label">${esc(label)}</span><strong>${esc(value)}</strong></div>`;
    }

    function riskMarkup(counts) {
      const hasCounts = Object.keys(counts).length > 0;
      return `<div class="risk-panel"><span class="metric-label">风险分布</span><div class="risk-breakdown">
        <div class="risk-stat high-risk"><span>高</span><strong>${esc(hasCounts ? counts.high || 0 : "-")}</strong></div>
        <div class="risk-stat medium-risk"><span>中</span><strong>${esc(hasCounts ? counts.medium || 0 : "-")}</strong></div>
        <div class="risk-stat low-risk"><span>低</span><strong>${esc(hasCounts ? counts.low || 0 : "-")}</strong></div>
      </div></div>`;
    }

    function scoreLabel(summary) {
      const status = summary.score_status;
      if (status === "no_log_samples") return "无日志样本";
      if (status === "insufficient_coverage") return "覆盖不足";
      if (status === "ai_incomplete") return "AI 未完成";
      if (status === "scoped") return "范围评分";
      if (status === "local_only") return "本地规则";
      const score = summary.score;
      if (score >= 85) return "健康";
      if (score >= 60) return "需关注";
      return "高风险";
    }

    function scoreTone(summary) {
      if (summary.score === null || summary.score === undefined) return summary.score_status === "insufficient_coverage" ? "score-warning" : "score-neutral";
      const score = summary.score;
      if (score >= 85) return "score-healthy";
      if (score >= 60) return "score-warning";
      return "score-danger";
    }

    function renderCoverageBanner(summary) {
      if (!summary) {
        coverageBanner.classList.add("hidden");
        return;
      }
      const languages = summary.language_coverage || [];
      const unsupported = languages.filter(item => item.support_level === "unsupported" && item.discovered_files > 0);
      const failed = languages.filter(item => item.failed_files > 0);
      const parseFailures = state.report?.parse_failures || [];
      const unrecognized = summary.unrecognized_extensions || {};
      const complete = summary.coverage_status === "complete";
      const fullyHealthyContext = complete && summary.ai_status === "complete" && ["scored", "scoped"].includes(summary.score_status);
      coverageBanner.classList.toggle("hidden", fullyHealthyContext);
      coverageBanner.classList.toggle("complete", fullyHealthyContext);
      coverageBanner.classList.toggle("failure", parseFailures.length > 0);
      if (parseFailures.length) {
        const visible = parseFailures.slice(0, 5).map(failure => {
          const kind = ({
            parse_error: "解析错误",
            native_crash: "原生进程崩溃",
            timeout: "解析超时",
            protocol_error: "通信错误",
            worker_start_failed: "进程启动失败"
          })[failure.error_kind] || failure.error_kind;
          const reason = String(failure.message || "未知原因").slice(0, 180);
          return `<span><code>${esc(failure.file_path)}</code> · ${esc(kind)} · ${esc(reason)}</span>`;
        }).join("");
        const remaining = parseFailures.length > 5 ? `<span>其余 ${parseFailures.length - 5} 个失败文件请查看报告。</span>` : "";
        coverageBanner.innerHTML = `<strong>有 ${parseFailures.length} 个文件解析失败</strong><div class="coverage-failure-list">${visible}${remaining}</div>`;
      } else if (unsupported.length || Object.keys(unrecognized).length) {
        const details = unsupported.map(item => `${item.label} ${item.discovered_files} 个文件`);
        Object.entries(unrecognized).forEach(([extension, count]) => details.push(`未知扩展 ${extension} ${count} 个文件`));
        const detail = details.join("、");
        const insightApis = [...new Set((state.report?.language_insights || []).flatMap(item => item.logging_apis || []))].slice(0, 6);
        const insightText = insightApis.length ? ` AI 抽样发现日志接口：${insightApis.map(esc).join("、")}；这些线索不计入覆盖率。` : "";
        coverageBanner.innerHTML = `<strong>分析覆盖不足</strong>：${esc(detail)}暂不支持，当前评分不能代表整个仓库。${insightText}`;
      } else if (failed.length) {
        coverageBanner.innerHTML = `<strong>部分源码解析失败</strong>：${esc(failed.map(item => `${item.label} ${item.failed_files} 个`).join("、"))}。`;
      } else if (summary.ai_status === "partial") {
        coverageBanner.innerHTML = `<strong>AI 分析未完整完成</strong>：本地规则结果已保留，建议重新分析失败批次。`;
      } else if (summary.ai_status === "skipped") {
        coverageBanner.innerHTML = `<strong>当前仅完成本地规则分析</strong>：选择在线运行时可获得语义和缺失日志分析。`;
      } else if (summary.score_status === "no_log_samples") {
        coverageBanner.innerHTML = `<strong>未发现日志样本</strong>：结果不会被标记为健康。`;
      } else {
        const discovered = summary.discovered_files || summary.files_scanned || 0;
        coverageBanner.innerHTML = `<strong>源码覆盖完整</strong>：已分析 ${esc(summary.files_scanned)} / ${esc(discovered)} 个源码文件。`;
      }
    }

    function renderResultStream() {
      updateResultFilters();
      if (!state.report) return;
      const allGroups = issueGroups();
      const files = groupedFiles();
      const visibleCount = files.reduce((total, file) => total + file.groups.length, 0);
      resultsSummary.innerHTML = `<strong>显示 ${visibleCount} / ${allGroups.length} 个问题位置</strong> · ${files.length} 个文件`;
      const emptyLabel = state.actionFilter === "all"
        ? "没有匹配的分析结果"
        : `没有${actionTypeText(state.actionFilter)}类分析结果`;
      resultStream.innerHTML = files.length
        ? files.map(fileGroupMarkup).join("")
        : `<div class="results-empty">${esc(emptyLabel)}</div>`;
      resultStream.querySelectorAll("input[data-file-check]").forEach(input => {
        const file = files.find(item => item.path === input.dataset.fileCheck);
        const applicable = file ? file.groups.filter(isGroupApplicable) : [];
        const selectedCount = applicable.filter(group => state.selectedGroups.has(group.id)).length;
        input.checked = applicable.length > 0 && selectedCount === applicable.length;
        input.indeterminate = selectedCount > 0 && selectedCount < applicable.length;
      });
      const visibleGroups = files.flatMap(file => file.groups);
      expandAllButton.disabled = !visibleGroups.length || visibleGroups.every(group => state.expandedGroups.has(group.id));
      collapseAllButton.disabled = !visibleGroups.some(group => state.expandedGroups.has(group.id));
      renderBatchBar();
    }

    function issueGroups() {
      const issues = (state.report && state.report.issues) || [];
      const logs = (state.report && state.report.logs) || [];
      const logsById = new Map(logs.map(log => [log.id, log]));
      const severityRank = { high: 3, medium: 2, low: 1 };
      const groups = new Map();
      issues.forEach(issue => {
        const id = issue.log_call_id || issue.fix?.id || issue.id;
        if (!groups.has(id)) groups.set(id, { id, issues: [], primary: issue, log: logsById.get(issue.log_call_id) || null });
        const group = groups.get(id);
        group.issues.push(issue);
        if ((severityRank[issue.severity] || 0) > (severityRank[group.primary.severity] || 0)) group.primary = issue;
      });
      return [...groups.values()].map(group => {
        const actionType = groupActionType(group);
        return {
          ...group,
          actionType,
          filePath: group.primary.file_path,
          line: Number(group.primary.line || 0),
          searchText: [
            group.primary.file_path,
            group.primary.line,
            actionTypeText(actionType),
            ...group.issues.flatMap(issue => [issue.title, ruleText(issue.kind), issue.reason, issue.suggestion])
          ].join(" ").toLocaleLowerCase("zh-CN")
        };
      });
    }

    function visibleIssueGroups() {
      const query = state.searchQuery.trim().toLocaleLowerCase("zh-CN");
      return issueGroups().filter(group => {
        const severityMatches = state.severityFilter === "all" || group.primary.severity === state.severityFilter;
        const actionMatches = state.actionFilter === "all" || group.actionType === state.actionFilter;
        return severityMatches && actionMatches && (!query || group.searchText.includes(query));
      });
    }

    function groupedFiles() {
      const severityRank = { high: 3, medium: 2, low: 1 };
      const files = new Map();
      visibleIssueGroups().forEach(group => {
        if (!files.has(group.filePath)) files.set(group.filePath, { path: group.filePath, groups: [], maxRank: 0 });
        const file = files.get(group.filePath);
        file.groups.push(group);
        file.maxRank = Math.max(file.maxRank, severityRank[group.primary.severity] || 0);
      });
      return [...files.values()].map(file => ({
        ...file,
        groups: file.groups.sort((left, right) =>
          (severityRank[right.primary.severity] || 0) - (severityRank[left.primary.severity] || 0) || left.line - right.line
        )
      })).sort((left, right) => right.maxRank - left.maxRank || left.path.localeCompare(right.path, "zh-CN"));
    }

    function patchIssueIds(group) {
      return group ? [...new Set(group.issues.filter(issue => issue.fix?.id).map(issue => issue.id))] : [];
    }

    function isGroupApplied(group) {
      return group.issues.some(issue => state.appliedIssueIds.has(issue.id));
    }

    function isGroupApplicable(group) {
      return state.reportActionable && !state.scanning && patchIssueIds(group).length > 0 && !isGroupApplied(group);
    }

    function issueActionType(issue) {
      const fixAction = issue.fix?.action;
      if (fixAction === "delete") return "delete";
      if (fixAction === "insert_before") return "add";
      if (fixAction === "replace") return issue.log_call_id ? "modify" : "add";
      if (issue.kind === "missing_exception_log" || issue.kind === "ai_missing_log") return "add";
      if (issue.patch_action === "delete" || issue.kind === "debug_log") return "delete";
      return "modify";
    }

    function groupActionType(group) {
      const exact = group.issues.find(issue => issue.fix?.id);
      if (exact) return issueActionType(exact);
      const actions = new Set(group.issues.map(issueActionType));
      if (actions.has("add")) return "add";
      if (actions.has("delete")) return "delete";
      return "modify";
    }

    function updateResultFilters() {
      severityFilters.querySelectorAll("button[data-severity]").forEach(button => {
        const active = button.dataset.severity === state.severityFilter;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      const groups = issueGroups();
      const counts = { all: groups.length, add: 0, delete: 0, modify: 0 };
      groups.forEach(group => { counts[group.actionType] = (counts[group.actionType] || 0) + 1; });
      actionFilters.querySelectorAll("button[data-action]").forEach(button => {
        const action = button.dataset.action;
        const active = action === state.actionFilter;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        const count = button.querySelector("[data-action-count]");
        if (count) count.textContent = String(counts[action] || 0);
      });
    }

    function renderBatchBar() {
      const selected = issueGroups().filter(group => state.selectedGroups.has(group.id) && isGroupApplicable(group));
      const files = new Set(selected.map(group => group.filePath));
      batchBar.classList.toggle("hidden", !selected.length);
      batchSelectionCount.textContent = `已选择 ${selected.length} 项`;
      batchSelectionFiles.textContent = `${files.size} 个文件`;
      batchApplyButton.disabled = !selected.length || state.applying;
      batchApplyButton.textContent = selected.length ? `批量采纳（${selected.length}）` : "批量采纳";
    }

    function fileGroupMarkup(file) {
      const collapsed = state.collapsedFiles.has(file.path);
      const exact = file.groups.filter(group => patchIssueIds(group).length > 0 && !isGroupApplied(group));
      const applicable = file.groups.filter(isGroupApplicable);
      const previewOnly = !state.reportActionable && exact.length > 0;
      const allExpanded = file.groups.every(group => state.expandedGroups.has(group.id));
      const anyExpanded = file.groups.some(group => state.expandedGroups.has(group.id));
      return `
        <section class="file-group ${collapsed ? "collapsed" : ""}" data-file-group="${esc(file.path)}">
          <div class="file-group-header">
            <button class="file-toggle" type="button" data-file-toggle="${esc(file.path)}" aria-expanded="${collapsed ? "false" : "true"}">
              <svg class="icon file-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
              <svg class="icon file-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span><span class="file-path">${esc(file.path)}</span><span class="file-count">${file.groups.length} 个问题位置 · ${exact.length} 项可采纳</span></span>
            </button>
            <div class="file-header-actions">
              <div class="file-fold-actions" role="group" aria-label="${esc(file.path)} 批量展开与折叠">
                <button class="secondary icon-only file-fold-button" type="button" data-file-expand-all="${esc(file.path)}" title="展开该文件全部问题" aria-label="展开该文件全部问题" ${allExpanded && !collapsed ? "disabled" : ""}><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 5 5 5-5"/><path d="m7 13 5 5 5-5"/></svg></button>
                <button class="secondary icon-only file-fold-button" type="button" data-file-collapse-all="${esc(file.path)}" title="折叠该文件全部问题" aria-label="折叠该文件全部问题" ${!anyExpanded ? "disabled" : ""}><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 17 5-5 5 5"/><path d="m7 11 5-5 5 5"/></svg></button>
              </div>
              <label class="file-select" title="${previewOnly ? state.scanning ? "分析完成后可选择" : "未完成的临时结果仅供预览" : applicable.length ? "选择该文件的全部精确修改" : "该文件没有精确修改"}"><input type="checkbox" data-file-check="${esc(file.path)}" ${applicable.length ? "" : "disabled"}>${previewOnly ? state.scanning ? "等待完成" : "仅预览" : applicable.length ? "选择可采纳项" : "无精确修改"}</label>
            </div>
          </div>
          <div class="file-results">${file.groups.map(resultItemMarkup).join("")}</div>
        </section>`;
    }

    function resultItemMarkup(group) {
      const issue = group.primary;
      const expanded = state.expandedGroups.has(group.id);
      const applied = isGroupApplied(group);
      const applicable = isGroupApplicable(group);
      const pending = !state.reportActionable && patchIssueIds(group).length > 0 && !applied;
      const pendingLabel = state.scanning ? "分析完成后可采纳" : "临时结果仅供预览";
      const pendingStatus = state.scanning ? "分析中" : "仅预览";
      const rules = [...new Set(group.issues.map(item => ruleText(item.kind)).filter(Boolean))];
      const reasons = uniqueText(group.issues.map(item => item.reason));
      const suggestions = uniqueText(group.issues.map(item => item.suggestion));
      const fixIssue = group.issues.find(item => item.fix?.id);
      const fix = fixIssue?.fix || null;
      return `
        <article class="result-item ${expanded ? "expanded" : ""} ${state.selectedGroups.has(group.id) ? "selected" : ""}">
          <div class="result-item-header">
            <label class="issue-select" title="${applicable ? "选择此修改" : pending ? pendingLabel : applied ? "该修改已采纳" : "当前问题没有精确修改"}"><input type="checkbox" data-group-check="${esc(group.id)}" ${state.selectedGroups.has(group.id) ? "checked" : ""} ${applicable ? "" : "disabled"}></label>
            <span class="pill ${esc(issue.severity)}">${esc(severityText(issue.severity))}</span>
            <button class="result-toggle" type="button" data-group-toggle="${esc(group.id)}" aria-expanded="${expanded ? "true" : "false"}">
              <span class="result-title-line"><span class="result-title">${esc(issue.title)}</span><span class="action-chip ${esc(group.actionType)}">${esc(actionTypeText(group.actionType))}</span>${applied ? '<span class="issue-status">已采纳</span>' : pending ? `<span class="issue-status muted">${pendingStatus}</span>` : !fix ? '<span class="issue-status muted">仅建议</span>' : ""}</span>
              <span class="result-rules">第 ${esc(issue.line)} 行 · ${esc(rules.join("、"))} · ${esc(sourceText(issue.source))}</span>
            </button>
            <svg class="icon result-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
          </div>
          ${expanded ? `
            <div class="result-content">
              <div class="finding-copy">
                <div class="copy-row"><span>原因</span><div>${reasons.map(value => `<p>${esc(value)}</p>`).join("") || "未提供"}</div></div>
                <div class="copy-row"><span>建议</span><div>${suggestions.map(value => `<p>${esc(value)}</p>`).join("") || "未提供"}</div></div>
              </div>
              <div class="inline-block"><div class="inline-block-header"><span>相关代码</span><span>${esc(issue.file_path)}:${esc(issue.line)}</span></div><div class="code-view">${codeContextMarkup(relatedCodeTextFor(group))}</div></div>
              ${fix ? `<div class="inline-block"><div class="inline-block-header"><span>修改预览</span><span>${esc(fixActionText(fix))} · ${esc(fixSourceText(fix.source))}</span></div><div class="diff-view inline-diff">${diffMarkup(fixPreview(fix))}</div></div>` : ""}
              ${fix ? `<div class="result-footer"><button type="button" data-apply-group="${esc(group.id)}" ${applicable ? "" : "disabled"}>${applied ? "已采纳" : pending ? pendingLabel : "采纳此修改"}</button></div>` : ""}
            </div>` : ""}
        </article>`;
    }

    function uniqueText(values) {
      return [...new Set(values.map(value => String(value || "").trim()).filter(Boolean))];
    }

    function relatedCodeTextFor(group) {
      const issue = group.primary;
      const context = issue.context || (group.log && group.log.context) || "";
      if (!context) return "当前报告没有保存源码上下文，请重新运行分析。";
      return context.split("\\n").map(line =>
        line.trimStart().startsWith(String(issue.line) + ":") ? "> " + line : "  " + line
      ).join("\\n");
    }

    function codeContextMarkup(value) {
      return String(value || "").split("\\n").map(line => {
        const target = line.startsWith("> ");
        const content = target || line.startsWith("  ") ? line.slice(2) : line;
        return `<span class="code-line ${target ? "target" : ""}">${esc(content || " ")}</span>`;
      }).join("");
    }

    function diffMarkup(value) {
      return String(value || "").split("\\n").map(line => {
        let type = "context";
        if (line.startsWith("--- ") || line.startsWith("+++ ")) type = "file";
        else if (line.startsWith("@@")) type = "hunk";
        else if (line.startsWith("+")) type = "add";
        else if (line.startsWith("-")) type = "remove";
        else if (line.startsWith("\\\\") || line.startsWith("#")) type = "note";
        return `<span class="diff-line ${type}">${esc(line || " ")}</span>`;
      }).join("");
    }

    function fixPreview(fix) {
      const removed = String(fix.expected_text || "").split("\\n").map(line => `- ${line}`).join("\\n");
      const added = String(fix.replacement_text || "").split("\\n").filter(line => line.length).map(line => `+ ${line}`).join("\\n");
      if (fix.action === "delete") return removed;
      if (fix.action === "replace") return `${removed}\\n${added}`;
      return added;
    }

    function fixActionText(fix) {
      return ({ delete: "删除日志", replace: "替换代码", insert_before: "补充日志" })[fix.action] || "修改代码";
    }

    function fixSourceText(source) {
      return ({ fixed: "固定模板", repository: "仓库风格", builtin: "内置模板", rule: "规则生成" })[source] || "精确修改";
    }

    function renderFullPatch(value) {
      fullPatchPre.innerHTML = diffMarkup(value);
    }

    function handleResultStreamClick(event) {
      const fileExpand = event.target.closest("button[data-file-expand-all]");
      if (fileExpand) {
        setFileGroupsExpanded(fileExpand.dataset.fileExpandAll, true);
        return;
      }
      const fileCollapse = event.target.closest("button[data-file-collapse-all]");
      if (fileCollapse) {
        setFileGroupsExpanded(fileCollapse.dataset.fileCollapseAll, false);
        return;
      }
      const fileToggle = event.target.closest("button[data-file-toggle]");
      if (fileToggle) {
        const path = fileToggle.dataset.fileToggle;
        if (state.collapsedFiles.has(path)) state.collapsedFiles.delete(path);
        else state.collapsedFiles.add(path);
        renderResultStream();
        return;
      }
      const groupToggle = event.target.closest("button[data-group-toggle]");
      if (groupToggle) {
        const id = groupToggle.dataset.groupToggle;
        if (state.expandedGroups.has(id)) state.expandedGroups.delete(id);
        else state.expandedGroups.add(id);
        renderResultStream();
        return;
      }
      const applyButton = event.target.closest("button[data-apply-group]");
      if (applyButton) {
        const group = issueGroups().find(item => item.id === applyButton.dataset.applyGroup);
        if (group) openApplyDialog(patchIssueIds(group));
      }
    }

    function setVisibleGroupsExpanded(expanded) {
      const groups = visibleIssueGroups();
      groups.forEach(group => {
        if (expanded) state.expandedGroups.add(group.id);
        else state.expandedGroups.delete(group.id);
        if (expanded) state.collapsedFiles.delete(group.filePath);
      });
      renderResultStream();
    }

    function setFileGroupsExpanded(path, expanded) {
      const file = groupedFiles().find(item => item.path === path);
      if (!file) return;
      file.groups.forEach(group => {
        if (expanded) state.expandedGroups.add(group.id);
        else state.expandedGroups.delete(group.id);
      });
      if (expanded) state.collapsedFiles.delete(path);
      renderResultStream();
    }

    function handleResultStreamChange(event) {
      const groupInput = event.target.closest("input[data-group-check]");
      if (groupInput) {
        if (groupInput.checked) state.selectedGroups.add(groupInput.dataset.groupCheck);
        else state.selectedGroups.delete(groupInput.dataset.groupCheck);
        renderResultStream();
        return;
      }
      const fileInput = event.target.closest("input[data-file-check]");
      if (!fileInput) return;
      const file = groupedFiles().find(item => item.path === fileInput.dataset.fileCheck);
      if (!file) return;
      file.groups.filter(isGroupApplicable).forEach(group => {
        if (fileInput.checked) state.selectedGroups.add(group.id);
        else state.selectedGroups.delete(group.id);
      });
      renderResultStream();
    }

    function renderDiagnostics() {
      const traces = (state.report && state.report.ai_traces) || [];
      const summary = document.querySelector("#diagnosticsSummary");
      if (!traces.length) state.diagnosticsOpen = false;
      diagnosticsToggle.disabled = !traces.length;
      diagnosticsToggle.querySelector("span").textContent = traces.length
        ? (state.diagnosticsOpen ? "收起诊断" : "查看诊断")
        : "暂无诊断";
      summary.textContent = traces.length
        ? traces.length + " 条运行记录，仅用于排查模型分析异常"
        : "当前结果未包含模型运行记录";
      diagnosticsPre.classList.toggle("hidden", !state.diagnosticsOpen || !traces.length);
      diagnosticsPre.textContent = traces.map(trace => [
        "运行时  " + (trace.runtime_id || "未知"),
        "版本    " + (trace.runtime_version || "未知"),
        "耗时    " + (trace.duration_ms || 0) + " ms",
        "状态    " + trace.status,
        "",
        "请求\\n" + (trace.prompt || "无请求内容"),
        "",
        "返回\\n" + (trace.error || trace.raw_response || "无返回内容")
      ].join("\\n")).join("\\n\\n----------------\\n\\n");
    }

    function severityText(value) {
      if (value === "high") return "高";
      if (value === "medium") return "中";
      if (value === "low") return "低";
      return value;
    }

    function actionTypeText(value) {
      if (value === "add") return "增加日志";
      if (value === "delete") return "删除日志";
      if (value === "modify") return "修改日志";
      return "全部动作";
    }

    function sourceText(value) {
      if (value === "rule") return "规则分析";
      if (String(value).startsWith("runtime:")) return `${String(value).slice(8)} 运行时`;
      return value || "未知来源";
    }

    function ruleText(value) {
      return ({
        forbidden_log: "禁用接口",
        debug_log: "调试日志",
        low_value_log: "低价值信息",
        sensitive_log: "敏感数据",
        duplicate_log: "重复信息",
        missing_exception_log: "异常记录",
        ai_log_quality: "AI 质量分析",
        ai_missing_log: "AI 缺失分析"
      })[value] || value;
    }

    function renderHistory(runs) {
      const target = document.querySelector("#historyList");
      if (!runs.length) {
        target.innerHTML = '<div class="empty">暂无历史记录</div>';
        return;
      }
      target.innerHTML = runs.map(run => {
        const sev = run.severity_counts || {};
        return `
          <div class="item history-row">
            <div>
              <h3>${esc(repositoryName(run.repository))}</h3>
              <div class="meta">${esc(formatTime(run.created_at))} · ${esc(run.repository)}</div>
            </div>
            <div class="history-score"><strong>${esc(run.score ?? "N/A")}</strong>${run.score === null || run.score === undefined ? "" : "<span> / 100</span>"}</div>
            <div class="history-stats">${esc(run.files_scanned)} / ${esc(run.discovered_files || run.files_scanned)} 文件 · ${esc(run.log_count)} 日志 · ${esc(run.issue_count)} 问题<br>${esc(run.runtime_id || "规则分析")} · 高 ${esc(sev.high || 0)} · 中 ${esc(sev.medium || 0)} · 低 ${esc(sev.low || 0)}</div>
            <button class="secondary" type="button" data-run-id="${esc(run.run_id)}"><span>查看</span><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button>
          </div>
        `;
      }).join("");
      target.querySelectorAll("button[data-run-id]").forEach(button => {
        button.addEventListener("click", () => loadHistoryRun(button.dataset.runId));
      });
    }

    function formatTime(value) {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value || "未知时间";
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    init();
  </script>
</body>
</html>"""
