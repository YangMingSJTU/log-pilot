from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run_scan
from .runtime import RuntimeRegistry
from .web import serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="logpilot", description="Scan repositories for log quality issues.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan a repository and write report artifacts.")
    scan_parser.add_argument("path", nargs="?", default=".", help="Repository path to scan.")
    scan_parser.add_argument("--output", default=None, help="Output directory. Defaults to <repo>/.logpilot.")
    scan_parser.add_argument("--config", default=None, help="Optional .logpilot.yaml path.")
    scan_parser.add_argument("--runtime", default=None, help="AI runtime: auto, codex, or claude.")
    scan_parser.set_defaults(func=_scan)

    report_parser = subparsers.add_parser("report", help="Print a compact summary from report.json.")
    report_parser.add_argument("--output", default=".logpilot", help="Directory containing report.json.")
    report_parser.set_defaults(func=_report)

    ui_parser = subparsers.add_parser("ui", help="Start the local Web debug console.")
    ui_parser.add_argument("--path", default=".", help="Repository path containing .logpilot artifacts.")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    ui_parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    ui_parser.set_defaults(func=_ui)

    runtimes_parser = subparsers.add_parser("runtimes", help="List detected local AI runtimes.")
    runtimes_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    runtimes_parser.set_defaults(func=_runtimes)

    args = parser.parse_args(argv)
    args.func(args)


def _scan(args) -> None:
    repo_root = Path(args.path)
    output_dir = Path(args.output) if args.output else None
    config_path = Path(args.config) if args.config else None
    report = run_scan(repo_root, output_dir=output_dir, config_path=config_path, runtime_id=args.runtime)
    print(f"Score: {report.summary.score}/100")
    print(f"Logs: {report.summary.log_count}")
    print(f"Issues: {report.summary.issue_count}")
    print(f"Artifacts: {(output_dir or repo_root / '.logpilot').resolve()}")


def _report(args) -> None:
    path = Path(args.output) / "report.json"
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
