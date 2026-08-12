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
                    meta TEXT,
                    parent_span_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at);
                """
            )
            conn.commit()
            # 迁移:老库(建表时还没有 parent_span_id 列)补列。CREATE TABLE
            # IF NOT EXISTS 对已存在的表不会补新列，SQLite 也没有
            # "ADD COLUMN IF NOT EXISTS"，只能靠捕获重复添加时的异常幂等。
            try:
                conn.execute("ALTER TABLE spans ADD COLUMN parent_span_id TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # 列已存在(新库已在 CREATE TABLE 里带上,或已迁移过)
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
                        success, prompt_tokens, completion_tokens, meta, parent_span_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s.span_id, s.trace_id, s.name, s.kind, s.started_at, s.ended_at,
                     s.latency_ms,
                     None if s.success is None else int(s.success),
                     s.prompt_tokens, s.completion_tokens,
                     json.dumps(s.meta, ensure_ascii=False),
                     getattr(s, "parent_span_id", None)),
                )
            conn.commit()
        finally:
            conn.close()

    def recent_traces(self, limit: int = 20, session_id: Optional[str] = None,
                      since: Optional[float] = None) -> list[dict]:
        """最近的 trace;给了 `since`(unix 秒)则只取该时刻之后开始的。

        `since` 是后加的,补的是**看板上卡片与表格口径不一致**这个缺陷(走查时实测):
        指标走 `/api/metrics?window_hours=N`,而「最近请求」表格走
        `/api/traces?limit=50`——**不带窗口**。切到「近 1 小时」时,卡片显示"总请求数 1"、
        页脚写"统计口径:近 1 小时",紧接着的表格仍然是 50 行、跨度 40.9 小时。

        危害不只是数字对不上:排查时选「近 1 小时」看现状,点表格里某一行看调用链,
        实际看到的是 40 小时前的事;或者卡片说"错误率 0%"而表格里有一条老的失败行,
        读者会以为指标算错了。窗口选择器在表格上方,「最近请求」理所当然应当遵守它。

        与 `all_traces(since=...)` 同一个参数形状和同一条理由(见那里的说明)。
        """
        conn = self.connect()
        try:
            where, params = [], []
            if session_id:
                where.append("session_id = ?")
                params.append(session_id)
            if since is not None:
                where.append("started_at >= ?")
                params.append(since)
            sql = "SELECT * FROM traces"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY started_at DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        finally:
            conn.close()

    def all_traces(self, since: Optional[float] = None) -> list[dict]:
        """全部 trace;给了 `since`(unix 秒)则只取该时刻之后开始的。

        时间窗是后加的:`compute_metrics` 原先聚合**全部历史**,于是一个已经修好
        的问题会永远留在看板上——修完之后新调用全成功,而累计值被几百条旧失败
        压着,红色要好几周才褪。运维看到的是"改了没用",实际是"口径不对"。
        """
        conn = self.connect()
        try:
            if since is None:
                rows = conn.execute("SELECT * FROM traces").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM traces WHERE started_at >= ?", (float(since),)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def all_spans(self, since: Optional[float] = None) -> list[dict]:
        """全部 span;`since` 同 `all_traces`。

        按 span 自己的 `started_at` 过滤,而不是先查窗内 trace 再按 trace_id 关联:
        后者要么发一条 `IN (几百个 id)`,要么两次查询在 Python 里 join,而两者
        都会随历史增长变慢——这个端点是看板每次刷新都要调的。
        """
        conn = self.connect()
        try:
            if since is None:
                rows = conn.execute("SELECT * FROM spans").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM spans WHERE started_at >= ?", (float(since),)
                ).fetchall()
            # 注意:这里**保持 meta 的原始字符串形态**,不走 `_decode_meta`——
            # `compute_metrics` 按字符串包含判护栏动作。见 `_decode_meta` 的说明。
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _decode_meta(row: dict) -> dict:
        """把 span 行里 JSON 字符串形式的 `meta` 解析成对象。

        库里 `meta` 存的是 TEXT(JSON 串)。`get_trace` 是**面向 API** 的读取口,
        直接透传会让前端拿到"JSON 里套一个 JSON 字符串",迫使每个消费方各自
        再 parse 一次——第一个忘记 parse 的地方就会静默拿不到字段
        (`span.meta?.error` 在字符串上恒为 undefined,不报错)。

        **只在这一个方法里解析,不动 `all_spans()`**:`compute_metrics` 用的是
        `all_spans()`,而它是按**字符串包含**判断护栏动作的
        (`'"action": "block"' in s["meta"]`)。在那边解析会当场改坏指标口径。
        两处口径不同是既有事实,这里把它写下来,而不是顺手"统一"掉。
        """
        item = dict(row)
        raw = item.get("meta")
        if isinstance(raw, str) and raw:
            try:
                item["meta"] = json.loads(raw)
            except (ValueError, TypeError):
                item["meta"] = None      # 存坏了就当没有,不把原始串塞给前端
        return item

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
            trace["spans"] = [self._decode_meta(s) for s in spans]
            return trace
        finally:
            conn.close()
