from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    AiTrace,
    AnalysisTarget,
    ExcludedMapping,
    Issue,
    LogCall,
    ParseFailure,
    ScanReport,
)
from .planning import ScanPlan, ScanModule


SCHEMA_VERSION = 1


class RunResultStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def for_run(cls, data_dir: Path, run_id: str) -> "RunResultStore":
        return cls(data_dir / "runs" / run_id / "results.sqlite3")

    def initialize_run(self, plan: ScanPlan, modules: list[ScanModule], runtime_id: str, depth: str) -> None:
        selected_ids = {module.id for module in modules}
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs(
                    run_id, repository, plan_id, status, created_at, updated_at,
                    runtime_id, depth, source_files, source_bytes, selected_modules,
                    total_modules, scope, summary_json, error
                ) VALUES(?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '')
                """,
                (
                    self.run_id,
                    plan.repository,
                    plan.id,
                    _now(),
                    _now(),
                    runtime_id,
                    depth,
                    plan.source_files,
                    plan.total_bytes,
                    len(modules),
                    len(plan.modules),
                    "repository" if len(modules) == len(plan.modules) else "selected_modules",
                ),
            )
            connection.execute("DELETE FROM modules WHERE run_id = ?", (self.run_id,))
            connection.execute("DELETE FROM chunks WHERE run_id = ?", (self.run_id,))
            for module in plan.modules:
                status = "waiting" if module.id in selected_ids else "skipped"
                connection.execute(
                    """
                    INSERT INTO modules(
                        run_id, module_id, path, name, status, selected, file_count,
                        total_bytes, languages_json, completed_chunks, total_chunks,
                        log_count, issue_count, error
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, '')
                    """,
                    (
                        self.run_id,
                        module.id,
                        module.path,
                        module.name,
                        status,
                        int(module.id in selected_ids),
                        module.file_count,
                        module.total_bytes,
                        json.dumps(module.languages, ensure_ascii=False),
                        len(module.chunks),
                    ),
                )
                if module.id not in selected_ids:
                    continue
                for chunk in module.chunks:
                    connection.execute(
                        """
                        INSERT INTO chunks(
                            run_id, chunk_id, module_id, chunk_index, status,
                            file_count, total_bytes, error, updated_at
                        ) VALUES(?, ?, ?, ?, 'waiting', ?, ?, '', ?)
                        """,
                        (
                            self.run_id,
                            chunk.id,
                            module.id,
                            chunk.index,
                            chunk.file_count,
                            chunk.total_bytes,
                            _now(),
                        ),
                    )
            for item in plan.skipped_large_files:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO files(
                        run_id, module_id, chunk_id, path, language, size, status, error
                    ) VALUES(?, '', '', ?, ?, ?, 'skipped_large', '文件超过 10 MiB')
                    """,
                    (self.run_id, item.path, item.language, item.size),
                )
            connection.execute(
                "INSERT OR REPLACE INTO run_data(run_id,key,payload_json) VALUES(?,?,?)",
                (
                    self.run_id,
                    "excluded_mappings",
                    json.dumps(
                        [asdict(item) for item in plan.excluded_mappings],
                        ensure_ascii=False,
                    ),
                ),
            )

    @property
    def run_id(self) -> str:
        return self.database.parent.name

    def start_run(self) -> None:
        self._set_run_status("running")

    def finish_run(self, summary: dict[str, Any], status: str = "completed", error: str = "") -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status=?, summary_json=?, error=?, updated_at=? WHERE run_id=?",
                (status, json.dumps(summary, ensure_ascii=False), error, _now(), self.run_id),
            )

    def mark_running_interrupted(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE chunks SET status='interrupted', updated_at=? WHERE run_id=? AND status='running'",
                (_now(), self.run_id),
            )
            connection.execute(
                "UPDATE modules SET status='interrupted' WHERE run_id=? AND status IN ('scanning','ai')",
                (self.run_id,),
            )
            connection.execute(
                "UPDATE runs SET status='interrupted', updated_at=? WHERE run_id=? AND status='running'",
                (_now(), self.run_id),
            )

    def begin_chunk(self, module_id: str, chunk_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE modules SET status='scanning' WHERE run_id=? AND module_id=?",
                (self.run_id, module_id),
            )
            connection.execute(
                "UPDATE chunks SET status='running', error='', updated_at=? WHERE run_id=? AND chunk_id=?",
                (_now(), self.run_id, chunk_id),
            )

    def replace_chunk(
        self,
        module_id: str,
        chunk_id: str,
        files: Iterable[dict[str, Any]],
        logs: Iterable[LogCall],
        targets: Iterable[AnalysisTarget],
        issues: Iterable[Issue],
        failures: Iterable[ParseFailure],
    ) -> None:
        with self.transaction() as connection:
            self._delete_chunk(connection, chunk_id)
            for item in files:
                connection.execute(
                    """
                    INSERT INTO files(run_id,module_id,chunk_id,path,language,size,status,error)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.run_id, module_id, chunk_id, item["path"], item["language"],
                        int(item.get("size", 0)), item.get("status", "analyzed"), item.get("error", ""),
                    ),
                )
            self._insert_logs(connection, module_id, chunk_id, logs)
            self._insert_targets(connection, module_id, chunk_id, targets)
            self._insert_issues(connection, module_id, chunk_id, issues)
            for failure in failures:
                connection.execute(
                    """
                    INSERT INTO parse_failures(
                        run_id,module_id,chunk_id,file_path,language,error_kind,message,
                        worker_exit_code,recoverable,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.run_id, module_id, chunk_id, failure.file_path, failure.language,
                        failure.error_kind, failure.message, failure.worker_exit_code,
                        int(failure.recoverable), json.dumps(asdict(failure), ensure_ascii=False),
                    ),
                )
            connection.execute(
                "UPDATE chunks SET status='completed', error='', updated_at=? WHERE run_id=? AND chunk_id=?",
                (_now(), self.run_id, chunk_id),
            )
            self._refresh_module_counts(connection, module_id)

    def fail_chunk(self, module_id: str, chunk_id: str, error: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE chunks SET status='failed', error=?, updated_at=? WHERE run_id=? AND chunk_id=?",
                (error, _now(), self.run_id, chunk_id),
            )
            connection.execute(
                "UPDATE modules SET status='failed', error=? WHERE run_id=? AND module_id=?",
                (error, self.run_id, module_id),
            )

    def record_chunk_failure(
        self,
        module_id: str,
        chunk_id: str,
        files: Iterable[dict[str, Any]],
        error: str,
    ) -> None:
        with self.transaction() as connection:
            self._delete_chunk(connection, chunk_id)
            for item in files:
                connection.execute(
                    """
                    INSERT INTO files(run_id,module_id,chunk_id,path,language,size,status,error)
                    VALUES(?,?,?,?,?,?,'failed',?)
                    """,
                    (
                        self.run_id, module_id, chunk_id, item["path"], item["language"],
                        int(item.get("size", 0)), error,
                    ),
                )
            connection.execute(
                "UPDATE chunks SET status='failed', error=?, updated_at=? WHERE run_id=? AND chunk_id=?",
                (error, _now(), self.run_id, chunk_id),
            )
            connection.execute(
                "UPDATE modules SET status='failed', error=? WHERE run_id=? AND module_id=?",
                (error, self.run_id, module_id),
            )

    def begin_module_ai(self, module_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE modules SET status='ai' WHERE run_id=? AND module_id=? AND status!='failed'",
                (self.run_id, module_id),
            )

    def append_ai_results(
        self,
        module_id: str,
        logs: Iterable[LogCall],
        issues: Iterable[Issue],
        traces: Iterable[AiTrace],
    ) -> None:
        with self.transaction() as connection:
            self._insert_logs(connection, module_id, "ai", logs)
            self._insert_issues(connection, module_id, "ai", issues)
            for trace in traces:
                prompt = gzip.compress(trace.prompt.encode("utf-8"))
                response = gzip.compress(trace.raw_response.encode("utf-8"))
                connection.execute(
                    """
                    INSERT INTO ai_records(
                        run_id,module_id,log_call_id,status,runtime_id,runtime_version,
                        duration_ms,task,prompt_gzip,response_gzip,error
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.run_id, module_id, trace.log_call_id, trace.status,
                        trace.runtime_id, trace.runtime_version, trace.duration_ms,
                        trace.task, prompt, response, trace.error,
                    ),
                )
            self._refresh_module_counts(connection, module_id)

    def complete_module(self, module_id: str) -> None:
        with self.transaction() as connection:
            self._refresh_module_counts(connection, module_id)
            connection.execute(
                "UPDATE modules SET status='completed', error='' WHERE run_id=? AND module_id=? AND status!='failed'",
                (self.run_id, module_id),
            )

    def progress(self) -> dict[str, Any]:
        with self.connection() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            if run is None:
                raise FileNotFoundError(f"分析记录不存在：{self.run_id}")
            modules = [self._module_payload(row) for row in connection.execute(
                "SELECT * FROM modules WHERE run_id=? ORDER BY path", (self.run_id,)
            )]
            chunks = connection.execute(
                "SELECT status, COUNT(*) AS amount FROM chunks WHERE run_id=? GROUP BY status",
                (self.run_id,),
            ).fetchall()
            counts = {row["status"]: row["amount"] for row in chunks}
            total = sum(counts.values())
            completed = counts.get("completed", 0)
            return {
                "run_id": self.run_id,
                "repository": run["repository"],
                "status": run["status"],
                "error": run["error"],
                "completed": completed,
                "total": total,
                "percent": round(completed / total * 100) if total else 0,
                "updated_at": run["updated_at"],
                "summary": json.loads(run["summary_json"] or "{}"),
                "modules": modules,
            }

    def run_detail(self) -> dict[str, Any]:
        payload = self.progress()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM parse_failures WHERE run_id=? ORDER BY file_path LIMIT 5",
                (self.run_id,),
            ).fetchall()
            count = connection.execute(
                "SELECT COUNT(*) FROM parse_failures WHERE run_id=?",
                (self.run_id,),
            ).fetchone()[0]
        payload["parse_failures"] = [json.loads(row[0]) for row in rows]
        payload["parse_failure_count"] = int(count)
        payload["language_insights"] = self.get_run_data("language_insights", [])
        payload["excluded_mappings"] = self.get_run_data("excluded_mappings", [])
        return payload

    def module_logs(self, module_id: str, limit: int, offset: int = 0) -> list[LogCall]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM logs WHERE run_id=? AND module_id=? ORDER BY file_path,line LIMIT ? OFFSET ?",
                (self.run_id, module_id, limit, offset),
            )
            return [LogCall(**json.loads(row[0])) for row in rows]

    def module_targets(self, module_id: str, limit: int, offset: int = 0) -> list[AnalysisTarget]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM analysis_targets WHERE run_id=? AND module_id=? ORDER BY file_path,start_line LIMIT ? OFFSET ?",
                (self.run_id, module_id, limit, offset),
            )
            return [AnalysisTarget(**json.loads(row[0])) for row in rows]

    def completed_chunk_ids(self) -> set[str]:
        with self.connection() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT chunk_id FROM chunks WHERE run_id=? AND status='completed'",
                    (self.run_id,),
                )
            }

    def selected_module_ids(self) -> list[str]:
        with self.connection() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT module_id FROM modules WHERE run_id=? AND selected=1 ORDER BY path",
                    (self.run_id,),
                )
            ]

    def aggregate_counts(self) -> dict[str, Any]:
        with self.connection() as connection:
            severity = {
                str(row["severity"]): int(row["amount"])
                for row in connection.execute(
                    "SELECT severity,COUNT(*) AS amount FROM issues WHERE run_id=? GROUP BY severity",
                    (self.run_id,),
                )
            }
            languages = {
                str(row["language"]): int(row["amount"])
                for row in connection.execute(
                    "SELECT language,COUNT(*) AS amount FROM files WHERE run_id=? AND status='analyzed' GROUP BY language",
                    (self.run_id,),
                )
            }
            failed_languages = {
                str(row["language"]): int(row["amount"])
                for row in connection.execute(
                    "SELECT language,COUNT(*) AS amount FROM files WHERE run_id=? AND status='failed' GROUP BY language",
                    (self.run_id,),
                )
            }
            log_languages = {
                str(row["language"]): int(row["amount"])
                for row in connection.execute(
                    "SELECT language,COUNT(*) AS amount FROM logs WHERE run_id=? GROUP BY language",
                    (self.run_id,),
                )
            }
            return {
                "files_scanned": sum(languages.values()),
                "analyzed_languages": languages,
                "failed_languages": failed_languages,
                "log_languages": log_languages,
                "log_count": connection.execute(
                    "SELECT COUNT(*) FROM logs WHERE run_id=?", (self.run_id,)
                ).fetchone()[0],
                "issue_count": connection.execute(
                    "SELECT COUNT(*) FROM issues WHERE run_id=?", (self.run_id,)
                ).fetchone()[0],
                "severity_counts": severity,
                "parse_failure_count": connection.execute(
                    "SELECT COUNT(*) FROM parse_failures WHERE run_id=?", (self.run_id,)
                ).fetchone()[0],
            }

    def set_run_data(self, key: str, payload: Any) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO run_data(run_id,key,payload_json) VALUES(?,?,?)",
                (self.run_id, key, json.dumps(payload, ensure_ascii=False)),
            )

    def get_run_data(self, key: str, default: Any = None) -> Any:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM run_data WHERE run_id=? AND key=?",
                (self.run_id, key),
            ).fetchone()
        return json.loads(row[0]) if row else default

    def query_issues(
        self,
        *,
        module_id: str = "",
        severity: str = "",
        action: str = "",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        clauses = ["run_id = ?"]
        params: list[Any] = [self.run_id]
        if module_id:
            clauses.append("module_id = ?")
            params.append(module_id)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if search:
            clauses.append("search_text LIKE ?")
            params.append(f"%{search.casefold()}%")
        where = " AND ".join(clauses)
        with self.connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM issues WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT payload_json FROM issues WHERE {where}
                ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                         file_path, line, issue_id
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            issues = [json.loads(row[0]) for row in rows]
            log_ids = [item.get("log_call_id") for item in issues if item.get("log_call_id")]
            logs: dict[str, Any] = {}
            if log_ids:
                placeholders = ",".join("?" for _ in log_ids)
                for row in connection.execute(
                    f"SELECT log_id,payload_json FROM logs WHERE run_id=? AND log_id IN ({placeholders})",
                    [self.run_id, *log_ids],
                ):
                    logs[row["log_id"]] = json.loads(row["payload_json"])
        return {
            "run_id": self.run_id,
            "items": issues,
            "logs": logs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(issues) < total,
        }

    def load_report_dict(self) -> dict[str, Any]:
        with self.connection() as connection:
            run = connection.execute("SELECT summary_json FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            if run is None:
                raise FileNotFoundError(f"分析记录不存在：{self.run_id}")
            logs = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM logs WHERE run_id=? ORDER BY file_path,line", (self.run_id,)
            )]
            issues = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM issues WHERE run_id=? ORDER BY file_path,line", (self.run_id,)
            )]
            failures = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM parse_failures WHERE run_id=? ORDER BY file_path", (self.run_id,)
            )]
            traces = []
            for row in connection.execute("SELECT * FROM ai_records WHERE run_id=?", (self.run_id,)):
                traces.append(
                    {
                        "log_call_id": row["log_call_id"],
                        "status": row["status"],
                        "prompt": gzip.decompress(row["prompt_gzip"]).decode("utf-8"),
                        "raw_response": gzip.decompress(row["response_gzip"]).decode("utf-8"),
                        "error": row["error"],
                        "runtime_id": row["runtime_id"],
                        "runtime_version": row["runtime_version"],
                        "duration_ms": row["duration_ms"],
                        "task": row["task"],
                    }
                )
            return {
                "summary": json.loads(run["summary_json"] or "{}"),
                "logs": logs,
                "issues": issues,
                "ai_traces": traces,
                "language_insights": self.get_run_data("language_insights", []),
                "parse_failures": failures,
                "excluded_mappings": self.get_run_data("excluded_mappings", []),
            }

    def close_module_results(self, module_id: str) -> None:
        with self.transaction() as connection:
            for table in ("files", "logs", "analysis_targets", "issues", "fixes", "ai_records", "parse_failures"):
                connection.execute(f"DELETE FROM {table} WHERE run_id=? AND module_id=?", (self.run_id, module_id))
            connection.execute(
                "UPDATE chunks SET status='waiting', error='', updated_at=? WHERE run_id=? AND module_id=?",
                (_now(), self.run_id, module_id),
            )
            connection.execute(
                "UPDATE modules SET status='waiting', completed_chunks=0, log_count=0, issue_count=0, error='' WHERE run_id=? AND module_id=?",
                (self.run_id, module_id),
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    def _set_run_status(self, status: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (status, _now(), self.run_id),
            )

    def _delete_chunk(self, connection: sqlite3.Connection, chunk_id: str) -> None:
        for table in ("files", "logs", "analysis_targets", "issues", "fixes", "parse_failures"):
            connection.execute(
                f"DELETE FROM {table} WHERE run_id=? AND chunk_id=?",
                (self.run_id, chunk_id),
            )

    def _insert_logs(self, connection, module_id: str, chunk_id: str, logs: Iterable[LogCall]) -> None:
        for log in logs:
            connection.execute(
                """
                INSERT OR REPLACE INTO logs(
                    run_id,module_id,chunk_id,log_id,file_path,line,language,level,callee,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.run_id, module_id, chunk_id, log.id, log.file_path, log.line,
                    log.language, log.level, log.callee, json.dumps(asdict(log), ensure_ascii=False),
                ),
            )

    def _insert_targets(self, connection, module_id: str, chunk_id: str, targets: Iterable[AnalysisTarget]) -> None:
        for target in targets:
            connection.execute(
                """
                INSERT OR REPLACE INTO analysis_targets(
                    run_id,module_id,chunk_id,target_id,kind,file_path,start_line,payload_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    self.run_id, module_id, chunk_id, target.id, target.kind,
                    target.file_path, target.start_line, json.dumps(asdict(target), ensure_ascii=False),
                ),
            )

    def _insert_issues(self, connection, module_id: str, chunk_id: str, issues: Iterable[Issue]) -> None:
        for issue in issues:
            payload = asdict(issue)
            severity = issue.severity.value
            action = _issue_action(issue)
            search_text = " ".join(
                [issue.file_path, issue.title, issue.kind, issue.reason, issue.suggestion]
            ).casefold()
            connection.execute(
                """
                INSERT OR REPLACE INTO issues(
                    run_id,module_id,chunk_id,issue_id,file_path,line,severity,action,
                    kind,title,search_text,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.run_id, module_id, chunk_id, issue.id, issue.file_path,
                    issue.line, severity, action, issue.kind, issue.title,
                    search_text, json.dumps(payload, ensure_ascii=False),
                ),
            )
            if issue.fix:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO fixes(
                        run_id,module_id,chunk_id,fix_id,issue_id,action,file_path,start_line,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.run_id, module_id, chunk_id, issue.fix.id, issue.id,
                        issue.fix.action, issue.fix.file_path, issue.fix.start_line,
                        json.dumps(asdict(issue.fix), ensure_ascii=False),
                    ),
                )

    def _refresh_module_counts(self, connection: sqlite3.Connection, module_id: str) -> None:
        completed = connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE run_id=? AND module_id=? AND status='completed'",
            (self.run_id, module_id),
        ).fetchone()[0]
        logs = connection.execute(
            "SELECT COUNT(*) FROM logs WHERE run_id=? AND module_id=?",
            (self.run_id, module_id),
        ).fetchone()[0]
        issues = connection.execute(
            "SELECT COUNT(*) FROM issues WHERE run_id=? AND module_id=?",
            (self.run_id, module_id),
        ).fetchone()[0]
        connection.execute(
            "UPDATE modules SET completed_chunks=?,log_count=?,issue_count=? WHERE run_id=? AND module_id=?",
            (completed, logs, issues, self.run_id, module_id),
        )

    @staticmethod
    def _module_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["module_id"],
            "path": row["path"],
            "name": row["name"],
            "status": row["status"],
            "selected": bool(row["selected"]),
            "file_count": row["file_count"],
            "total_bytes": row["total_bytes"],
            "languages": json.loads(row["languages_json"] or "{}"),
            "completed_chunks": row["completed_chunks"],
            "total_chunks": row["total_chunks"],
            "log_count": row["log_count"],
            "issue_count": row["issue_count"],
            "error": row["error"],
        }


def report_from_dict(payload: dict[str, Any]) -> ScanReport:
    from .models import FixProposal, LanguageCoverage, ScanSummary, Severity

    summary_data = dict(payload["summary"])
    summary_data["language_coverage"] = [LanguageCoverage(**item) for item in summary_data.get("language_coverage", [])]
    summary = ScanSummary(**summary_data)
    logs = [LogCall(**item) for item in payload.get("logs", [])]
    issues = []
    for item in payload.get("issues", []):
        issue_data = dict(item)
        issue_data["severity"] = Severity(issue_data["severity"])
        if isinstance(issue_data.get("fix"), dict):
            issue_data["fix"] = FixProposal(**issue_data["fix"])
        issues.append(Issue(**issue_data))
    return ScanReport(
        summary=summary,
        logs=logs,
        issues=issues,
        ai_traces=[AiTrace(**item) for item in payload.get("ai_traces", [])],
        language_insights=list(payload.get("language_insights", [])),
        parse_failures=[ParseFailure(**item) for item in payload.get("parse_failures", [])],
        excluded_mappings=[
            ExcludedMapping(**item) for item in payload.get("excluded_mappings", [])
        ],
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _issue_action(issue: Issue) -> str:
    if issue.fix:
        if issue.fix.action == "delete":
            return "delete"
        if issue.fix.action == "insert_before":
            return "add"
        if issue.fix.action == "replace":
            return "modify" if issue.log_call_id else "add"
    if issue.kind in {"missing_exception_log", "ai_missing_log"}:
        return "add"
    if issue.patch_action == "delete" or issue.kind == "debug_log":
        return "delete"
    return "modify"


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY, repository TEXT NOT NULL, plan_id TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    runtime_id TEXT NOT NULL, depth TEXT NOT NULL, source_files INTEGER NOT NULL,
    source_bytes INTEGER NOT NULL, selected_modules INTEGER NOT NULL,
    total_modules INTEGER NOT NULL, scope TEXT NOT NULL, summary_json TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS modules(
    run_id TEXT NOT NULL, module_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
    status TEXT NOT NULL, selected INTEGER NOT NULL, file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL, languages_json TEXT NOT NULL,
    completed_chunks INTEGER NOT NULL, total_chunks INTEGER NOT NULL,
    log_count INTEGER NOT NULL, issue_count INTEGER NOT NULL, error TEXT NOT NULL,
    PRIMARY KEY(run_id,module_id)
);
CREATE TABLE IF NOT EXISTS chunks(
    run_id TEXT NOT NULL, chunk_id TEXT NOT NULL, module_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL, status TEXT NOT NULL, file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL, error TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id,chunk_id)
);
CREATE TABLE IF NOT EXISTS files(
    run_id TEXT NOT NULL, module_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    path TEXT NOT NULL, language TEXT NOT NULL, size INTEGER NOT NULL,
    status TEXT NOT NULL, error TEXT NOT NULL, PRIMARY KEY(run_id,path)
);
CREATE TABLE IF NOT EXISTS logs(
    run_id TEXT NOT NULL, module_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    log_id TEXT NOT NULL, file_path TEXT NOT NULL, line INTEGER NOT NULL,
    language TEXT NOT NULL, level TEXT NOT NULL, callee TEXT NOT NULL,
    payload_json TEXT NOT NULL, PRIMARY KEY(run_id,log_id)
);
CREATE TABLE IF NOT EXISTS analysis_targets(
    run_id TEXT NOT NULL, module_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    target_id TEXT NOT NULL, kind TEXT NOT NULL, file_path TEXT NOT NULL,
    start_line INTEGER NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,target_id)
);
CREATE TABLE IF NOT EXISTS issues(
    run_id TEXT NOT NULL, module_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    issue_id TEXT NOT NULL, file_path TEXT NOT NULL, line INTEGER NOT NULL,
    severity TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, search_text TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,issue_id)
);
CREATE TABLE IF NOT EXISTS fixes(
    run_id TEXT NOT NULL, module_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    fix_id TEXT NOT NULL, issue_id TEXT NOT NULL, action TEXT NOT NULL,
    file_path TEXT NOT NULL, start_line INTEGER NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,fix_id)
);
CREATE TABLE IF NOT EXISTS ai_records(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, module_id TEXT NOT NULL,
    log_call_id TEXT NOT NULL, status TEXT NOT NULL, runtime_id TEXT NOT NULL,
    runtime_version TEXT NOT NULL, duration_ms INTEGER NOT NULL, task TEXT NOT NULL,
    prompt_gzip BLOB NOT NULL, response_gzip BLOB NOT NULL, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parse_failures(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, module_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL, file_path TEXT NOT NULL, language TEXT NOT NULL,
    error_kind TEXT NOT NULL, message TEXT NOT NULL, worker_exit_code INTEGER,
    recoverable INTEGER NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_data(
    run_id TEXT NOT NULL, key TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,key)
);
CREATE INDEX IF NOT EXISTS idx_modules_status ON modules(run_id,status,path);
CREATE INDEX IF NOT EXISTS idx_issues_module ON issues(run_id,module_id);
CREATE INDEX IF NOT EXISTS idx_issues_filters ON issues(run_id,severity,action,file_path,line);
CREATE INDEX IF NOT EXISTS idx_issues_path ON issues(run_id,file_path,line);
CREATE INDEX IF NOT EXISTS idx_logs_module ON logs(run_id,module_id,file_path,line);
CREATE INDEX IF NOT EXISTS idx_targets_module ON analysis_targets(run_id,module_id,kind,file_path,start_line);
CREATE INDEX IF NOT EXISTS idx_failures_module ON parse_failures(run_id,module_id,file_path);
"""
