from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logpilot.ai import analyze_with_ai
from logpilot.config import AiConfig
from logpilot.models import LogCall
from logpilot.runtime import RuntimeExecution, RuntimeExecutor, RuntimeInfo, RuntimeRegistry


class RuntimeTests(unittest.TestCase):
    def test_registry_detects_and_versions_built_in_runtimes(self) -> None:
        def runner(command, **kwargs):
            name = Path(command[0]).stem.lower()
            return subprocess.CompletedProcess(command, 0, stdout=f"{name} 1.2.3\n", stderr="")

        registry = RuntimeRegistry(
            which=lambda command: str(ROOT / f"{command}.cmd"),
            runner=runner,
        )
        runtimes = registry.refresh()

        self.assertEqual([runtime.id for runtime in runtimes], ["codex", "claude"])
        self.assertTrue(all(runtime.status == "online" for runtime in runtimes))
        self.assertEqual(registry.resolve("auto").id, "codex")

    def test_codex_executor_uses_read_only_structured_mode(self) -> None:
        captured: list[str] = []

        def runner(command, **kwargs):
            captured.extend(command)
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text('{"findings": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runtime = RuntimeInfo("codex", "Codex", "codex", "online", "codex.cmd", "codex-cli 1.0")
        result = RuntimeExecutor(runner=runner).execute(
            runtime,
            "review",
            ROOT,
            {"type": "object"},
            timeout_seconds=10,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(json.loads(result.raw_response), {"findings": []})
        self.assertIn("--ephemeral", captured)
        self.assertEqual(captured[captured.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", captured)

    def test_claude_executor_disables_tools_and_reads_structured_output(self) -> None:
        captured: list[str] = []

        def runner(command, **kwargs):
            captured.extend(command)
            payload = {"structured_output": {"findings": []}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

        runtime = RuntimeInfo("claude", "Claude", "claude", "online", "claude.cmd", "2.1.0")
        result = RuntimeExecutor(runner=runner).execute(
            runtime,
            "review",
            ROOT,
            {"type": "object"},
            timeout_seconds=10,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(captured[captured.index("--tools") + 1], "")
        self.assertEqual(captured[captured.index("--permission-mode") + 1], "plan")
        self.assertEqual(json.loads(result.raw_response), {"findings": []})

    def test_batch_analysis_maps_runtime_findings_to_issues(self) -> None:
        log = LogCall(
            id="service.py:3:logger.info",
            file_path="service.py",
            line=3,
            column=4,
            language="python",
            level="info",
            callee="logger.info",
            message="start",
            context="logger.info('start')",
            source_line="logger.info('start')",
        )
        runtime = RuntimeInfo("codex", "Codex", "codex", "online", "codex.cmd", "codex-cli 1.0")

        class Registry:
            def resolve(self, runtime_id):
                return runtime

        class Executor:
            def execute(self, *args, **kwargs):
                payload = {
                    "findings": [
                        {
                            "log_call_id": log.id,
                            "has_issue": True,
                            "severity": "low",
                            "title": "缺少业务语义",
                            "reason": "无法判断处理对象。",
                            "suggestion": "增加业务标识字段。",
                        }
                    ]
                }
                return RuntimeExecution("codex", "ok", json.dumps(payload, ensure_ascii=False), duration_ms=25)

        issues, traces = analyze_with_ai(
            [log],
            AiConfig(enabled=True),
            ROOT,
            registry=Registry(),
            executor=Executor(),
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].source, "runtime:codex")
        self.assertEqual(traces[0].runtime_version, "codex-cli 1.0")
        self.assertEqual(traces[0].duration_ms, 25)

    def test_executor_reports_timeout(self) -> None:
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        runtime = RuntimeInfo("claude", "Claude", "claude", "online", "claude.cmd", "2.1.0")
        result = RuntimeExecutor(runner=runner).execute(
            runtime,
            "review",
            ROOT,
            {"type": "object"},
            timeout_seconds=1,
        )
        self.assertEqual(result.status, "timeout")
        self.assertIn("1 秒", result.error)


if __name__ == "__main__":
    unittest.main()
