"""坐席队列（SQLite 持久化）。"""

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from app.config.settings import settings


class HandoffQueue:
    def __init__(self, db_path: Optional[str] = None, id_factory=None):
        self.db_path = db_path or settings.hitl_db_path
        self._id = id_factory or (lambda: uuid.uuid4().hex[:12])

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
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    intent TEXT,
                    reasons TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_handoff_status ON handoffs(status);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def add(self, bundle: dict) -> str:
        hid = self._id()
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO handoffs (handoff_id, session_id, intent, reasons, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (hid, bundle.get("session_id"), bundle.get("intent"),
                 json.dumps(bundle.get("reasons", []), ensure_ascii=False),
                 json.dumps(bundle, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        return hid

    def _row(self, r) -> dict:
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
        return d

    def list_pending(self) -> list:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM handoffs WHERE status='pending' ORDER BY created_at DESC"
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def get(self, handoff_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            r = conn.execute(
                "SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
            return self._row(r) if r else None
        finally:
            conn.close()

    def resolve(self, handoff_id: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE handoffs SET status='resolved', "
                "resolved_at=CURRENT_TIMESTAMP WHERE handoff_id=? AND status='pending'",
                (handoff_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def count_pending(self) -> int:
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM handoffs WHERE status='pending'"
            ).fetchone()["c"]
        finally:
            conn.close()
