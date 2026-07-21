from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import retry_module, run_scan


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m logpilot.scan_runner")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--runtime", default="auto")
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--retry-module", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--cancel-file", required=True)
    args = parser.parse_args(argv)
    cancel_file = Path(args.cancel_file)

    def emit(event: dict[str, object]) -> None:
        print(json.dumps({"type": "progress", "event": event}, ensure_ascii=False), flush=True)

    def cancelled() -> bool:
        return cancel_file.exists()

    try:
        if args.retry_module:
            retry_module(
                Path(args.repository),
                args.run,
                args.retry_module,
                runtime_id=args.runtime,
                progress=emit,
                should_cancel=cancelled,
            )
        else:
            run_scan(
                Path(args.repository),
                runtime_id=args.runtime,
                progress=emit,
                should_cancel=cancelled,
                plan_id=args.plan,
                module_ids=list(args.module),
                run_id=args.run,
                resume=args.resume,
                return_report=False,
            )
    except InterruptedError:
        print(json.dumps({"type": "cancelled", "run_id": args.run}, ensure_ascii=False), flush=True)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"type": "failed", "run_id": args.run, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1
    print(json.dumps({"type": "completed", "run_id": args.run}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
