from __future__ import annotations

import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .history import list_history_runs, load_history_run
from .pipeline import run_scan


def build_server(repo_root: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    initial_root = repo_root.resolve()
    state = {"repo_root": initial_root, "artifacts": initial_root / ".logpilot"}

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
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/scan":
                self._handle_scan()
            elif parsed.path == "/api/browse":
                self._handle_browse()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args) -> None:
            return

        def _handle_scan(self) -> None:
            try:
                payload = self._read_json()
                target = _resolve_repo_path(str(payload.get("path", "")))
                if not target.exists() or not target.is_dir():
                    self._send_json({"error": f"Repository path does not exist: {target}"}, HTTPStatus.BAD_REQUEST)
                    return

                report = run_scan(target)
                state["repo_root"] = target.resolve()
                state["artifacts"] = target.resolve() / ".logpilot"
                history = list_history_runs(state["artifacts"])
                self._send_json(
                    {
                        "repository": str(target.resolve()),
                        "report": report.to_dict(),
                        "history": history,
                        "run": history[0] if history else None,
                    }
                )
            except Exception as exc:  # Keep the local UI useful during early scanner work.
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_browse(self) -> None:
            try:
                selected = choose_directory(state["repo_root"])
                if not selected:
                    self._send_json({"cancelled": True, "path": ""})
                    return
                self._send_json({"cancelled": False, "path": str(selected)})
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _send_history_run(self, query: str) -> None:
            run_ids = parse_qs(query).get("run_id", [])
            if not run_ids:
                self._send_json({"error": "run_id is required."}, HTTPStatus.BAD_REQUEST)
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
                raise ValueError("JSON body must be an object.")
            return loaded

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.exists():
                self._send_json(
                    {"error": f"Artifact not found: {path.name}", "repository": str(state["repo_root"])},
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
    if os.name == "nt":
        try:
            return _choose_directory_windows(initial_dir)
        except RuntimeError:
            pass
    return _choose_directory_tk(initial_dir)


def _choose_directory_windows(initial_dir: Path) -> Path | None:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "选择 LogPilot 要分析的仓库"
$dialog.SelectedPath = $env:LOGPILOT_INITIAL_DIR
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
  Write-Output $dialog.SelectedPath
}
"""
    env = os.environ.copy()
    env["LOGPILOT_INITIAL_DIR"] = str(initial_dir)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Windows folder picker failed.")
    selected = result.stdout.strip()
    return Path(selected).resolve() if selected else None


def _choose_directory_tk(initial_dir: Path) -> Path | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=str(initial_dir),
            title="Select repository for LogPilot analysis",
        )
    finally:
        root.destroy()
    return Path(selected).resolve() if selected else None


def _resolve_repo_path(raw_path: str) -> Path:
    if not raw_path.strip():
        raise ValueError("Repository path is required.")
    return Path(raw_path).expanduser().resolve()


def _state_payload(state: dict[str, Path]) -> dict[str, object]:
    artifacts = state["artifacts"]
    return {
        "repository": str(state["repo_root"]),
        "has_report": (artifacts / "report.json").exists(),
        "history": list_history_runs(artifacts),
    }


def _html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LogPilot Analysis Workbench</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #687386;
      --line: #d7dee8;
      --blue: #1f66d1;
      --blue-dark: #174a9b;
      --green: #19805a;
      --amber: #a86608;
      --red: #c5342e;
      --code: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      padding: 22px 32px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }
    p { margin: 0; color: var(--muted); }
    main { padding: 22px 32px 28px; display: grid; gap: 18px; }
    .workspace {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 12px;
      align-items: end;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
    input {
      width: 100%;
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }
    button {
      height: 42px;
      border: 0;
      border-radius: 6px;
      padding: 0 18px;
      background: var(--blue);
      color: #fff;
      font-weight: 650;
      cursor: pointer;
      white-space: nowrap;
    }
    button:hover { background: var(--blue-dark); }
    button:disabled { opacity: .62; cursor: wait; }
    .secondary {
      background: #e8edf5;
      color: var(--ink);
      border: 1px solid var(--line);
    }
    .secondary:hover { background: #dde5f1; }
    .status {
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--line);
    }
    .tab {
      height: 36px;
      background: transparent;
      color: var(--muted);
      border: 0;
      border-radius: 6px 6px 0 0;
      padding: 0 14px;
    }
    .tab.active {
      background: var(--panel);
      color: var(--ink);
      border: 1px solid var(--line);
      border-bottom-color: var(--panel);
      margin-bottom: -1px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 12px;
    }
    .metric, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .metric { padding: 16px; }
    .metric strong { display: block; font-size: 28px; line-height: 1.1; }
    .metric span { display: block; margin-top: 6px; color: var(--muted); font-size: 13px; }
    .grid { display: grid; grid-template-columns: 1.15fr .85fr; gap: 18px; align-items: start; }
    section { overflow: hidden; }
    section h2 {
      margin: 0;
      padding: 14px 16px;
      font-size: 16px;
      border-bottom: 1px solid var(--line);
    }
    .list { max-height: 520px; overflow: auto; }
    .history-list { max-height: 680px; overflow: auto; }
    .item { padding: 14px 16px; border-bottom: 1px solid var(--line); }
    .item:last-child { border-bottom: 0; }
    .item h3 { margin: 0 0 6px; font-size: 15px; }
    .item button { margin-top: 10px; height: 34px; padding: 0 12px; }
    .history-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }
    .meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .pill {
      display: inline-block;
      min-width: 52px;
      padding: 2px 8px;
      border-radius: 999px;
      color: #fff;
      text-align: center;
      font-size: 12px;
      margin-right: 8px;
    }
    .high { background: var(--red); }
    .medium { background: var(--amber); }
    .low { background: var(--green); }
    pre {
      margin: 0;
      padding: 16px;
      overflow: auto;
      max-height: 520px;
      background: var(--code);
      color: #e8edf7;
      font-size: 12px;
      line-height: 1.5;
    }
    .empty { padding: 16px; color: var(--muted); }
    .hidden { display: none; }
    @media (max-width: 900px) {
      header, main { padding-left: 18px; padding-right: 18px; }
      .workspace, .metrics, .grid, .history-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>LogPilot 本地分析工作台</h1>
    <p>选择或输入本地仓库路径，点击开始分析，查看日志质量问题、建议、历史记录和 Patch 预览。</p>
  </header>
  <main>
    <section class="workspace">
      <div>
        <label for="repoPath">仓库路径</label>
        <input id="repoPath" type="text" spellcheck="false" placeholder="例如 D:\\GitHub\\log-pilot">
        <div class="status" id="status">正在读取默认仓库...</div>
      </div>
      <button class="secondary" id="browseButton" type="button">选择仓库</button>
      <button id="scanButton" type="button">开始分析</button>
    </section>
    <nav class="tabs" aria-label="LogPilot views">
      <button class="tab active" id="currentTab" type="button">当前分析</button>
      <button class="tab" id="historyTab" type="button">历史记录</button>
    </nav>
    <div id="currentPanel">
      <div class="metrics" id="metrics"></div>
      <div class="grid">
        <section>
          <h2>问题列表</h2>
          <div class="list" id="issues"></div>
        </section>
        <section>
          <h2>Patch 预览</h2>
          <pre id="patch">等待分析结果...</pre>
        </section>
      </div>
      <div class="grid">
        <section>
          <h2>日志调用</h2>
          <div class="list" id="logs"></div>
        </section>
        <section>
          <h2>AI 调试信息</h2>
          <div class="list" id="ai"></div>
        </section>
      </div>
    </div>
    <section id="historyPanel" class="hidden">
      <h2>历史分析结果</h2>
      <div class="history-list" id="historyList"></div>
    </section>
  </main>
  <script>
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[char]));
    const state = {
      path: "",
      scanning: false,
      browsing: false,
      history: []
    };
    const repoPath = document.querySelector("#repoPath");
    const browseButton = document.querySelector("#browseButton");
    const scanButton = document.querySelector("#scanButton");
    const statusLine = document.querySelector("#status");
    const currentTab = document.querySelector("#currentTab");
    const historyTab = document.querySelector("#historyTab");
    const currentPanel = document.querySelector("#currentPanel");
    const historyPanel = document.querySelector("#historyPanel");

    scanButton.addEventListener("click", () => startScan(repoPath.value));
    browseButton.addEventListener("click", browseRepository);
    repoPath.addEventListener("keydown", event => {
      if (event.key === "Enter") startScan(repoPath.value);
    });
    currentTab.addEventListener("click", () => showTab("current"));
    historyTab.addEventListener("click", () => showTab("history"));

    async function init() {
      try {
        const response = await fetch("/api/state");
        const payload = await response.json();
        state.path = payload.repository || "";
        state.history = payload.history || [];
        repoPath.value = state.path;
        renderHistory(state.history);
        statusLine.textContent = payload.has_report
          ? "已读取最新分析结果，可重新点击开始分析。"
          : "请选择或输入仓库路径后点击开始分析。";
        if (payload.has_report) await loadReport();
        else renderEmpty();
      } catch (error) {
        statusLine.textContent = `初始化失败：${error.message}`;
        renderEmpty();
      }
    }

    async function browseRepository() {
      if (state.browsing) return;
      setBrowsing(true);
      statusLine.textContent = "正在打开文件夹选择窗口...";
      try {
        const response = await fetch("/api/browse", { method: "POST" });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "选择仓库失败");
        if (payload.cancelled) {
          statusLine.textContent = "已取消选择。";
          return;
        }
        repoPath.value = payload.path;
        statusLine.textContent = `已选择仓库：${payload.path}`;
      } catch (error) {
        statusLine.textContent = `选择失败：${error.message}`;
      } finally {
        setBrowsing(false);
      }
    }

    async function startScan(path) {
      if (state.scanning) return;
      const target = path.trim();
      if (!target) {
        statusLine.textContent = "请先输入或选择本地仓库路径。";
        return;
      }
      setScanning(true);
      statusLine.textContent = "正在分析仓库，较大的项目可能需要一些时间...";
      try {
        const response = await fetch("/api/scan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "扫描失败");
        state.path = payload.repository;
        state.history = payload.history || [];
        repoPath.value = state.path;
        renderReport(payload.report);
        renderHistory(state.history);
        await loadPatch();
        showTab("current");
        const runText = payload.run ? `，已保存历史记录 ${payload.run.run_id}` : "";
        statusLine.textContent = `分析完成：${state.path}${runText}`;
      } catch (error) {
        statusLine.textContent = `分析失败：${error.message}`;
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
      document.querySelector("#patch").textContent = response.ok ? text : "暂无 Patch 产物。";
    }

    async function loadHistoryRun(runId) {
      statusLine.textContent = `正在读取历史记录：${runId}`;
      try {
        const response = await fetch(`/api/history/run?run_id=${encodeURIComponent(runId)}`);
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "历史记录读取失败");
        renderReport(payload.report);
        document.querySelector("#patch").textContent = payload.patch || "暂无 Patch 产物。";
        showTab("current");
        statusLine.textContent = `已加载历史记录：${payload.metadata.created_at}`;
      } catch (error) {
        statusLine.textContent = `历史记录读取失败：${error.message}`;
      }
    }

    function setScanning(value) {
      state.scanning = value;
      scanButton.disabled = value;
      scanButton.textContent = value ? "分析中..." : "开始分析";
    }

    function setBrowsing(value) {
      state.browsing = value;
      browseButton.disabled = value;
      browseButton.textContent = value ? "选择中..." : "选择仓库";
    }

    function showTab(name) {
      const showHistory = name === "history";
      currentPanel.classList.toggle("hidden", showHistory);
      historyPanel.classList.toggle("hidden", !showHistory);
      currentTab.classList.toggle("active", !showHistory);
      historyTab.classList.toggle("active", showHistory);
    }

    function renderEmpty() {
      document.querySelector("#metrics").innerHTML = [
        ["Score", "-"], ["Files", "-"], ["Logs", "-"], ["Issues", "-"], ["High / Medium / Low", "-"]
      ].map(([label, value]) => `<div class="metric"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join("");
      document.querySelector("#issues").innerHTML = '<div class="empty">还没有分析结果。</div>';
      document.querySelector("#logs").innerHTML = '<div class="empty">点击开始分析后会展示日志调用。</div>';
      document.querySelector("#ai").innerHTML = '<div class="empty">AI 默认关闭，当前仅展示规则分析结果。</div>';
      document.querySelector("#patch").textContent = "等待分析结果...";
    }

    function renderReport(report) {
      renderMetrics(report.summary);
      renderIssues(report.issues || []);
      renderLogs(report.logs || []);
      renderAi(report.ai_traces || []);
    }

    function renderMetrics(summary) {
      const sev = summary.severity_counts || {};
      document.querySelector("#metrics").innerHTML = [
        ["Score", `${summary.score}/100`],
        ["Files", summary.files_scanned],
        ["Logs", summary.log_count],
        ["Issues", summary.issue_count],
        ["High / Medium / Low", `${sev.high || 0} / ${sev.medium || 0} / ${sev.low || 0}`]
      ].map(([label, value]) => `<div class="metric"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join("");
    }

    function renderIssues(issues) {
      const target = document.querySelector("#issues");
      if (!issues.length) { target.innerHTML = '<div class="empty">没有发现问题。</div>'; return; }
      target.innerHTML = issues.map(issue => `
        <div class="item">
          <h3><span class="pill ${esc(issue.severity)}">${esc(issue.severity)}</span>${esc(issue.title)}</h3>
          <div class="meta">${esc(issue.file_path)}:${esc(issue.line)} · ${esc(issue.kind)} · ${esc(issue.source)}</div>
          <p>${esc(issue.reason)}</p>
          <p>${esc(issue.suggestion)}</p>
        </div>
      `).join("");
    }

    function renderLogs(logs) {
      const target = document.querySelector("#logs");
      if (!logs.length) { target.innerHTML = '<div class="empty">没有识别到日志调用。</div>'; return; }
      target.innerHTML = logs.map(log => `
        <div class="item">
          <h3>${esc(log.callee)} <span class="meta">${esc(log.level)}</span></h3>
          <div class="meta">${esc(log.file_path)}:${esc(log.line)}</div>
          <p>${esc(log.message || "<empty>")}</p>
        </div>
      `).join("");
    }

    function renderAi(traces) {
      const target = document.querySelector("#ai");
      if (!traces.length) { target.innerHTML = '<div class="empty">AI 未开启，当前仅展示规则分析结果。</div>'; return; }
      target.innerHTML = traces.map(trace => `
        <div class="item">
          <h3>${esc(trace.status)} <span class="meta">${esc(trace.log_call_id)}</span></h3>
          <p>${esc(trace.error || trace.raw_response || "No response")}</p>
        </div>
      `).join("");
    }

    function renderHistory(runs) {
      const target = document.querySelector("#historyList");
      if (!runs.length) {
        target.innerHTML = '<div class="empty">暂无历史分析结果。完成一次分析后会自动记录。</div>';
        return;
      }
      target.innerHTML = runs.map(run => {
        const sev = run.severity_counts || {};
        return `
          <div class="item history-row">
            <div>
              <h3>${esc(run.created_at)} <span class="meta">score ${esc(run.score)}/100</span></h3>
              <div class="meta">${esc(run.repository)}</div>
              <p>${esc(run.files_scanned)} files · ${esc(run.log_count)} logs · ${esc(run.issue_count)} issues · high/medium/low ${esc(sev.high || 0)}/${esc(sev.medium || 0)}/${esc(sev.low || 0)}</p>
            </div>
            <button type="button" data-run-id="${esc(run.run_id)}">查看结果</button>
          </div>
        `;
      }).join("");
      target.querySelectorAll("button[data-run-id]").forEach(button => {
        button.addEventListener("click", () => loadHistoryRun(button.dataset.runId));
      });
    }

    init();
  </script>
</body>
</html>"""
