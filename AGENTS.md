# Repository Guidelines

## Project Structure & Module Organization

This repository implements the LogPilot Python CLI and local Web analysis workbench. Source code lives under `src/logpilot/`, tests live under `tests/`, product and architecture docs live under `docs/`, and examples live under `examples/`. Generated scan artifacts are written to `.logpilot/` and must stay ignored.

## Build, Test, and Development Commands

Use the local Python runtime to install and validate the project:

- `python -m pip install -e .` installs the editable CLI package.
- `python -m unittest discover -s tests` runs the test suite.
- `python -m logpilot runtimes` checks local Codex and Claude runtimes.
- `python -m logpilot scan .` scans the current repository.
- `python -m logpilot ui --path .` starts the local debug console.

Run `git diff --check` before submitting changes. Runtime dependencies are tracked in `requirements.lock`; keep it updated when dependencies are added.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and keep modules focused around one responsibility: scanning, parsing, rules, runtime execution, AI, reporting, patching, CLI, or Web UI. Prefer dataclasses and explicit type hints for shared data structures. Keep Markdown headings sentence-case and use fenced code blocks with language labels.

## Testing Guidelines

Tests use Python `unittest` and should be added under `tests/` with names such as `test_pipeline.py` or `test_config.py`. Cover parser behavior, rule findings, report generation, patch output, and Web artifact rendering. Use temporary directories for generated scan output.

## Commit & Pull Request Guidelines

The current history contains only `Initial commit`; continue with short, imperative commit subjects such as `Add scan pipeline` or `Improve report output`. Pull requests should include a concise summary, the commands or manual checks performed, and screenshots or diagrams when UI or visual documentation changes. Keep diagrams simple, proportional, and accompanied by a brief explanation.
