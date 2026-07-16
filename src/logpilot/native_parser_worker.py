from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .native_parser import parse_c_family_file


def _response(request_id: str, **payload: Any) -> None:
    value = {"request_id": request_id, **payload}
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_request(request: dict[str, Any]) -> None:
    request_id = str(request.get("request_id", ""))
    try:
        repo_root = Path(str(request["repo_root"])).expanduser().resolve()
        file_path = Path(str(request["file_path"])).expanduser().resolve()
        language = str(request["language"])
        if not request_id or language not in {"c", "cpp"}:
            raise ValueError("请求字段无效。")
        file_path.relative_to(repo_root)
    except (KeyError, OSError, ValueError) as exc:
        _response(
            request_id,
            status="error",
            error_kind="protocol_error",
            message=f"解析请求无效：{exc}",
        )
        return

    try:
        logs, targets = parse_c_family_file(file_path, repo_root, language)
    except Exception as exc:
        _response(
            request_id,
            status="error",
            error_kind="parse_error",
            message=f"{type(exc).__name__}: {exc}",
        )
        return
    _response(
        request_id,
        status="ok",
        logs=[asdict(log) for log in logs],
        targets=[asdict(target) for target in targets],
    )


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _response("", status="error", error_kind="protocol_error", message=f"请求不是有效 JSON：{exc}")
            continue
        if not isinstance(request, dict):
            _response("", status="error", error_kind="protocol_error", message="请求必须是 JSON 对象。")
            continue
        _handle_request(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
