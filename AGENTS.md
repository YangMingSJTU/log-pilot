# Repository Guidelines

## Project Structure & Module Organization

This repository implements the LogPilot Python Engine/CLI and Tauri desktop client. Python source lives under `src/logpilot/`, the Vite/React/TypeScript client under `ui/src/`, the Rust shell under `ui/src-tauri/`, tests under `tests/`, and architecture docs under `docs/`. Compiled Web assets belong in `src/logpilot/web_assets/`. Generated reports, history, patches, and backups belong in the user application data directory; never write them into a scanned repository.

## Build, Test, and Development Commands

Use the local Python runtime to install and validate the project:

- `python -m pip install -e .` installs the editable CLI package.
- `python -m unittest discover -s tests` runs the test suite.
- `cd ui && npm run check && npm test && npm run build` validates and compiles the client.
- `cd ui && npm run desktop:dev` launches the Tauri client with the source Python Engine.
- `python scripts/build_desktop_engine.py` builds the packaged Engine sidecar.
- `cd ui && npx tauri build --bundles nsis` creates the Windows installer.
- `python -m logpilot runtimes` checks local Codex and Claude runtimes.
- `python -m logpilot scan .` scans the current repository.
- `python -m logpilot scan . --module src` limits a run to a planned directory module.
- `python -m logpilot apply . --run latest` selects exact changes to apply.
- `python -m logpilot rollback .` restores the latest unchanged apply transaction.
- `python -m logpilot ui` starts the browser-only debug console; enter paths manually because native folder selection belongs to Tauri.

Run `git diff --check` before submitting. Update `requirements.lock` when dependencies change. Treat the Tree-sitter pins as one compatibility set and run the real C/C++ regression scan before upgrading them.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and 2 spaces for TypeScript/React. Keep modules focused around one responsibility. Prefer dataclasses and explicit type hints for Python contracts and typed interfaces for frontend API contracts. Register language support in `src/logpilot/languages.py`; do not duplicate suffix maps. Keep `src/logpilot/web.py` limited to API and asset serving: new UI belongs in `ui/`, and native capabilities belong in Tauri commands or plugins.

## Testing Guidelines

Python tests use `unittest`; frontend tests use Vitest. Cover module planning, chunk transactions, pagination, parser behavior, AI degradation, apply validation, Engine authentication and shutdown, and desktop API routing. Set `LOGPILOT_DATA_DIR` to a temporary directory so tests never pollute the real profile. For desktop changes, also open the packaged window, exercise the native picker, and verify no Engine process remains after exit.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects such as `Add remediation workflow` or `Improve report output`. Pull requests should include a concise summary, the commands or manual checks performed, and screenshots or diagrams when UI or visual documentation changes. Keep diagrams simple, proportional, and accompanied by a brief explanation.
