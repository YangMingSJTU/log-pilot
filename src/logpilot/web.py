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
from .pipeline import run_scan
from .runtime import RuntimeExecutor, RuntimeRegistry


def build_server(
    repo_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
) -> ThreadingHTTPServer:
    initial_root = repo_root.resolve()
    state: dict[str, Any] = {
        "repo_root": initial_root,
        "artifacts": initial_root / ".logpilot",
        "runtime_id": "auto",
    }
    runtimes = runtime_registry or RuntimeRegistry()
    executor = runtime_executor or RuntimeExecutor()
    browse_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send("text/html; charset=utf-8", _html())
            elif parsed.path == "/api/state":
                self._send_json(_state_payload(state))
            elif parsed.path == "/api/report":
                self._send_file(state["artifacts"] / "report.json", "application/json; charset=utf-8")
            elif parsed.path == "/api/patch":
                self._send_file(state["artifacts"] / "changes.diff", "text/plain; charset=utf-8")
            elif parsed.path == "/api/history":
                self._send_json({"repository": str(state["repo_root"]), "runs": list_history_runs(state["artifacts"])})
            elif parsed.path == "/api/history/run":
                self._send_history_run(parsed.query)
            elif parsed.path == "/api/runtimes":
                self._send_json(_runtime_payload(runtimes, state, refresh=False))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/scan":
                self._handle_scan()
            elif parsed.path == "/api/browse":
                self._handle_browse()
            elif parsed.path == "/api/runtimes/refresh":
                self._send_json(_runtime_payload(runtimes, state, refresh=True))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args) -> None:
            return

        def _handle_scan(self) -> None:
            try:
                payload = self._read_json()
                target = _resolve_repo_path(str(payload.get("path", "")))
                if not target.exists() or not target.is_dir():
                    self._send_json({"error": f"仓库路径不存在：{target}"}, HTTPStatus.BAD_REQUEST)
                    return

                requested_runtime = str(payload.get("runtime", "auto")).strip() or "auto"
                selected_runtime = runtimes.resolve(requested_runtime)
                report = run_scan(
                    target,
                    runtime_id=selected_runtime.id,
                    runtime_registry=runtimes,
                    runtime_executor=executor,
                )
                state["repo_root"] = target.resolve()
                state["artifacts"] = target.resolve() / ".logpilot"
                state["runtime_id"] = selected_runtime.id
                history = list_history_runs(state["artifacts"])
                self._send_json(
                    {
                        "repository": str(target.resolve()),
                        "report": report.to_dict(),
                        "history": history,
                        "run": history[0] if history else None,
                        "runtime": selected_runtime.to_dict(),
                    }
                )
            except Exception as exc:  # Keep the local UI useful during early scanner work.
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_browse(self) -> None:
            if not browse_lock.acquire(blocking=False):
                self._send_json(
                    {"error": "已有选择窗口打开，请先关闭或稍后再试。"},
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                selected = choose_directory(state["repo_root"])
                if not selected:
                    self._send_json({"cancelled": True, "path": ""})
                    return
                self._send_json({"cancelled": False, "path": str(selected)})
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            finally:
                browse_lock.release()

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


def serve(repo_root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = build_server(repo_root, host, port)
    artifacts = repo_root.resolve() / ".logpilot"
    print(f"LogPilot UI: http://{host}:{port}")
    print(f"Default repository: {repo_root.resolve()}")
    print(f"Reading artifacts from: {artifacts}")
    server.serve_forever()


def choose_directory(initial_dir: Path) -> Path | None:
    return _choose_directory_tk_subprocess(initial_dir)


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
    button, input, select { font: inherit; }
    button, input, .runtime-control, .nav-item, .issue-row {
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
      grid-template-rows: 64px minmax(0, 1fr);
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
    .topbar {
      grid-column: 2;
      grid-row: 1;
      min-height: 64px;
      display: grid;
      grid-template-columns: minmax(360px, 1fr) 180px auto;
      gap: 12px;
      align-items: center;
      padding: 11px 22px;
      border-bottom: 1px solid var(--line);
      background: #0d0d0e;
      backdrop-filter: none;
    }
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
      grid-row: 2;
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
      grid-template-columns: 1.25fr repeat(4, minmax(108px, 1fr));
      gap: 10px;
    }
    .score-panel, .metric {
      min-height: 96px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025), 0 1px 2px rgba(0, 0, 0, .16);
    }
    .metric-label {
      color: var(--muted);
      font-size: 11px;
    }
    .score-line { display: flex; align-items: baseline; gap: 8px; }
    .score-line strong, .metric strong {
      margin: 0;
      color: #fff;
      font-size: 25px;
      line-height: 1;
      font-weight: 720;
    }
    .score-line span { margin: 0; color: var(--muted); font-size: 11px; }
    .score-track { height: 3px; border-radius: 999px; background: #25252a; overflow: hidden; }
    .score-track i {
      display: block;
      width: calc(var(--score, 0) * 1%);
      height: 100%;
      background: var(--accent);
      border-radius: inherit;
    }
    .metric span { margin: 0; }
    .workspace-section { margin-top: 28px; }
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
    .legend { display: flex; align-items: center; gap: 14px; color: var(--muted); font-size: 10px; }
    .legend span { display: flex; align-items: center; gap: 6px; }
    .legend i { width: 6px; height: 6px; border-radius: 50%; }
    .legend .high-dot { background: var(--red); }
    .legend .medium-dot { background: var(--amber); }
    .legend .low-dot { background: var(--green); }
    .analysis-workspace {
      height: max(420px, calc(100dvh - 324px));
      display: grid;
      grid-template-columns: minmax(320px, .82fr) minmax(470px, 1.18fr);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .02), 0 2px 8px rgba(0, 0, 0, .14);
      overflow: hidden;
    }
    .issue-pane, .detail {
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .issue-pane { border-right: 1px solid var(--line); }
    .pane-heading {
      flex: 0 0 38px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      border-bottom: 1px solid var(--line);
      background: #121214;
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .list { flex: 1; max-height: none; overflow: auto; }
    .issue-row {
      width: 100%;
      height: auto;
      min-height: 72px;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      padding: 13px 16px;
      border: 0;
      border-bottom: 1px solid var(--line-soft);
      border-radius: 0;
      background: transparent;
      color: var(--ink);
      text-align: left;
      white-space: normal;
    }
    .issue-row:hover { background: #171719; }
    .issue-row.active {
      background: var(--accent-soft);
      box-shadow: inset 2px 0 0 #a78bfa;
    }
    .issue-title { display: block; margin: 0 0 7px; font-size: 13px; font-weight: 650; }
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
    .detail { min-height: 0; }
    .detail-body {
      flex: 0 0 auto;
      max-height: 230px;
      display: grid;
      padding: 18px 20px;
      gap: 13px;
      border-bottom: 1px solid var(--line);
      overflow: auto;
      background: #101012;
    }
    .detail-body h3 { margin: 0; font-size: 16px; }
    .kv { display: grid; grid-template-columns: 58px 1fr; gap: 8px; font-size: 12px; line-height: 1.55; }
    .kv span:first-child { color: var(--subtle); }
    .section-tabs {
      flex: 0 0 auto;
      display: flex;
      gap: 3px;
      padding: 5px 12px;
      border-top: 1px solid var(--line-soft);
      border-bottom: 1px solid var(--line);
      background: var(--surface-2);
    }
    .mini-tab {
      height: 30px;
      padding: 0 10px;
      border: 0;
      border-radius: 5px;
      background: transparent;
      color: var(--muted);
      font-size: 11px;
      box-shadow: none;
    }
    .mini-tab:hover { border-color: transparent; background: #1d1d21; color: var(--ink); }
    .mini-tab.active {
      border: 0;
      background: #272230;
      color: #fff;
      box-shadow: inset 0 0 0 1px rgba(167, 139, 250, .22), 0 1px 2px rgba(0, 0, 0, .24);
    }
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
    .dialog pre { min-height: 320px; max-height: calc(100dvh - 108px); flex: 1; }
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

    @media (max-width: 1080px) {
      .app-shell { grid-template-columns: 190px minmax(0, 1fr); }
      .topbar, main { grid-column: 2; }
      .topbar { grid-template-columns: minmax(0, 1fr) auto; }
      .runtime-control { display: none; }
      .view-panel { padding-left: 28px; padding-right: 28px; }
      .analysis-workspace { grid-template-columns: minmax(300px, .85fr) minmax(410px, 1.15fr); }
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
      .topbar {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        padding: 10px 16px;
      }
      main { overflow: visible; }
      .view-panel { padding: 22px 20px 32px; }
      .analysis-workspace { height: auto; grid-template-columns: 1fr; }
      .issue-pane { height: 390px; border-right: 0; border-bottom: 1px solid var(--line); }
      .detail { min-height: 560px; }
      .history-header { display: none; }
      .history-row { grid-template-columns: 1fr auto; }
      .history-score, .history-stats { display: none; }
      .toast-region { top: auto; right: 16px; bottom: 16px; }
      .runtime-header { display: none; }
      .runtime-row { grid-template-columns: minmax(150px, 1fr) auto; }
      .runtime-row .runtime-value { display: none; }
      .section-actions { gap: 8px; }
      .legend { display: none; }
    }
    @media (max-width: 560px) {
      .sidebar { grid-template-columns: 1fr; grid-template-rows: 56px auto; }
      .brand { height: 56px; padding: 0 16px; }
      .side-nav { justify-content: stretch; padding: 8px; border-top: 1px solid var(--line-soft); }
      .nav-item { flex: 1; justify-items: center; grid-template-columns: 18px auto; }
      .topbar { grid-template-columns: 1fr; }
      .repo-control { grid-template-columns: 1fr auto; }
      #scanButton { width: 100%; }
      .summary-grid { grid-template-columns: 1fr 1fr; }
      .score-panel { grid-column: 1 / -1; }
      .summary-grid > * { border: 1px solid var(--line); }
      .compact-action span { display: none; }
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
        <button class="nav-item active" id="currentTab" type="button"><span class="nav-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/></svg></span><span>分析概览</span></button>
        <button class="nav-item" id="historyTab" type="button"><span class="nav-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg></span><span>历史记录</span></button>
        <button class="nav-item" id="settingsTab" type="button"><span class="nav-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.09a2 2 0 0 1-1-1.74v-.51a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg></span><span>设置</span></button>
      </nav>
      <div class="sidebar-footer"><span class="local-dot"></span><span>本地模式</span></div>
    </aside>
    <header class="topbar">
      <div class="repo-control">
        <input id="repoPath" type="text" spellcheck="false" aria-label="仓库路径" placeholder="D:\\GitHub\\log-pilot">
        <button class="secondary" id="browseButton" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/></svg><span>选择仓库</span></button>
      </div>
      <label class="runtime-control" title="选择执行日志分析的本机运行时">
        <span class="state-dot offline" id="runtimeDot"></span>
        <select id="runtimeSelect" aria-label="分析运行时"><option value="">检测运行时...</option></select>
      </label>
      <button id="scanButton" type="button"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 3 14 9-14 9z"/></svg><span>开始分析</span></button>
    </header>
    <div class="toast-region" id="toastRegion" aria-live="polite" aria-atomic="true"></div>
    <main>
      <div class="view-panel" id="currentPanel">
        <section class="summary-section"><div class="summary-grid" id="metrics"></div></section>
        <section class="workspace-section">
          <div class="section-bar">
            <div class="section-title"><h2>问题清单</h2></div>
            <div class="section-actions">
              <div class="legend"><span><i class="high-dot"></i>高风险</span><span><i class="medium-dot"></i>中风险</span><span><i class="low-dot"></i>低风险</span></div>
              <button class="secondary compact-action" id="fullPatchButton" type="button" title="查看本次分析生成的全部安全修改"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><polyline points="14 2 14 8 20 8"/><path d="m9 15 2 2 4-4"/></svg><span>完整修改</span></button>
            </div>
          </div>
          <div class="analysis-workspace">
            <section class="issue-pane">
              <div class="pane-heading"><span>检测结果</span><span>文件位置</span></div>
              <div class="list" id="issues"></div>
            </section>
            <section class="detail">
              <div class="pane-heading"><span>问题详情</span><span>处理建议</span></div>
            <div class="detail-body" id="issueDetail"></div>
            <div class="section-tabs">
              <button class="mini-tab active" id="sourceTab" type="button">相关代码</button>
              <button class="mini-tab" id="patchTab" type="button">修改预览</button>
            </div>
            <pre id="detailPre">等待分析结果</pre>
            </section>
          </div>
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
        <div class="section-bar"><div class="section-title"><h2>运行时</h2><span class="section-count">选择日志分析使用的本机执行环境</span></div></div>
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
  <div class="dialog-backdrop hidden" id="fullPatchDialog" role="dialog" aria-modal="true" aria-labelledby="fullPatchTitle">
    <section class="dialog">
      <header class="dialog-header">
        <div class="dialog-title"><h2 id="fullPatchTitle">完整修改</h2><p>本次分析生成的全部安全修改，仅供审查，不会自动写入源码</p></div>
        <button class="secondary icon-only" id="closePatchDialog" type="button" title="关闭"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      </header>
      <pre id="fullPatchPre">暂无修改内容</pre>
    </section>
  </div>
  <script>
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[char]));
    const state = {
      path: "",
      scanning: false,
      browsing: false,
      history: [],
      report: null,
      patch: "",
      selectedIssueId: "",
      detailMode: "source",
      diagnosticsOpen: false,
      runtimes: [],
      selectedRuntime: "",
      activeView: "current"
    };
    const repoPath = document.querySelector("#repoPath");
    const browseButton = document.querySelector("#browseButton");
    const scanButton = document.querySelector("#scanButton");
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
    const sourceTab = document.querySelector("#sourceTab");
    const patchTab = document.querySelector("#patchTab");
    const fullPatchButton = document.querySelector("#fullPatchButton");
    const fullPatchDialog = document.querySelector("#fullPatchDialog");
    const fullPatchPre = document.querySelector("#fullPatchPre");
    const closePatchDialog = document.querySelector("#closePatchDialog");
    const diagnosticsToggle = document.querySelector("#diagnosticsToggle");
    const diagnosticsPre = document.querySelector("#diagnosticsPre");

    scanButton.addEventListener("click", () => startScan(repoPath.value));
    browseButton.addEventListener("click", browseRepository);
    repoPath.addEventListener("keydown", event => {
      if (event.key === "Enter") startScan(repoPath.value);
    });
    currentTab.addEventListener("click", () => showTab("current"));
    historyTab.addEventListener("click", () => showTab("history"));
    settingsTab.addEventListener("click", () => showTab("settings"));
    refreshRuntimesButton.addEventListener("click", () => loadRuntimes(true));
    runtimeSelect.addEventListener("change", () => {
      state.selectedRuntime = runtimeSelect.value;
      window.localStorage.setItem("logpilot.runtime", state.selectedRuntime);
      renderRuntimes();
    });
    sourceTab.addEventListener("click", () => setDetailMode("source"));
    patchTab.addEventListener("click", () => setDetailMode("patch"));
    fullPatchButton.addEventListener("click", openFullPatch);
    closePatchDialog.addEventListener("click", closeFullPatch);
    fullPatchDialog.addEventListener("click", event => {
      if (event.target === fullPatchDialog) closeFullPatch();
    });
    diagnosticsToggle.addEventListener("click", toggleDiagnostics);
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !fullPatchDialog.classList.contains("hidden")) closeFullPatch();
    });

    async function init() {
      try {
        const [stateResponse] = await Promise.all([fetch("/api/state"), loadRuntimes(false)]);
        const payload = await stateResponse.json();
        if (!stateResponse.ok || payload.error) throw new Error(payload.error || "状态读取失败");
        state.path = payload.repository || "";
        state.history = payload.history || [];
        repoPath.value = state.path;
        updateRepositoryIdentity(state.path);
        renderHistory(state.history);
        if (payload.has_report) await loadReport();
        else renderEmpty();
      } catch (error) {
        showToast(`初始化失败：${error.message}`, "error");
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
        if (refresh) showToast(`刷新失败：${error.message}`, "error");
      } finally {
        refreshRuntimesButton.disabled = false;
      }
    }

    async function browseRepository() {
      if (state.browsing) return;
      setBrowsing(true);
      showToast("正在打开仓库选择器...", "info", 0);
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 125000);
      try {
        const response = await fetch("/api/browse", { method: "POST", signal: controller.signal });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "选择仓库失败");
        if (payload.cancelled) {
          showToast("已取消选择", "info");
          return;
        }
        state.path = payload.path;
        repoPath.value = payload.path;
        updateRepositoryIdentity(state.path);
        showToast("仓库路径已更新", "success");
      } catch (error) {
        const message = error.name === "AbortError" ? "选择窗口超时，请手动输入路径或重试。" : error.message;
        showToast(`选择失败：${message}`, "error");
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
      const runtime = selectedRuntime();
      if (!runtime) {
        showToast("没有可用运行时，请先在运行时页面检查 Codex 或 Claude", "warning");
        return;
      }
      setScanning(true);
      showToast(`正在通过 ${runtime.name} 分析仓库...`, "info", 0);
      try {
        const response = await fetch("/api/scan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target, runtime: runtime.id })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "扫描失败");
        state.path = payload.repository;
        state.history = payload.history || [];
        repoPath.value = state.path;
        updateRepositoryIdentity(state.path);
        renderReport(payload.report);
        renderHistory(state.history);
        await loadPatch();
        showTab("current");
        showToast(`${runtime.name} 分析完成，结果已更新`, "success");
      } catch (error) {
        showToast(`分析失败：${error.message}`, "error");
      } finally {
        setScanning(false);
      }
    }

    async function loadReport() {
      const reportResponse = await fetch("/api/report");
      const report = await reportResponse.json();
      if (!reportResponse.ok || report.error) throw new Error(report.error || "报告读取失败");
      renderReport(report);
      await loadPatch();
    }

    async function loadPatch() {
      const response = await fetch("/api/patch");
      const text = await response.text();
      state.patch = response.ok ? text : "暂无补丁产物。";
      fullPatchButton.disabled = false;
      fullPatchPre.textContent = state.patch;
    }

    async function loadHistoryRun(runId) {
      showToast("正在读取历史分析...", "info", 0);
      try {
        const response = await fetch(`/api/history/run?run_id=${encodeURIComponent(runId)}`);
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "历史记录读取失败");
        renderReport(payload.report);
        state.patch = payload.patch || "暂无补丁产物。";
        fullPatchButton.disabled = false;
        fullPatchPre.textContent = state.patch;
        if (payload.metadata && payload.metadata.repository) {
          updateRepositoryIdentity(payload.metadata.repository);
        }
        renderDetailContent();
        showTab("current");
        showToast("历史分析已加载", "success");
      } catch (error) {
        showToast(`历史记录读取失败：${error.message}`, "error");
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

    function setScanning(value) {
      state.scanning = value;
      scanButton.disabled = value || !selectedRuntime();
      runtimeSelect.disabled = value;
      scanButton.querySelector("span").textContent = value ? "分析中..." : "开始分析";
    }

    function setBrowsing(value) {
      state.browsing = value;
      browseButton.disabled = value;
      browseButton.querySelector("span").textContent = value ? "选择中..." : "选择仓库";
    }

    function setDetailMode(mode) {
      state.detailMode = mode;
      sourceTab.classList.toggle("active", mode === "source");
      patchTab.classList.toggle("active", mode === "patch");
      renderDetailContent();
    }

    function openFullPatch() {
      fullPatchPre.textContent = state.patch || "本次分析没有生成安全修改。";
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

    function renderEmpty() {
      state.report = null;
      state.patch = "";
      state.selectedIssueId = "";
      state.detailMode = "source";
      document.querySelector("#metrics").innerHTML = summaryMarkup(null);
      document.querySelector("#issues").innerHTML = '<div class="empty">暂无分析结果</div>';
      document.querySelector("#issueDetail").innerHTML = '<div class="muted">选择仓库并开始分析。</div>';
      fullPatchButton.disabled = true;
      setDetailMode("source");
      renderDiagnostics();
    }

    function renderReport(report) {
      state.report = report;
      state.patch = "";
      state.selectedIssueId = (report.issues && report.issues[0] && report.issues[0].id) || "";
      fullPatchButton.disabled = true;
      renderMetrics(report.summary);
      renderIssues(report.issues || []);
      renderIssueDetail();
      setDetailMode("source");
      renderDiagnostics();
    }

    function renderMetrics(summary) {
      document.querySelector("#metrics").innerHTML = summaryMarkup(summary);
    }

    function summaryMarkup(summary) {
      if (!summary) {
        return `
          <div class="score-panel"><span class="metric-label">治理评分</span><div class="score-line"><strong>-</strong><span>未分析</span></div><div class="score-track" style="--score:0"><i></i></div></div>
          ${metricMarkup("-", "扫描文件")}
          ${metricMarkup("-", "日志调用")}
          ${metricMarkup("-", "发现问题")}
          ${metricMarkup("-", "高 / 中 / 低")}
        `;
      }
      const sev = summary.severity_counts || {};
      return `
        <div class="score-panel"><span class="metric-label">治理评分</span><div class="score-line"><strong>${esc(summary.score)}</strong><span>/ 100 · ${esc(scoreLabel(summary.score))}</span></div><div class="score-track" style="--score:${esc(summary.score)}"><i></i></div></div>
        ${metricMarkup(summary.files_scanned, "扫描文件")}
        ${metricMarkup(summary.log_count, "日志调用")}
        ${metricMarkup(summary.issue_count, "发现问题")}
        ${metricMarkup(`${sev.high || 0} / ${sev.medium || 0} / ${sev.low || 0}`, "高 / 中 / 低")}
      `;
    }

    function metricMarkup(value, label) {
      return `<div class="metric"><span class="metric-label">${esc(label)}</span><strong>${esc(value)}</strong></div>`;
    }

    function scoreLabel(score) {
      if (score >= 85) return "健康";
      if (score >= 60) return "需关注";
      return "高风险";
    }

    function renderIssues(issues) {
      const target = document.querySelector("#issues");
      if (!issues.length) { target.innerHTML = '<div class="empty">未发现问题</div>'; return; }
      target.innerHTML = issues.map(issue => `
        <button class="issue-row ${issue.id === state.selectedIssueId ? "active" : ""}" type="button" data-issue-id="${esc(issue.id)}">
          <span class="pill ${esc(issue.severity)}">${esc(severityText(issue.severity))}</span>
          <span><span class="issue-title">${esc(issue.title)}</span><span class="meta">${esc(issue.file_path)}:${esc(issue.line)} · ${esc(ruleText(issue.kind))}</span></span>
        </button>
      `).join("");
      target.querySelectorAll("button[data-issue-id]").forEach(button => {
        button.addEventListener("click", () => {
          state.selectedIssueId = button.dataset.issueId;
          renderIssues(state.report.issues || []);
          renderIssueDetail();
          renderDetailContent();
        });
      });
    }

    function renderIssueDetail() {
      const detail = document.querySelector("#issueDetail");
      const issue = selectedIssue();
      if (!issue) {
        detail.innerHTML = '<div class="muted">暂无问题详情</div>';
        return;
      }
      detail.innerHTML = `
        <div>
          <h3>${esc(issue.title)}</h3>
          <div class="meta">${esc(issue.file_path)}:${esc(issue.line)} · ${esc(sourceText(issue.source))}</div>
        </div>
        <div class="kv"><span>级别</span><strong class="${esc(issue.severity === "high" ? "danger" : issue.severity === "medium" ? "warning" : "success")}">${esc(severityText(issue.severity))}</strong></div>
        <div class="kv"><span>原因</span><div>${esc(issue.reason)}</div></div>
        <div class="kv"><span>建议</span><div>${esc(issue.suggestion)}</div></div>
      `;
    }

    function renderDetailContent() {
      const target = document.querySelector("#detailPre");
      target.textContent = state.detailMode === "patch" ? issuePatchText() : relatedCodeText();
    }

    function selectedIssue() {
      const issues = (state.report && state.report.issues) || [];
      return issues.find(issue => issue.id === state.selectedIssueId) || issues[0] || null;
    }

    function selectedIssueLog() {
      const issue = selectedIssue();
      const logs = (state.report && state.report.logs) || [];
      if (!issue) return null;
      return logs.find(log => log.id === issue.log_call_id) || null;
    }

    function relatedCodeText() {
      const issue = selectedIssue();
      if (!issue) return "选择一个问题查看相关代码。";
      const log = selectedIssueLog();
      const context = issue.context || (log && log.context) || "";
      if (!context) return issue.file_path + ":" + issue.line + "\\n\\n当前报告没有保存源码上下文，请重新运行分析。";
      const marked = context.split("\\n").map(line =>
        line.startsWith(String(issue.line) + ":") ? "> " + line : "  " + line
      ).join("\\n");
      return issue.file_path + ":" + issue.line + "\\n\\n" + marked;
    }

    function issuePatchText() {
      const issue = selectedIssue();
      if (!issue) return "选择一个问题查看修改预览。";
      const log = selectedIssueLog();
      const sourceLine = issue.source_line || (log && log.source_line) || "";
      if (issue.patch_action === "delete" && sourceLine) {
        return "文件  " + issue.file_path + "\\n位置  第 " + issue.line + " 行\\n操作  删除当前日志\\n\\n- " + sourceLine;
      }
      return "当前问题没有可安全自动生成的修改。\\n\\n处理建议\\n" + issue.suggestion;
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
        ai_log_quality: "运行时分析"
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
            <div class="history-score"><strong>${esc(run.score)}</strong><span> / 100</span></div>
            <div class="history-stats">${esc(run.files_scanned)} 文件 · ${esc(run.log_count)} 日志 · ${esc(run.issue_count)} 问题<br>${esc(run.runtime_id || "规则分析")} · 高 ${esc(sev.high || 0)} · 中 ${esc(sev.medium || 0)} · 低 ${esc(sev.low || 0)}</div>
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
