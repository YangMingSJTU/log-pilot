from __future__ import annotations

import json
import os
import string
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_EXCLUDES
from .models import LogCall
from .parsers import LANGUAGE_BY_SUFFIX
from .storage import initialize_repository_storage, repository_data_dir


LANGUAGE_LABELS = {
    "python": "Python",
    "java": "Java",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
}
LANGUAGE_EXTENSIONS = {
    language: sorted(suffix for suffix, mapped in LANGUAGE_BY_SUFFIX.items() if mapped == language)
    for language in LANGUAGE_LABELS
}
BUILTIN_TEMPLATES = {
    "python": '{logger}.exception("{event}")',
    "java": '{logger}.error("{event}", {exception})',
    "javascript": '{logger}.error("{event}", {exception})',
    "typescript": '{logger}.error("{event}", {exception})',
}
ALLOWED_PLACEHOLDERS = {"event", "exception", "function", "logger", "indent"}


@dataclass(slots=True)
class RepositorySettings:
    language_mode: str = "auto"
    selected_languages: list[str] = field(default_factory=list)
    templates: dict[str, str] = field(default_factory=dict)
    language_presets: list[dict[str, Any]] = field(default_factory=list)
    template_presets: list[dict[str, Any]] = field(default_factory=list)
    active_language_preset: str = "auto"
    active_template_preset: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsError(ValueError):
    pass


def load_repository_settings(repo_root: Path) -> RepositorySettings:
    path = repository_data_dir(repo_root) / "settings.json"
    if not path.is_file():
        return RepositorySettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RepositorySettings()
    if not isinstance(payload, dict):
        return RepositorySettings()
    try:
        return normalize_settings(payload)
    except SettingsError:
        return RepositorySettings()


def save_repository_settings(repo_root: Path, payload: dict[str, Any]) -> RepositorySettings:
    settings = normalize_settings(payload)
    data_dir = initialize_repository_storage(repo_root)
    (data_dir / "settings.json").write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return settings


def normalize_settings(payload: dict[str, Any]) -> RepositorySettings:
    mode = str(payload.get("language_mode", "auto")).strip().lower()
    if mode not in {"auto", "custom"}:
        raise SettingsError("语言模式必须是 auto 或 custom。")

    raw_languages = payload.get("selected_languages", [])
    if not isinstance(raw_languages, list):
        raise SettingsError("自定义语言必须是列表。")
    selected = [str(item).lower() for item in raw_languages if str(item).lower() in LANGUAGE_LABELS]
    selected = list(dict.fromkeys(selected))
    if mode == "custom" and not selected:
        raise SettingsError("自定义模式下请至少选择一种语言。")

    raw_templates = payload.get("templates", {})
    if not isinstance(raw_templates, dict):
        raise SettingsError("日志模板必须是对象。")
    templates: dict[str, str] = {}
    for language, value in raw_templates.items():
        key = str(language).lower()
        if key not in LANGUAGE_LABELS or not str(value).strip():
            continue
        templates[key] = validate_template(str(value))

    language_presets = _normalize_language_presets(payload.get("language_presets", []))
    template_presets = _normalize_template_presets(payload.get("template_presets", []))
    active_language_preset = _normalize_active_preset(
        payload.get("active_language_preset", "auto"),
        language_presets,
    )
    active_template_preset = _normalize_active_preset(
        payload.get("active_template_preset", "auto"),
        template_presets,
    )
    return RepositorySettings(
        language_mode=mode,
        selected_languages=selected,
        templates=templates,
        language_presets=language_presets,
        template_presets=template_presets,
        active_language_preset=active_language_preset,
        active_template_preset=active_template_preset,
    )


def _normalize_language_presets(raw_presets: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_presets, list):
        raise SettingsError("语言方案必须是列表。")
    presets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_presets[:50]:
        if not isinstance(item, dict):
            continue
        identifier = _normalize_preset_id(item.get("id"))
        if identifier in seen:
            continue
        name = _normalize_preset_name(item.get("name"))
        raw_languages = item.get("languages", [])
        if not isinstance(raw_languages, list):
            raise SettingsError("语言方案内容必须是列表。")
        languages = list(
            dict.fromkeys(
                str(language).lower()
                for language in raw_languages
                if str(language).lower() in LANGUAGE_LABELS
            )
        )
        if not languages:
            raise SettingsError(f"语言方案“{name}”至少需要一种语言。")
        presets.append({"id": identifier, "name": name, "languages": languages})
        seen.add(identifier)
    return presets


def _normalize_template_presets(raw_presets: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_presets, list):
        raise SettingsError("模板方案必须是列表。")
    presets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_presets[:50]:
        if not isinstance(item, dict):
            continue
        identifier = _normalize_preset_id(item.get("id"))
        if identifier in seen:
            continue
        name = _normalize_preset_name(item.get("name"))
        raw_templates = item.get("templates", {})
        if not isinstance(raw_templates, dict):
            raise SettingsError("模板方案内容必须是对象。")
        templates: dict[str, str] = {}
        for language, value in raw_templates.items():
            key = str(language).lower()
            if key in LANGUAGE_LABELS and str(value).strip():
                templates[key] = validate_template(str(value))
        if not templates:
            raise SettingsError(f"模板方案“{name}”至少需要一个模板。")
        presets.append({"id": identifier, "name": name, "templates": templates})
        seen.add(identifier)
    return presets


def _normalize_preset_id(value: Any) -> str:
    identifier = str(value or "").strip()
    safe = identifier.replace("-", "").replace("_", "")
    if not identifier or len(identifier) > 80 or not safe.isalnum():
        raise SettingsError("方案标识格式无效。")
    return identifier


def _normalize_preset_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 40:
        raise SettingsError("方案名称必须是 1 到 40 个字符。")
    return name


def _normalize_active_preset(value: Any, presets: list[dict[str, Any]]) -> str:
    identifier = str(value or "auto").strip()
    if identifier == "auto" or any(item["id"] == identifier for item in presets):
        return identifier
    return "auto"


def validate_template(template: str) -> str:
    cleaned = template.strip()
    if not cleaned or len(cleaned) > 500 or "\n" in cleaned or "\r" in cleaned:
        raise SettingsError("日志模板必须是 1 到 500 个字符的单行内容。")
    try:
        placeholders = {
            field_name
            for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(cleaned)
            if field_name
        }
    except ValueError as exc:
        raise SettingsError("日志模板中的花括号不完整。") from exc
    unknown = placeholders.difference(ALLOWED_PLACEHOLDERS)
    if unknown:
        raise SettingsError(f"日志模板包含未知变量：{', '.join(sorted(unknown))}")
    if "event" not in placeholders:
        raise SettingsError("日志模板必须包含 {event}。")
    return cleaned


def selected_extensions(settings: RepositorySettings) -> list[str] | None:
    if settings.language_mode != "custom":
        return None
    return sorted(
        {
            extension
            for language in settings.selected_languages
            for extension in LANGUAGE_EXTENSIONS[language]
        }
    )


def build_language_profile(
    repo_root: Path,
    logs: list[LogCall],
    excludes: list[str] | None = None,
    file_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    excluded = set(excludes or DEFAULT_EXCLUDES)
    counts: Counter[str] = Counter(file_counts or {})
    if file_counts is None:
        for current_root, directories, filenames in os.walk(repo_root):
            directories[:] = [directory for directory in directories if directory not in excluded]
            current = Path(current_root)
            for filename in filenames:
                path = current / filename
                language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
                if language:
                    counts[language] += 1

    log_counts = Counter(log.language for log in logs)
    detected = [
        {
            "id": language,
            "label": label,
            "file_count": counts[language],
            "log_count": log_counts[language],
            "recommended": counts[language] > 0,
            "automatic_fix": language == "python",
        }
        for language, label in LANGUAGE_LABELS.items()
    ]
    recommendations = {
        language: _recommend_template(language, logs)
        for language in LANGUAGE_LABELS
    }
    profile = {
        "detected_languages": detected,
        "template_recommendations": recommendations,
    }
    data_dir = initialize_repository_storage(repo_root)
    (data_dir / "language-profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return profile


def load_language_profile(repo_root: Path) -> dict[str, Any]:
    path = repository_data_dir(repo_root) / "language-profile.json"
    if not path.is_file():
        return {"detected_languages": [], "template_recommendations": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"detected_languages": [], "template_recommendations": {}}
    return payload if isinstance(payload, dict) else {"detected_languages": [], "template_recommendations": {}}


def settings_payload(repo_root: Path) -> dict[str, Any]:
    return {
        "settings": load_repository_settings(repo_root).to_dict(),
        "profile": load_language_profile(repo_root),
        "languages": [
            {
                "id": language,
                "label": label,
                "builtin_template": BUILTIN_TEMPLATES[language],
                "automatic_fix": language == "python",
            }
            for language, label in LANGUAGE_LABELS.items()
        ],
    }


def resolve_template(
    language: str,
    settings: RepositorySettings,
    profile: dict[str, Any],
) -> tuple[str, str]:
    fixed = settings.templates.get(language, "").strip()
    if fixed:
        return fixed, "fixed"
    recommendation = profile.get("template_recommendations", {}).get(language, {})
    if isinstance(recommendation, dict) and recommendation.get("source") == "repository":
        template = str(recommendation.get("template", "")).strip()
        if template:
            return template, "repository"
    return BUILTIN_TEMPLATES[language], "builtin"


def _recommend_template(language: str, logs: list[LogCall]) -> dict[str, Any]:
    candidates = [
        log
        for log in logs
        if log.language == language and log.level in {"exception", "error", "critical"}
    ]
    if not candidates:
        return {
            "template": BUILTIN_TEMPLATES[language],
            "source": "builtin",
            "confidence": 0,
            "sample": "",
            "logger": "logger",
        }

    method_counts = Counter(log.callee.rsplit(".", 1)[-1] for log in candidates)
    if language == "python" and method_counts["exception"]:
        method = "exception"
    else:
        method = method_counts.most_common(1)[0][0]
    method_logs = [log for log in candidates if log.callee.rsplit(".", 1)[-1] == method]
    logger_counts = Counter(log.callee.rsplit(".", 1)[0] for log in method_logs if "." in log.callee)
    logger_name = logger_counts.most_common(1)[0][0] if logger_counts else "logger"
    if language == "python" and method == "error" and any("exc_info=True" in log.source_line for log in method_logs):
        template = '{logger}.error("{event}", exc_info=True)'
    elif language == "python":
        template = f'{{logger}}.{method}("{{event}}")'
    else:
        template = f'{{logger}}.{method}("{{event}}", {{exception}})'
    return {
        "template": template,
        "source": "repository",
        "confidence": round(min(0.98, 0.55 + len(method_logs) / max(10, len(candidates) * 2)), 2),
        "sample": method_logs[0].source_line.strip(),
        "logger": logger_name,
    }
