"""WS2:session_archive 的 FTS5 全文召回源(技术方案 §3)。

定位是门禁用例合成的**第三采样源**(`case_synthesis.SOURCE_FTS`,弱证据):
只决定**挑哪些真实会话**,不决定任何一条断言——"采样可以启发式,裁判尺不能"
(case_synthesis docstring)。相对关键词源(整串子串)的增量是**分词匹配**:
jieba 切词后 MATCH,能捞到"词被隔开但语义相连"的会话,且对声明关键词的写法
不敏感(关键词源受 frontmatter 自报质量影响)。

三态纪律(与 faq_cache 同源):**ok / unavailable 必须可区分**。索引构建失败
报 unavailable,调用方退回两源现状并把状态写进 stats——不许静默空,否则
"FTS 没捞到"与"FTS 坏了"在看板上长得一模一样,正是本项目反复修的那类病。

卖家侧硬隔离:`session_archive` 目前只有买家会话,卖家 skill 的关键词与买家
话题高度重叠(refund-attribution 实测教训),按全文捞会捞到买家提问——**一条
错的裁判尺比没有更糟**。`search_sessions_with_state(actor="seller")` 在入口
直接返回空 + 理由,纪律代码化而不是靠调用方记得。

索引形态对齐 memory_store:content=jieba 分词文本(参与 MATCH),
raw=原文(UNINDEXED,只作命中返回与人工核对)。索引文本 = 首条 user 消息 +
会话 summary——FTS 命中的就是这两段,所以 case_synthesis 的 FTS 路只取会话的
**首个 user 轮**(与"被索引的内容"严格对齐,不把整段会话铺开)。
"""

from __future__ import annotations

import json
import logging

from app.utils.zh_segment import SEGMENTER_VERSION, segment, segment_tokens

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_UNAVAILABLE = "unavailable"

_FTS_TABLE = "archive_fts_seg"
_META_TABLE = "archive_fts_meta"


def _index_text(messages, summary: str | None) -> str:
    """被索引的原文:首条 user 消息 + summary。两者之外不进索引。"""
    first_user = ""
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except Exception:  # noqa: BLE001 坏 messages 当空处理,索引侧不崩
            messages = []
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            first_user = str(msg.get("content") or "")
            break
    parts = [p for p in (first_user, summary or "") if p]
    return "\n".join(parts)


def _create_locked(conn) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "
        "USING fts5(session_id UNINDEXED, content, raw UNINDEXED)")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_META_TABLE} "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _stored_version(conn) -> str:
    row = conn.execute(
        f"SELECT value FROM {_META_TABLE} WHERE key = 'segmenter_version'").fetchone()
    return str(dict(row)["value"]) if row else ""


def rebuild_index(db) -> None:
    """全量重建(删表重来)。分词口径换版本、或索引与归档条数对不上时调用。"""
    conn = db.connect()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {_FTS_TABLE}")
        conn.execute(f"DELETE FROM {_META_TABLE} WHERE key = 'segmenter_version'")
        _create_locked(conn)
        rows = conn.execute(
            "SELECT session_id, messages, summary FROM session_archive").fetchall()
        for row in rows:
            item = dict(row)
            raw = _index_text(item.get("messages"), item.get("summary"))
            if not raw:
                continue
            conn.execute(
                f"INSERT INTO {_FTS_TABLE} (session_id, content, raw) VALUES (?, ?, ?)",
                (str(item["session_id"]), segment(raw), raw))
        conn.execute(
            f"INSERT OR REPLACE INTO {_META_TABLE} (key, value) "
            "VALUES ('segmenter_version', ?)", (SEGMENTER_VERSION,))
        conn.commit()
    finally:
        conn.close()


def ensure_index(db) -> str:
    """确保索引可用,返回 STATE_OK / STATE_UNAVAILABLE。

    版本戳不符 → 全量重建(分词口径变了,旧索引是另一种切法的产品);
    任何异常 → unavailable,**不半建半留**(半张索引比没有更危险:捞到的
    会话看起来像全量检索的结果)。
    """
    try:
        conn = db.connect()
        try:
            _create_locked(conn)
            conn.commit()
            stale = _stored_version(conn) != SEGMENTER_VERSION
        finally:
            conn.close()
        if stale:
            rebuild_index(db)
        return STATE_OK
    except Exception as exc:  # noqa: BLE001 索引是增强,坏了退回两源现状
        logger.warning("archive_fts 索引不可用,退回两源采样:%s", exc)
        return STATE_UNAVAILABLE


def index_session(db, session_id: str, messages, summary: str | None) -> bool:
    """增量索引一条归档(归档写入旁路调用)。fail-soft:失败返回 False,
    下次 ensure_index 的版本/条数校验会发现并重建。"""
    raw = _index_text(messages, summary)
    if not raw:
        return False
    try:
        conn = db.connect()
        try:
            if _stored_version(conn) != SEGMENTER_VERSION:
                return False          # 索引待重建,增量写入没有意义
            conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE session_id = ?",
                         (session_id,))
            conn.execute(
                f"INSERT INTO {_FTS_TABLE} (session_id, content, raw) VALUES (?, ?, ?)",
                (session_id, segment(raw), raw))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 旁路索引绝不影响归档主流程
        logger.warning("archive_fts 增量索引失败(session=%s):%s", session_id, exc)
        return False


def search_sessions_with_state(db, query: str, top_k: int = 20,
                               actor: str = "buyer") -> tuple[list[str], str, str]:
    """`(session_ids, state, reason)`。

    actor != buyer 直接返回空 + 理由:归档语料只有买家会话,卖家 skill 按全文
    捞会捞到买家提问,据此合成的用例是**错的**而不只是弱的(关键词路同款教训)。
    """
    if actor != "buyer":
        return [], STATE_OK, ("session_archive 目前只有买家会话,"
                              f"actor={actor} 不使用全文召回源")
    if not query or not query.strip():
        return [], STATE_OK, ""
    state = ensure_index(db)
    if state != STATE_OK:
        return [], state, "FTS 索引不可用,本源跳过"
    tokens = segment_tokens(query)
    if not tokens:
        return [], STATE_OK, ""
    match = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)
    try:
        conn = db.connect()
        try:
            rows = conn.execute(
                f"SELECT session_id, bm25({_FTS_TABLE}) AS score "
                f"FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ? "
                "ORDER BY score LIMIT ?", (match, top_k)).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 MATCH 语法/索引异常 → unavailable
        logger.warning("archive_fts 查询失败:%s", exc)
        return [], STATE_UNAVAILABLE, f"FTS 查询异常: {type(exc).__name__}"
    seen: list[str] = []
    for row in rows:
        sid = str(dict(row)["session_id"])
        if sid not in seen:
            seen.append(sid)
    # 子串计数保底(与 memory_store.search 同一条纪律:"不会比改造前少召回任何
    # 一条")。jieba 两侧词表不对称是实测到的:索引侧 cut_for_search 在长句里把
    # 未登录词「挤脚」切成 挤/脚,查询侧对同一个词却返回整词——带引号的 MATCH
    # 于是恒空。MATCH 命中在前,子串命中补后,按 content 去重。
    if len(seen) < top_k:
        terms = [t for t in tokens if len(t) >= 2]
        if terms:
            try:
                conn = db.connect()
                try:
                    fallback = conn.execute(
                        f"SELECT session_id, raw FROM {_FTS_TABLE}").fetchall()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 保底也炸了就认:返回 MATCH 那部分
                fallback = []
            scored: list[tuple[int, str]] = []
            for row in fallback:
                item = dict(row)
                raw = str(item.get("raw") or "")
                hits = sum(raw.count(t) for t in terms)
                if hits:
                    scored.append((hits, str(item["session_id"])))
            for _hits, sid in sorted(scored, key=lambda x: -x[0]):
                if sid not in seen:
                    seen.append(sid)
                if len(seen) >= top_k:
                    break
    return seen, STATE_OK, ""


def search_sessions(db, query: str, top_k: int = 20, actor: str = "buyer") -> list[str]:
    """便捷口:只要 id。需要 state/reason 做披露时用 `search_sessions_with_state`。"""
    ids, _state, _reason = search_sessions_with_state(db, query, top_k, actor)
    return ids
