from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .app_logging import configure_logging, shutdown_logging
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
    configure_logging("scan-runner")
    logger = logging.getLogger("logpilot.scan_runner")
    cancel_file = Path(args.cancel_file)
    logger.info(
        "scan_process_started run_id=%s repository=%s runtime=%s modules=%s retry_module=%s resume=%s",
        args.run,
        Path(args.repository).resolve(),
        args.runtime,
        len(args.module),
        args.retry_module or "none",
        args.resume,
    )

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
        logger.info("scan_process_cancelled run_id=%s", args.run)
        print(json.dumps({"type": "cancelled", "run_id": args.run}, ensure_ascii=False), flush=True)
        shutdown_logging()
        return 2
    except Exception as exc:
        logger.exception("scan_process_failed run_id=%s", args.run)
        print(
            json.dumps(
                {"type": "failed", "run_id": args.run, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            flush=True,
        )
        shutdown_logging()
        return 1
    logger.info("scan_process_completed run_id=%s", args.run)
    print(json.dumps({"type": "completed", "run_id": args.run}, ensure_ascii=False), flush=True)
    shutdown_logging()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
