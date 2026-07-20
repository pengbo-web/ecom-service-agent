"""Trace 持久化（独立 SQLite）。"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.observability.trace import Trace


class TraceStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.trace_db_path

    def connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    user_input TEXT,
                    intent TEXT,
                    started_at REAL,
                    ended_at REAL,
                    latency_ms REAL,
                    status TEXT,
                    error TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT,
                    name TEXT,
                    kind TEXT,
                    started_at REAL,
                    ended_at REAL,
                    latency_ms REAL,
                    success INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    meta TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_trace(self, trace: Trace) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO traces
                   (trace_id, session_id, user_input, intent, started_at, ended_at,
                    latency_ms, status, error, prompt_tokens, completion_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trace.trace_id, trace.session_id, trace.user_input, trace.intent,
                 trace.started_at, trace.ended_at, trace.latency_ms, trace.status,
                 trace.error, trace.prompt_tokens, trace.completion_tokens),
            )
            for s in trace.spans:
                conn.execute(
                    """INSERT OR REPLACE INTO spans
                       (span_id, trace_id, name, kind, started_at, ended_at, latency_ms,
                        success, prompt_tokens, completion_tokens, meta)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s.span_id, s.trace_id, s.name, s.kind, s.started_at, s.ended_at,
                     s.latency_ms,
                     None if s.success is None else int(s.success),
                     s.prompt_tokens, s.completion_tokens,
                     json.dumps(s.meta, ensure_ascii=False)),
                )
            conn.commit()
        finally:
            conn.close()

    def recent_traces(self, limit: int = 20, session_id: Optional[str] = None) -> list[dict]:
        conn = self.connect()
        try:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM traces WHERE session_id = ? "
                    "ORDER BY started_at DESC LIMIT ?", (session_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM traces ORDER BY started_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def all_traces(self) -> list[dict]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM traces").fetchall()]
        finally:
            conn.close()

    def all_spans(self) -> list[dict]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM spans").fetchall()]
        finally:
            conn.close()

    def get_trace(self, trace_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if not row:
                return None
            trace = dict(row)
            spans = conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at", (trace_id,)
            ).fetchall()
            trace["spans"] = [dict(s) for s in spans]
            return trace
        finally:
            conn.close()
