"""Skill 会话、运行和确认记录的 SQLite 持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4


class SessionError(RuntimeError):
    """会话状态不允许当前操作。"""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    project_id: str
    host_id: Optional[str]


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    session_id: str
    project_id: str
    host_id: Optional[str]
    skill_id: str
    status: str


@dataclass(frozen=True)
class RunSnapshot:
    """供状态查询使用的一次运行快照。"""

    run_id: str
    session_id: str
    project_id: str
    host_id: Optional[str]
    skill_id: str
    status: str
    created_at: str
    updated_at: str
    result: Optional[Mapping[str, Any]] = None
    error_code: str = ""
    error_message: str = ""


class SessionStore:
    """保存 AI 会话，不把会话状态放入 Host Service。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._create_tables()

    def create_session(self, project_id: str, host_id: Optional[str] = None) -> SessionRecord:
        if not project_id.strip():
            raise ValueError("project_id 不能为空")
        session_id = uuid4().hex
        now = _now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, project_id, host_id, now),
            )
        return SessionRecord(session_id, project_id, host_id)

    def recover_incomplete_runs(self) -> int:
        """把上一次 Runtime 遗留的活动任务标记为失败。

        Runtime 进程异常退出时，数据库无法知道宿主调用最终是否完成。
        继续保留 ``queued`` 或 ``running`` 会让任务表长期显示假状态，
        也会阻塞同一会话提交新任务，因此新 Runtime 启动时统一结束这些记录。
        """

        now = _now()
        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    error_code = 'runtime_restarted',
                    error_message = ?,
                    updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                ("Runtime 重启后，未完成任务已终止", now),
            )
        return cursor.rowcount

    def get_session(self, session_id: str) -> SessionRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, project_id, host_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionError("会话不存在")
        return SessionRecord(row[0], row[1], row[2])

    def start_run(
        self,
        session_id: str,
        skill_id: str,
        args: Mapping[str, Any],
        approved: bool,
        host_id: Optional[str] = None,
    ) -> RunRecord:
        session = self.get_session(session_id)
        run_id = uuid4().hex
        now = _now()
        with self._write_lock, self._connection() as connection:
            active = connection.execute(
                "SELECT 1 FROM runs WHERE session_id = ? AND status IN ('queued', 'running')",
                (session_id,),
            ).fetchone()
            if active is not None:
                raise SessionError("当前会话已有活动的 Skill")
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, project_id, host_id, skill_id, status,
                    args_json, approved, result_json, error_code, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, '', '', '', ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    session.project_id,
                    host_id or session.host_id,
                    skill_id,
                    json.dumps(args, ensure_ascii=False, sort_keys=True),
                    int(approved),
                    now,
                    now,
                ),
            )
        return RunRecord(
            run_id,
            session_id,
            session.project_id,
            host_id or session.host_id,
            skill_id,
            "running",
        )

    def queue_run(
        self,
        session_id: str,
        skill_id: str,
        args: Mapping[str, Any],
        approved: bool,
        host_id: Optional[str] = None,
    ) -> RunRecord:
        """创建 queued 状态的运行，并拒绝同一会话的活动运行。"""

        session = self.get_session(session_id)
        run_id = uuid4().hex
        now = _now()
        with self._write_lock, self._connection() as connection:
            active = connection.execute(
                "SELECT 1 FROM runs WHERE session_id = ? AND status IN ('queued', 'running')",
                (session_id,),
            ).fetchone()
            if active is not None:
                raise SessionError("当前会话已有活动的 Skill")
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, project_id, host_id, skill_id, status,
                    args_json, approved, result_json, error_code, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, '', '', '', ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    session.project_id,
                    host_id or session.host_id,
                    skill_id,
                    json.dumps(args, ensure_ascii=False, sort_keys=True),
                    int(approved),
                    now,
                    now,
                ),
            )
        return RunRecord(
            run_id,
            session_id,
            session.project_id,
            host_id or session.host_id,
            skill_id,
            "queued",
        )

    def mark_running(self, run_id: str) -> bool:
        """把 queued 运行原子地切换为 running；已取消时返回 False。"""

        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (_now(), run_id),
            )
        return cursor.rowcount == 1

    def finish_run(
        self,
        run_id: str,
        status: str,
        result: Optional[Mapping[str, Any]] = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if status not in ("success", "failed", "timeout", "cancelled"):
            raise ValueError("运行状态必须是 success、failed、timeout 或 cancelled")
        with self._write_lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else "",
                    error_code,
                    error_message,
                    _now(),
                    run_id,
                ),
            )

    def get_run(self, run_id: str) -> RunSnapshot:
        """读取一次运行的持久化快照。"""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, session_id, project_id, host_id, skill_id, status, "
                "created_at, updated_at, result_json, error_code, error_message "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise SessionError("运行记录不存在")
        result = json.loads(row[8]) if row[8] else None
        if result is not None and not isinstance(result, dict):
            raise SessionError("运行结果格式无效")
        return RunSnapshot(
            run_id=row[0],
            session_id=row[1],
            project_id=row[2],
            host_id=row[3],
            skill_id=row[4],
            status=row[5],
            created_at=row[6],
            updated_at=row[7],
            result=result,
            error_code=row[9],
            error_message=row[10],
        )

    def cancel_queued(self, run_id: str) -> bool:
        """只取消尚未进入宿主调用的 queued 运行。"""

        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'cancelled', updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (_now(), run_id),
            )
        return cursor.rowcount == 1

    def add_message(self, session_id: str, run_id: str, role: str, content: str) -> None:
        self.get_session(session_id)
        if role not in ("user", "assistant", "system"):
            raise ValueError("消息角色无效")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO messages(session_id, run_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, run_id, role, content, _now()),
            )

    def _create_tables(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    host_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    host_id TEXT,
                    skill_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_session_status
                    ON runs(session_id, status);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_runs(connection)

    @staticmethod
    def _migrate_runs(connection: sqlite3.Connection) -> None:
        """给旧任务表补齐项目、宿主和创建时间字段。"""

        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "project_id" not in columns:
            connection.execute(
                "ALTER TABLE runs ADD COLUMN project_id TEXT NOT NULL DEFAULT ''"
            )
        if "host_id" not in columns:
            connection.execute("ALTER TABLE runs ADD COLUMN host_id TEXT")
        if "created_at" not in columns:
            connection.execute(
                "ALTER TABLE runs ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """
            UPDATE runs
            SET project_id = COALESCE(
                    NULLIF(project_id, ''),
                    (SELECT project_id FROM sessions WHERE sessions.id = runs.session_id),
                    ''
                ),
                host_id = COALESCE(
                    host_id,
                    (SELECT host_id FROM sessions WHERE sessions.id = runs.session_id)
                ),
                created_at = CASE
                    WHEN created_at = '' THEN updated_at
                    ELSE created_at
                END
            """
        )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
