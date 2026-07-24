# LogPilot

LogPilot is a local desktop log-quality scanner for source repositories. It inventories source-language coverage, identifies noisy logs, forbidden log APIs, missing exception logs, and possible sensitive data exposure, then presents reviewable and applicable changes without uploading the repository.

## Quick start

Install and launch the packaged desktop application for the full workflow, including the native repository picker. The Python CLI remains available for automation:

```bash
python -m pip install .
logpilot runtimes
logpilot scan . --runtime codex
logpilot scan . --module src
logpilot report .
logpilot apply . --run latest
logpilot rollback .
```

The workbench detects local Codex and Claude CLI runtimes, shows their health and versions, and sends each log-analysis batch through the selected runtime. Codex runs in read-only ephemeral mode; Claude runs without tools in plan mode. AI analysis first discovers custom logging APIs, then reviews recognized logs and exception paths. Successful runtime responses are cached in the user data directory. Override executable discovery with `LOGPILOT_CODEX_PATH` or `LOGPILOT_CLAUDE_PATH`.

The selected repository is not used for generated artifacts. Reports, history, patch previews, and apply backups are stored in the current user's application data directory:

- Windows: `%LOCALAPPDATA%\LogPilot\repositories\<repository_id>\`
- macOS: `~/Library/Application Support/LogPilot/repositories/<repository_id>/`
- Linux: `$XDG_DATA_HOME/logpilot/repositories/<repository_id>/`

Set `LOGPILOT_DATA_DIR` to override the root directory for tests or isolated environments. A repository may still contain a user-maintained `.logpilot.yaml` scan configuration; LogPilot never creates it.

Large repositories use bounded execution. LogPilot profiles the repository with `git ls-files` when available, identifies project modules, and splits each selected module into hidden chunks of at most 1,000 files or 128 MiB. Repositories with at least 5,000 source files or 512 MiB show a directory selector before analysis. Files larger than 10 MiB are skipped by default and make coverage partial; use `--include-large-files` only when they must be inspected.

New runs store canonical results in `runs/<run_id>/results.sqlite3`. The Web service keeps progress summaries only and queries findings in pages of 100, with a hard API maximum of 200. Parsing, rules, and AI run in a separate `logpilot.scan_runner` process. Completed chunks remain durable, so an interrupted run can continue without repeating them.

The workbench groups findings by file in a single vertical result stream. Search and severity filters narrow the stream, high-risk findings open by default, and each expanded item keeps its reason, source context, and exact diff together. Exact deletions, replacements, and insertions can be selected per item or per file and applied as one checked batch.

LogPilot automatically detects repository languages from source files; users choose the directory scope and log templates rather than maintaining a language allowlist. Python and C/C++ have full parser support; Java, JavaScript, and TypeScript have limited parser support. C/C++ recognition includes Qt logging, glog macros, standard streams, and `printf` calls. Other known or unknown languages are reported as unsupported instead of being silently ignored. AI may inspect small samples to identify likely logging APIs, but these advisory results never count as parser coverage.

Before analysis, choose the AI depth: **Quick** prioritizes high-risk targets, **Standard** covers the main flow with high safety limits, and **Deep** removes AI target-count limits. A report only receives a numeric governance score when coverage and AI analysis are sufficient. Repositories with no log samples, unsupported primary languages, parser failures, or incomplete AI analysis show `N/A` with the reason.

Log templates are resolved in this order: user-fixed template, repository style recommendation, then the built-in safe template. Python exception handlers can currently receive validated automatic log insertions; C/C++ exact deletions are supported, while AI-only missing-log suggestions remain review-only.

Every apply validates the saved source context, stores a backup under `applies/<apply_id>/`, and can roll back the latest unchanged transaction. Repository settings and language profiles are stored beside these artifacts in the user data directory. Text-only AI suggestions remain review-only.

## Development

```bash
python -m pip install -e ".[desktop]"
python -m unittest discover -s tests
python -m logpilot runtimes
python -m logpilot scan . --runtime codex
cd ui
npm install
npm run check
npm test
npm run desktop:dev
```

The production client is a Tauri application under `ui/src-tauri/`. It starts a private Python Engine sidecar on a random loopback port, authenticates every API request with a per-launch token, and stops the Engine when the window exits. The repository chooser is provided by the operating system and opens from the path currently entered in the workbench. A second launch focuses the existing window.

Use `python -m logpilot ui` only for browser-based UI debugging. It remembers the most recently selected repository, but the browser mode intentionally has no native folder picker; enter a path directly or pass `--path <repository>`.

Build a Windows installer with:

```bash
python scripts/build_desktop_engine.py
cd ui
npm run build
npx tauri build --bundles nsis
```

The React/TypeScript client owns all presentation and desktop integration. `src/logpilot/web.py` is an API and static-asset server only; new UI markup or styling must not be embedded in Python. The extracted compatibility controller preserves the current workbench behavior while screens are migrated incrementally to React components.

C and C++ parsing runs in a reusable isolated worker process, so a native parser crash is recorded against the current file without terminating the Engine. The Tree-sitter versions in `requirements.lock` are a tested compatibility set and must be upgraded together only after the real C/C++ regression scan passes.
