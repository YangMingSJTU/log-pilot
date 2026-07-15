# LogPilot

LogPilot is a local log-quality scanner for source repositories. It identifies noisy logs, forbidden log APIs, missing exception logs, and possible sensitive data exposure, then writes review artifacts for humans to inspect.

## Quick start

```bash
python -m pip install -e .
logpilot ui
```

Open the printed local URL, use the repository picker or enter a path such as `D:\GitHub\log-pilot`, and click the analyze button. You can still run the CLI directly:

```bash
logpilot runtimes
logpilot scan . --runtime codex
logpilot report .
logpilot apply . --run latest
logpilot rollback .
```

The workbench detects local Codex and Claude CLI runtimes, shows their health and versions, and sends each log-analysis batch through the selected runtime. Codex runs in read-only ephemeral mode; Claude runs without tools in plan mode. Override executable discovery with `LOGPILOT_CODEX_PATH` or `LOGPILOT_CLAUDE_PATH`.

The selected repository is not used for generated artifacts. Reports, history, patch previews, and apply backups are stored in the current user's application data directory:

- Windows: `%LOCALAPPDATA%\LogPilot\repositories\<repository_id>\`
- macOS: `~/Library/Application Support/LogPilot/repositories/<repository_id>/`
- Linux: `$XDG_DATA_HOME/logpilot/repositories/<repository_id>/`

Set `LOGPILOT_DATA_DIR` to override the root directory for tests or isolated environments. A repository may still contain a user-maintained `.logpilot.yaml` scan configuration; LogPilot never creates it.

The workbench can apply exact deletion patches individually or in a checked batch. Every apply validates the saved source context, stores a backup under `applies/<apply_id>/`, and can roll back the latest unchanged transaction. Text-only AI suggestions remain review-only.
## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests
python -m logpilot runtimes
python -m logpilot scan . --runtime codex
python -m logpilot ui --path .
```

The local Web UI exposes repository selection, runtime-backed scanning, history, exact patch apply, and rollback endpoints. The MVP intentionally uses Python standard library modules only so it can run on the current local Python 3.14 environment. Future integrations can add Typer, FastAPI, Pydantic, and Tree-sitter behind the existing CLI, Web, model, and parser boundaries.
