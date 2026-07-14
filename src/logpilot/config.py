from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_FORBIDDEN_LOGS = ["print", "console.log", "System.out.println"]
DEFAULT_REQUIRED_FIELDS = ["request_id"]
DEFAULT_SENSITIVE_FIELDS = ["password", "passwd", "secret", "token", "api_key"]
DEFAULT_EXCLUDES = [
    ".git",
    ".hg",
    ".svn",
    ".logpilot",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
]


@dataclass(slots=True)
class RulesConfig:
    forbidden_logs: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN_LOGS))
    required_fields: list[str] = field(default_factory=lambda: list(DEFAULT_REQUIRED_FIELDS))
    sensitive_fields: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_FIELDS))


@dataclass(slots=True)
class AiConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"


@dataclass(slots=True)
class ScanConfig:
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    include_extensions: list[str] = field(
        default_factory=lambda: [".py", ".java", ".js", ".jsx", ".ts", ".tsx"]
    )


@dataclass(slots=True)
class LogPilotConfig:
    rules: RulesConfig = field(default_factory=RulesConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)


def load_config(repo_root: Path, config_path: Path | None = None) -> LogPilotConfig:
    path = config_path or repo_root / ".logpilot.yaml"
    if not path.exists():
        return LogPilotConfig()

    raw = path.read_text(encoding="utf-8")
    data = _load_yaml_like(raw)
    return _config_from_dict(data)


def _config_from_dict(data: dict[str, Any]) -> LogPilotConfig:
    config = LogPilotConfig()
    rules = data.get("rules", {})
    ai = data.get("ai", {})
    scan = data.get("scan", {})

    if isinstance(rules, dict):
        config.rules.forbidden_logs = _string_list(rules.get("forbidden_logs"), config.rules.forbidden_logs)
        config.rules.required_fields = _string_list(rules.get("required_fields"), config.rules.required_fields)
        config.rules.sensitive_fields = _string_list(rules.get("sensitive_fields"), config.rules.sensitive_fields)

    if isinstance(ai, dict):
        config.ai.enabled = _bool_value(ai.get("enabled"), config.ai.enabled)
        config.ai.provider = str(ai.get("provider", config.ai.provider))
        config.ai.model = str(ai.get("model", config.ai.model))
        config.ai.base_url = str(ai.get("base_url", config.ai.base_url))

    if isinstance(scan, dict):
        config.scan.exclude = _string_list(scan.get("exclude"), config.scan.exclude)
        config.scan.include_extensions = _string_list(scan.get("include_extensions"), config.scan.include_extensions)

    return config


def _load_yaml_like(raw: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return _minimal_yaml(raw)


def _minimal_yaml(raw: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[str]]] = [(-1, root)]
    last_key_by_indent: dict[int, str] = {}

    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if text.startswith("- "):
            value = _scalar(text[2:].strip())
            if isinstance(parent, list):
                parent.append(str(value))
            continue
        if ":" not in text or not isinstance(parent, dict):
            continue
        key, value_text = text.split(":", 1)
        key = key.strip()
        value_text = value_text.strip()
        if value_text:
            parent[key] = _scalar(value_text)
        else:
            container: dict[str, Any] | list[str]
            next_is_list = _next_non_empty(raw.splitlines(), line).strip().startswith("- ")
            container = [] if next_is_list else {}
            parent[key] = container
            stack.append((indent, container))
            last_key_by_indent[indent] = key
    return root


def _next_non_empty(lines: list[str], current_line: str) -> str:
    found_current = False
    for line in lines:
        if not found_current:
            found_current = line == current_line
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            return line
    return ""


def _scalar(value: str) -> Any:
    cleaned = value.strip().strip('"').strip("'")
    if cleaned.lower() in {"true", "yes", "on"}:
        return True
    if cleaned.lower() in {"false", "no", "off"}:
        return False
    return cleaned


def _string_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return list(default)


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return default
