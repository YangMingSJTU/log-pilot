# Repository Guidelines

## Project Structure & Module Organization

This repository implements the LogPilot Python CLI and local Web analysis workbench. Source code lives under `src/logpilot/`, tests under `tests/`, product and architecture docs under `docs/`, and examples under `examples/`. Generated reports, history, patches, and apply backups belong in the user application data directory resolved by `src/logpilot/storage.py`; never write them into a scanned repository. A repository-local `.logpilot.yaml` is user configuration, not generated output.

## Build, Test, and Development Commands

Use the local Python runtime to install and validate the project:

- `python -m pip install -e .` installs the editable CLI package.
- `python -m unittest discover -s tests` runs the test suite.
- `python -m logpilot runtimes` checks local Codex and Claude runtimes.
- `python -m logpilot scan .` scans the current repository.
- `python -m logpilot apply . --run latest` selects exact changes to apply.
- `python -m logpilot rollback .` restores the latest unchanged apply transaction.
- `python -m logpilot ui --path .` starts the local debug console.

Run `git diff --check` before submitting changes. Runtime dependencies are tracked in `requirements.lock`; keep it updated when dependencies are added.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and keep modules focused around one responsibility: language registration, scanning, parsing, rules, runtime execution, AI, reporting, patching, CLI, or Web UI. Prefer dataclasses and explicit type hints for shared data structures. Register extensions and support levels in `src/logpilot/languages.py`; do not duplicate suffix maps. Keep Markdown headings sentence-case and use fenced code blocks with language labels.

## Testing Guidelines

Tests use Python `unittest` and should be added under `tests/` with names such as `test_pipeline.py`, `test_language_coverage.py`, or `test_remediation.py`. Cover parser behavior, unsupported-language coverage, AI degradation, user-data storage, report generation, exact apply validation, atomic rollback, and Web rendering. Set `LOGPILOT_DATA_DIR` to a temporary directory so tests never pollute the real profile.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects such as `Add remediation workflow` or `Improve report output`. Pull requests should include a concise summary, the commands or manual checks performed, and screenshots or diagrams when UI or visual documentation changes. Keep diagrams simple, proportional, and accompanied by a brief explanation.
