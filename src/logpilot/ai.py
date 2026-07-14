from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .config import AiConfig
from .models import AiTrace, Issue, LogCall, Severity


class AiProvider:
    def analyze(self, log: LogCall, config: AiConfig) -> AiTrace:
        raise NotImplementedError


class DisabledProvider(AiProvider):
    def analyze(self, log: LogCall, config: AiConfig) -> AiTrace:
        return AiTrace(log_call_id=log.id, status="skipped", prompt="")


class OpenAICompatibleProvider(AiProvider):
    def analyze(self, log: LogCall, config: AiConfig) -> AiTrace:
        prompt = build_prompt(log)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return AiTrace(log_call_id=log.id, status="error", prompt=prompt, error="OPENAI_API_KEY is not set.")

        payload = {
            "model": os.getenv("LOGPILOT_MODEL", config.model),
            "messages": [
                {
                    "role": "system",
                    "content": "You review code logs. Return compact JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        url = os.getenv("LOGPILOT_BASE_URL", config.base_url).rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
            return AiTrace(log_call_id=log.id, status="ok", prompt=prompt, raw_response=raw)
        except (urllib.error.URLError, TimeoutError) as exc:
            return AiTrace(log_call_id=log.id, status="error", prompt=prompt, error=str(exc))


class MockProvider(AiProvider):
    def analyze(self, log: LogCall, config: AiConfig) -> AiTrace:
        prompt = build_prompt(log)
        raw = json.dumps({"risk": "low", "suggestion": "mocked"}, ensure_ascii=False)
        return AiTrace(log_call_id=log.id, status="ok", prompt=prompt, raw_response=raw)


def analyze_with_ai(logs: list[LogCall], config: AiConfig, provider: AiProvider | None = None) -> tuple[list[Issue], list[AiTrace]]:
    if not config.enabled:
        return [], []

    selected = provider or OpenAICompatibleProvider()
    traces: list[AiTrace] = []
    issues: list[Issue] = []
    for log in logs:
        trace = selected.analyze(log, config)
        traces.append(trace)
        issue = _issue_from_trace(log, trace)
        if issue:
            issues.append(issue)
    return issues, traces


def build_prompt(log: LogCall) -> str:
    payload = {
        "task": "Analyze whether this log is useful, missing fields, duplicated, or risky.",
        "expected_json": {
            "has_issue": True,
            "severity": "low|medium|high",
            "title": "short title",
            "reason": "why",
            "suggestion": "what to change",
        },
        "log": {
            "file_path": log.file_path,
            "line": log.line,
            "language": log.language,
            "level": log.level,
            "callee": log.callee,
            "message": log.message,
            "context": log.context,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _issue_from_trace(log: LogCall, trace: AiTrace) -> Issue | None:
    if trace.status != "ok" or not trace.raw_response:
        return None
    try:
        raw = json.loads(trace.raw_response)
        content = raw
        if isinstance(raw, dict) and "choices" in raw:
            content_text = raw["choices"][0]["message"]["content"]
            content = json.loads(content_text)
        if not isinstance(content, dict) or not content.get("has_issue"):
            return None
        severity = Severity(str(content.get("severity", "low")).lower())
        return Issue(
            id=f"ai:{log.id}",
            file_path=log.file_path,
            line=log.line,
            severity=severity,
            kind="ai_log_quality",
            title=str(content.get("title", "AI log quality finding")),
            reason=str(content.get("reason", "AI provider reported a log quality concern.")),
            suggestion=str(content.get("suggestion", "Review this log.")),
            source="ai",
            log_call_id=log.id,
            patch_action=None,
        )
    except Exception:
        return None
