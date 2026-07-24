from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .storage import app_data_root
from .web import API_VERSION, build_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="logpilot-engine", description="Run the LogPilot desktop API engine.")
    parser.add_argument("--path", default=None, help="Initial repository path.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback address to bind.")
    parser.add_argument("--port", type=int, default=0, help="Port to bind; zero selects an available port.")
    parser.add_argument("--token", default=os.environ.get("LOGPILOT_ENGINE_TOKEN", ""), help=argparse.SUPPRESS)
    parser.add_argument("--ready-file", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The desktop engine only accepts loopback addresses.")
    if not args.token:
        raise SystemExit("The desktop engine requires a connection token.")

    initial = Path(args.path).expanduser() if args.path else None
    server = build_server(
        initial,
        args.host,
        args.port,
        auth_token=args.token,
        allow_shutdown=True,
    )
    connection = {
        "baseUrl": f"http://127.0.0.1:{int(server.server_address[1])}",
        "apiVersion": API_VERSION,
        "pid": os.getpid(),
    }
    if args.ready_file:
        ready_file = Path(args.ready_file).expanduser().resolve()
        data_root = app_data_root().resolve()
        if not ready_file.is_relative_to(data_root):
            raise SystemExit("The ready file must be located inside the LogPilot data directory.")
        ready_file.parent.mkdir(parents=True, exist_ok=True)
        ready_file.write_text(json.dumps(connection), encoding="utf-8")
    print(json.dumps(connection), flush=True)

    try:
        server.serve_forever()
    finally:
        server.shutdown_active_processes()  # type: ignore[attr-defined]
        server.server_close()
        if args.ready_file:
            Path(args.ready_file).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
