# LogPilot

LogPilot is a local log-quality scanner for source repositories. It identifies noisy logs, forbidden log APIs, missing exception logs, and possible sensitive data exposure, then writes review artifacts for humans to inspect.

## Quick start

```bash
python -m pip install -e .
logpilot ui
```

Open the printed local URL, use the repository picker or enter a path such as `D:\GitHub\log-pilot`, and click the analyze button. You can still run the CLI directly:

```bash
logpilot scan .
logpilot report
```

Both the UI and scan command write artifacts to the selected repository's `.logpilot/` directory:

- `report.json` for structured tooling.
- `report.md` for human review.
- `changes.diff` for safe, reviewable patch suggestions.
- `runs/<run_id>/` for historical reports and patch snapshots.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests
python -m logpilot scan .
python -m logpilot ui --path .
```

The local Web UI exposes `POST /api/browse` for a native folder picker, `POST /api/scan` for path-based analysis, and history endpoints for loading earlier runs. The MVP intentionally uses Python standard library modules only so it can run on the current local Python 3.14 environment. Future integrations can add Typer, FastAPI, Pydantic, and Tree-sitter behind the existing CLI, Web, model, and parser boundaries.
