from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .history import list_history_runs, load_history_run
from .pipeline import run_scan
from .remediation import (
    ApplyConflictError,
    ApplyNotFoundError,
    RemediationError,
    applicable_issue_groups,
    apply_suggestions,
    rollback_apply,
)
from .runtime import RuntimeRegistry
from .storage import repository_data_dir
from .web import serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="logpilot", description="Scan repositories for log quality issues.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan a repository and write report artifacts.")
    scan_parser.add_argument("path", nargs="?", default=".", help="Repository path to scan.")
    scan_parser.add_argument("--config", default=None, help="Optional .logpilot.yaml path.")
    scan_parser.add_argument("--runtime", default=None, help="AI runtime: auto, codex, or claude.")
    scan_parser.set_defaults(func=_scan)

    report_parser = subparsers.add_parser("report", help="Print a compact summary from report.json.")
    report_parser.add_argument("path", nargs="?", default=".", help="Repository path whose report should be read.")
    report_parser.set_defaults(func=_report)

    ui_parser = subparsers.add_parser("ui", help="Start the local Web debug console.")
    ui_parser.add_argument("--path", default=".", help="Default repository path.")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    ui_parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    ui_parser.set_defaults(func=_ui)

    runtimes_parser = subparsers.add_parser("runtimes", help="List detected local AI runtimes.")
    runtimes_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    runtimes_parser.set_defaults(func=_runtimes)

    apply_parser = subparsers.add_parser("apply", help="Apply exact suggestions from an analysis run.")
    apply_parser.add_argument("path", nargs="?", default=".", help="Repository path to modify.")
    apply_parser.add_argument("--run", default="latest", help="History run ID, or latest.")
    apply_parser.add_argument("--issue", action="append", default=[], help="Issue ID to apply. Repeat as needed.")
    apply_parser.add_argument("--yes", action="store_true", help="Skip the final confirmation.")
    apply_parser.set_defaults(func=_apply)

    rollback_parser = subparsers.add_parser("rollback", help="Rollback the latest applied transaction.")
    rollback_parser.add_argument("path", nargs="?", default=".", help="Repository path to restore.")
    rollback_parser.add_argument("--apply-id", default=None, help="Expected latest apply transaction ID.")
    rollback_parser.set_defaults(func=_rollback)

    args = parser.parse_args(argv)
    args.func(args)


def _scan(args) -> None:
    repo_root = Path(args.path).expanduser().resolve()
    config_path = Path(args.config) if args.config else None
    report = run_scan(repo_root, config_path=config_path, runtime_id=args.runtime)
    print(f"Score: {report.summary.score}/100")
    print(f"Logs: {report.summary.log_count}")
    print(f"Issues: {report.summary.issue_count}")
    print(f"Artifacts: {repository_data_dir(repo_root)}")


def _report(args) -> None:
    path = repository_data_dir(Path(args.path)) / "report.json"
    if not path.exists():
        raise SystemExit(f"Report not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data["summary"]
    print(f"Repository: {summary['repository']}")
    print(f"Score: {summary['score']}/100")
    print(f"Logs: {summary['log_count']}")
    print(f"Issues: {summary['issue_count']}")


def _ui(args) -> None:
    serve(Path(args.path), host=args.host, port=args.port)


def _runtimes(args) -> None:
    runtimes = RuntimeRegistry().refresh()
    if args.json:
        print(json.dumps({"runtimes": [runtime.to_dict() for runtime in runtimes]}, ensure_ascii=False, indent=2))
        return
    for runtime in runtimes:
        detail = runtime.version if runtime.status == "online" else runtime.error
        print(f"{runtime.name:<8} {runtime.status:<7} {detail}")


def _apply(args) -> None:
    repo_root = Path(args.path).expanduser().resolve()
    data_dir = repository_data_dir(repo_root)
    run_id = _cli_run_id(data_dir, args.run)
    run = load_history_run(data_dir, run_id)
    groups = applicable_issue_groups(run["report"])
    if not groups:
        raise SystemExit("No exact suggestions are available for this run.")

    issue_ids = list(dict.fromkeys(args.issue))
    if not issue_ids:
        if not sys.stdin.isatty():
            raise SystemExit("Use --issue when standard input is not interactive.")
        print("Available exact changes:")
        for index, group in enumerate(groups, start=1):
            titles = " / ".join(dict.fromkeys(group["titles"]))
            print(f"  {index}. {group['file_path']}:{group['line']} - {titles}")
        selected = input("Select numbers separated by commas, or 'all': ").strip().lower()
        if selected == "all":
            chosen = groups
        else:
            try:
                indexes = {int(value.strip()) for value in selected.split(",") if value.strip()}
            except ValueError as exc:
                raise SystemExit("Invalid selection.") from exc
            if not indexes or min(indexes) < 1 or max(indexes) > len(groups):
                raise SystemExit("Invalid selection.")
            chosen = [group for index, group in enumerate(groups, start=1) if index in indexes]
        issue_ids = [issue_id for group in chosen for issue_id in group["issue_ids"]]

    selected_groups = [group for group in groups if set(group["issue_ids"]).intersection(issue_ids)]
    if not selected_groups:
        raise SystemExit("The selected issues have no exact changes.")
    files = {group["file_path"] for group in selected_groups}
    print(f"Ready to apply {len(selected_groups)} change(s) across {len(files)} file(s).")
    if not args.yes and input("Apply these changes? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Cancelled.")
        return
    try:
        record = apply_suggestions(repo_root, run_id, issue_ids)
    except (RemediationError, ApplyConflictError, ApplyNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Applied: {record['apply_id']}")
    print(f"Backup: {data_dir / 'applies' / record['apply_id']}")


def _rollback(args) -> None:
    repo_root = Path(args.path).expanduser().resolve()
    try:
        record = rollback_apply(repo_root, args.apply_id)
    except (RemediationError, ApplyConflictError, ApplyNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Rolled back: {record['apply_id']}")


def _cli_run_id(data_dir: Path, requested: str) -> str:
    if requested != "latest":
        return requested
    runs = list_history_runs(data_dir)
    if not runs:
        raise SystemExit("No analysis history found. Run logpilot scan first.")
    return str(runs[0]["run_id"])
