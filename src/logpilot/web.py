from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .history import list_history_runs, load_history_run
from .config import load_config
from .locking import RepositoryBusyError, repository_operation_lock
from .parsers import LANGUAGE_BY_SUFFIX
from .patching import generate_patch
from .pipeline import run_scan
from .planning import build_scan_plan, load_scan_plan, save_scan_plan, selected_modules
from .remediation import (
    ApplyConflictError,
    ApplyNotFoundError,
    RemediationError,
    apply_status,
    apply_suggestions,
    rollback_apply,
)
from .runtime import RuntimeExecutor, RuntimeRegistry
from .result_store import RunResultStore
from .scanner import scan_repository_detailed
from .scan_jobs import ScanJob, TERMINAL_SCAN_STATUSES
from .settings import (
    SettingsError,
    build_language_profile,
    save_repository_settings,
    settings_payload,
)
from .storage import initialize_repository_storage, load_last_repository, repository_data_dir, save_last_repository


API_VERSION = 1
DESKTOP_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://tauri.localhost",
    "tauri://localhost",
}


def build_server(
    repo_root: Path | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
    *,
    auth_token: str | None = None,
    allow_shutdown: bool = False,
) -> ThreadingHTTPServer:
    initial_root = _initial_repository(repo_root)
    save_last_repository(initial_root)
    state: dict[str, Any] = {
        "repo_root": initial_root,
        "artifacts": repository_data_dir(initial_root),
        "runtime_id": "auto",
    }
    _mark_interrupted_runs(state["artifacts"])
    runtimes = runtime_registry or RuntimeRegistry()
    executor = runtime_executor or RuntimeExecutor()
    use_scan_subprocess = runtime_executor is None and runtime_registry is None
    scan_jobs: dict[str, ScanJob] = {}
    scan_jobs_lock = threading.Lock()
    scan_processes: dict[str, tuple[subprocess.Popen[str], Path]] = {}

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

    def execute_scan_subprocess(
        job: ScanJob,
        target: Path,
        runtime,
        plan_id: str,
        module_ids: list[str],
        retry_module_id: str = "",
        resume: bool = False,
    ) -> None:
        data_dir = initialize_repository_storage(target)
        cancel_file = data_dir / "runs" / job.run_id / "cancel.requested"
        cancel_file.parent.mkdir(parents=True, exist_ok=True)
        cancel_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "logpilot.scan_runner",
            "--repository",
            str(target),
            "--plan",
            plan_id,
            "--run",
            job.run_id,
            "--runtime",
            runtime.id,
            "--cancel-file",
            str(cancel_file),
        ]
        for module_id in module_ids:
            command.extend(["--module", module_id])
        if retry_module_id:
            command.extend(["--retry-module", retry_module_id])
        if resume:
            command.append("--resume")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        source_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            env=env,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        with scan_jobs_lock:
            scan_processes[job.id] = (process, cancel_file)
        terminal_seen = False
        try:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = payload.get("type")
                if kind == "progress" and isinstance(payload.get("event"), dict):
                    job.update(payload["event"])
                elif kind == "completed":
                    terminal_seen = True
                    activate_repository(target, runtime.id)
                    job.complete(None, job.run_id)
                elif kind == "cancelled":
                    terminal_seen = True
                    job.cancel()
                elif kind == "failed":
                    terminal_seen = True
                    job.fail(str(payload.get("error", "分析进程失败。")))
            return_code = process.wait()
            if not terminal_seen:
                error = process.stderr.read().strip() if process.stderr else ""
                if job.should_cancel() or return_code == 2:
                    job.cancel()
                else:
                    job.fail(error or f"分析进程异常退出（exit code {return_code}）。")
        except Exception as exc:
            if job.should_cancel():
                job.cancel()
            else:
                job.fail(str(exc))
        finally:
            cancel_file.unlink(missing_ok=True)
            with scan_jobs_lock:
                scan_processes.pop(job.id, None)

    def stop_scan_process(job_id: str) -> None:
        with scan_jobs_lock:
            entry = scan_processes.get(job_id)
        if not entry:
            return
        process, cancel_file = entry
        cancel_file.touch(exist_ok=True)

        def force_stop() -> None:
            time.sleep(2)
            if process.poll() is not None:
                return
            _terminate_process_tree(process)

        threading.Thread(target=force_stop, daemon=True, name=f"logpilot-cancel-{job_id[:8]}").start()

    def stop_all_scan_processes() -> None:
        with scan_jobs_lock:
            entries = list(scan_processes.values())
        for process, cancel_file in entries:
            cancel_file.touch(exist_ok=True)
            if process.poll() is None:
                _terminate_process_tree(process)

    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-LogPilot-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
                return
            if parsed.path == "/":
                self._send("text/html; charset=utf-8", _html())
            elif parsed.path.startswith("/assets/"):
                self._send_ui_asset(parsed.path.removeprefix("/"))
            elif parsed.path == "/api/health":
                self._send_json({"status": "ok", "api_version": API_VERSION})
            elif parsed.path == "/api/meta":
                self._send_json(
                    {
                        "name": "LogPilot Engine",
                        "api_version": API_VERSION,
                        "shutdown_enabled": allow_shutdown,
                    }
                )
            elif parsed.path == "/api/state":
                payload = _state_payload(state)
                with scan_jobs_lock:
                    active_jobs = [job for job in scan_jobs.values() if not job.is_terminal]
                payload["active_scan"] = active_jobs[-1].snapshot() if active_jobs else None
                self._send_json(payload)
            elif parsed.path == "/api/report":
                self._send_latest_report()
            elif parsed.path == "/api/patch":
                self._send_latest_patch(parsed.query)
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
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/issues"):
                self._send_run_issues(parsed.path, parsed.query)
            elif parsed.path.startswith("/api/runs/"):
                self._send_run_detail(parsed.path)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
                return
            if parsed.path == "/api/shutdown":
                if not allow_shutdown:
                    self._send_json({"error": "当前服务不允许远程关闭。"}, HTTPStatus.FORBIDDEN)
                    return
                self._send_json({"status": "stopping"})
                threading.Thread(target=self._shutdown_server, daemon=True).start()
            elif parsed.path == "/api/scans":
                self._handle_scan_start()
            elif parsed.path == "/api/scan/plans":
                self._handle_scan_plan()
            elif parsed.path.startswith("/api/scans/") and parsed.path.endswith("/cancel"):
                self._handle_scan_cancel(parsed.path)
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/retry"):
                self._handle_module_retry(parsed.path)
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

        def _authorize_api(self, path: str) -> bool:
            if path == "/api/health" or not auth_token:
                return True
            provided = self.headers.get("X-LogPilot-Token", "")
            if secrets.compare_digest(provided, auth_token):
                return True
            self._send_json({"error": "LogPilot Engine authentication failed."}, HTTPStatus.UNAUTHORIZED)
            return False

        def _shutdown_server(self) -> None:
            stop_all_scan_processes()
            self.server.shutdown()

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
                            if not job.is_terminal
                        ),
                        None,
                    )
                    if active:
                        self._send_json(
                            {"error": "已有分析任务正在运行。LogPilot 同时只执行一个仓库扫描。", "job": active.snapshot()},
                            HTTPStatus.CONFLICT,
                        )
                        return
                    resume_run_id = str(payload.get("resume_run_id", "")).strip()
                    if resume_run_id:
                        resume_run_id = _safe_api_id(resume_run_id)
                        database = state["artifacts"] / "runs" / resume_run_id / "results.sqlite3"
                        if not database.is_file():
                            raise FileNotFoundError(f"分析记录不存在：{resume_run_id}")
                        resume_store = RunResultStore(database)
                        with resume_store.connection() as connection:
                            row = connection.execute("SELECT plan_id FROM runs WHERE run_id=?", (resume_run_id,)).fetchone()
                        if row is None:
                            raise FileNotFoundError(f"分析记录不存在：{resume_run_id}")
                        plan = load_scan_plan(target, str(row[0]))
                        module_ids = resume_store.selected_module_ids()
                    else:
                        requested_plan_id = str(payload.get("plan_id", "")).strip()
                        if requested_plan_id:
                            plan = load_scan_plan(target, requested_plan_id)
                        else:
                            config = load_config(target)
                            include_large_files = bool(payload.get("include_large_files", False))
                            plan = build_scan_plan(target, config.scan, include_large_files=include_large_files)
                            save_scan_plan(plan)
                        raw_module_ids = payload.get("module_ids", [])
                        if not isinstance(raw_module_ids, list):
                            raise ValueError("module_ids 必须是数组。")
                        module_ids = [str(item) for item in raw_module_ids]
                        selected_modules(plan, module_ids)
                    if plan.source_files == 0:
                        self._send_json(
                            {"error": "当前目录未发现源码文件，请确认仓库路径或 .logpilot.yaml 排除规则。"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                        return
                    if not plan.modules:
                        self._send_json(
                            {"error": "发现了源码候选，但没有可分析文件。请检查扩展名配置或超大文件限制。"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                        return
                    job = ScanJob(repository, selected_runtime.id)
                    job.run_id = resume_run_id or _new_web_run_id()
                    scan_jobs[job.id] = job
                    completed_jobs = [
                        item for item in scan_jobs.values() if item.status in TERMINAL_SCAN_STATUSES
                    ]
                    for old_job in completed_jobs[:-20]:
                        scan_jobs.pop(old_job.id, None)
                worker = threading.Thread(
                    target=execute_scan_subprocess if use_scan_subprocess else execute_scan_job,
                    args=(job, target, selected_runtime, plan.id, module_ids, "", bool(resume_run_id)) if use_scan_subprocess else (job, target, selected_runtime),
                    daemon=True,
                    name=f"logpilot-scan-{job.id[:8]}",
                )
                worker.start()
                self._send_json(
                    {"job": job.snapshot(), "runtime": selected_runtime.to_dict(), "plan": plan.to_dict()},
                    HTTPStatus.ACCEPTED,
                )
            except Exception as exc:  # Keep the local UI useful during early scanner work.
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_scan_plan(self) -> None:
            try:
                payload = self._read_json()
                target = _validate_repository(_resolve_repo_path(str(payload.get("path", ""))))
                config = load_config(target)
                plan = build_scan_plan(
                    target,
                    config.scan,
                    include_large_files=bool(payload.get("include_large_files", False)),
                )
                save_scan_plan(plan)
                self._send_json({"plan": plan.to_dict()})
            except (OSError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _send_run_issues(self, path: str, query: str) -> None:
            parts = [part for part in path.split("/") if part]
            if len(parts) != 4 or parts[:2] != ["api", "runs"] or parts[-1] != "issues":
                self._send_json({"error": "无效的问题查询路径。"}, HTTPStatus.NOT_FOUND)
                return
            try:
                run_id = _safe_api_id(parts[2])
                params = parse_qs(query)
                database = state["artifacts"] / "runs" / run_id / "results.sqlite3"
                if not database.is_file():
                    raise FileNotFoundError(f"分析记录不存在：{run_id}")
                store = RunResultStore(database)
                payload = store.query_issues(
                    module_id=params.get("module", [""])[0],
                    severity=params.get("severity", [""])[0],
                    action=params.get("action", [""])[0],
                    search=params.get("search", [""])[0],
                    limit=int(params.get("limit", ["100"])[0]),
                    offset=int(params.get("offset", ["0"])[0]),
                )
                self._send_json(payload)
            except (FileNotFoundError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)

        def _send_run_detail(self, path: str) -> None:
            parts = [part for part in path.split("/") if part]
            if len(parts) != 3 or parts[:2] != ["api", "runs"]:
                self._send_json({"error": "无效的分析记录路径。"}, HTTPStatus.NOT_FOUND)
                return
            try:
                run_id = _safe_api_id(parts[2])
                database = state["artifacts"] / "runs" / run_id / "results.sqlite3"
                if not database.is_file():
                    run = load_history_run(state["artifacts"], run_id)
                    self._send_json({
                        "run_id": run_id,
                        "status": "completed",
                        "summary": run["report"].get("summary", {}),
                        "modules": [],
                        "parse_failures": run["report"].get("parse_failures", [])[:5],
                        "parse_failure_count": len(run["report"].get("parse_failures", [])),
                        "language_insights": run["report"].get("language_insights", []),
                        "legacy": True,
                    })
                    return
                self._send_json(RunResultStore(database).run_detail())
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)

        def _handle_module_retry(self, path: str) -> None:
            parts = [part for part in path.split("/") if part]
            if len(parts) != 6 or parts[:2] != ["api", "runs"] or parts[3] != "modules" or parts[-1] != "retry":
                self._send_json({"error": "无效的目录重试路径。"}, HTTPStatus.NOT_FOUND)
                return
            try:
                run_id = _safe_api_id(parts[2])
                module_id = _safe_api_id(parts[4])
                payload = self._read_json()
                requested_runtime = str(payload.get("runtime", state.get("runtime_id", "auto"))).strip() or "auto"
                selected_runtime = runtimes.resolve(requested_runtime)
                database = state["artifacts"] / "runs" / run_id / "results.sqlite3"
                if not database.is_file():
                    raise FileNotFoundError(f"分析记录不存在：{run_id}")
                store = RunResultStore(database)
                with store.connection() as connection:
                    row = connection.execute("SELECT plan_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(f"分析记录不存在：{run_id}")
                with scan_jobs_lock:
                    active = next((item for item in scan_jobs.values() if not item.is_terminal), None)
                    if active:
                        self._send_json({"error": "已有分析任务正在运行。", "job": active.snapshot()}, HTTPStatus.CONFLICT)
                        return
                    job = ScanJob(str(state["repo_root"]), selected_runtime.id)
                    job.run_id = run_id
                    scan_jobs[job.id] = job
                worker = threading.Thread(
                    target=execute_scan_subprocess,
                    args=(job, state["repo_root"], selected_runtime, str(row[0]), [], module_id),
                    daemon=True,
                    name=f"logpilot-retry-{job.id[:8]}",
                )
                worker.start()
                self._send_json({"job": job.snapshot()}, HTTPStatus.ACCEPTED)
            except FileNotFoundError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _send_latest_report(self) -> None:
            history = list_history_runs(state["artifacts"])
            if not history:
                self._send_json({"error": "当前仓库还没有分析结果。"}, HTTPStatus.NOT_FOUND)
                return
            try:
                run = load_history_run(state["artifacts"], str(history[0]["run_id"]))
                self._send_json(run["report"])
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)

        def _send_latest_patch(self, query: str = "") -> None:
            history = list_history_runs(state["artifacts"])
            if not history:
                self._send_json({"error": "当前仓库还没有分析结果。"}, HTTPStatus.NOT_FOUND)
                return
            requested = parse_qs(query).get("run_id", [])
            run_id = _safe_api_id(requested[0]) if requested else str(history[0]["run_id"])
            try:
                run = load_history_run(state["artifacts"], run_id)
                patch = run["patch"]
                if not patch:
                    from .result_store import report_from_dict

                    report = report_from_dict(run["report"])
                    patch = generate_patch(state["repo_root"], report.logs, report.issues)
                    (state["artifacts"] / "runs" / run_id / "changes.diff").write_text(patch, encoding="utf-8")
                self._send("text/plain; charset=utf-8", patch)
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)

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
            if use_scan_subprocess:
                stop_scan_process(job.id)
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
            self._send_bytes(content_type, body.encode("utf-8"), status)

        def _send_ui_asset(self, relative_path: str) -> None:
            try:
                path = _ui_asset(relative_path)
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            content_type, _ = mimetypes.guess_type(path.name)
            self._send_bytes(content_type or "application/octet-stream", path.read_bytes())

        def _send_bytes(
            self,
            content_type: str,
            data: bytes,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(data)

        def _send_cors_headers(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin in DESKTOP_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

    server = ThreadingHTTPServer((host, port), Handler)
    server.shutdown_active_processes = stop_all_scan_processes  # type: ignore[attr-defined]
    return server


def serve(
    repo_root: Path | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auth_token: str | None = None,
    allow_shutdown: bool = False,
) -> None:
    initial_root = _initial_repository(repo_root)
    server = build_server(
        initial_root,
        host,
        port,
        auth_token=auth_token,
        allow_shutdown=allow_shutdown,
    )
    artifacts = repository_data_dir(initial_root)
    actual_port = int(server.server_address[1])
    print(f"LogPilot UI: http://{host}:{actual_port}")
    print(f"Default repository: {initial_root}")
    print(f"Reading artifacts from: {artifacts}")
    try:
        server.serve_forever()
    finally:
        server.shutdown_active_processes()  # type: ignore[attr-defined]
        server.server_close()


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


def _resolve_repo_path(raw_path: str) -> Path:
    if not raw_path.strip():
        raise ValueError("请先输入仓库路径。")
    return Path(raw_path).expanduser().resolve()


def _state_payload(state: dict[str, Any]) -> dict[str, object]:
    artifacts = state["artifacts"]
    history = list_history_runs(artifacts)
    return {
        "repository": str(state["repo_root"]),
        "has_report": bool(history),
        "history": history,
        "runtime_id": state.get("runtime_id", "auto"),
    }


def _safe_api_id(value: str) -> str:
    if not value or any(char in value for char in "\\/.: "):
        raise ValueError("无效的资源 ID。")
    return value


def _new_web_run_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=1,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        try:
            process.terminate()
        except OSError:
            pass


def _mark_interrupted_runs(data_dir: Path) -> None:
    runs_dir = data_dir / "runs"
    if not runs_dir.is_dir():
        return
    for database in runs_dir.glob("*/results.sqlite3"):
        try:
            store = RunResultStore(database)
            progress = store.progress()
            if progress.get("status") != "running":
                continue
            store.mark_running_interrupted()
            metadata_path = database.parent / "metadata.json"
            if metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["status"] = "interrupted"
                metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError, json.JSONDecodeError):
            continue


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


_UI_ASSET_ROOT = Path(__file__).with_name("web_assets")


def _ui_asset(relative_path: str) -> Path:
    root = _UI_ASSET_ROOT.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid UI asset path")
    return path


def _html() -> str:
    return _ui_asset("index.html").read_text(encoding="utf-8")
