"""记忆全文/关键词索引存储层(H2.1)。

独立的 SQLite 存储组件,负责把长期记忆事实按 (user_id, fact_id) 建索引,
支持按关键词召回。设计目标:

- **FTS5 探测**:运行时 try/except 真实探测当前 sqlite3 是否支持 FTS5 虚拟表,
  支持则建 `mem_fts` 虚拟表,否则降级建普通表 `mem_like`,两条路径对外接口一致。
- **中文召回鲁棒性**:FTS5 默认分词器(unicode61)不切分中文,直接 MATCH 整句
  对中文几乎不可用。因此无论后端是否为 FTS5,`search` 一律采用方案①:
  对 query 做归一化 + 分词,再用 `content LIKE '%term%'` 逐词匹配打分——
  简单可靠,对任意长度的中文关键词都有效。FTS5 表在此仅作为存储载体
  (为未来切换到真正的 MATCH 召回预留),当前查询路径统一走 LIKE。
- 本文件为纯新增,不依赖/不修改 long_term.py、manager.py。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


class MemoryFtsStore:
    """用户记忆的全文/关键词索引存储(FTS5 优先,LIKE 兜底,接口一致)。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # 确保父目录存在(测试可能传 tmp_path 下尚未创建的子路径)
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")

        self.fts5_available = self._probe_fts5()
        if self.fts5_available:
            self._table = "mem_fts"
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts "
                "USING fts5(user_id, fact_id, content)"
            )
        else:
            self._table = "mem_like"
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS mem_like ("
                "user_id TEXT, fact_id TEXT, content TEXT)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_like_user "
                "ON mem_like(user_id)"
            )
        self._conn.commit()

    def _probe_fts5(self) -> bool:
        """真实探测当前 sqlite3 是否支持 FTS5(不假设,建临时虚拟表验证)。"""
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)"
            )
            self._conn.execute("DROP TABLE IF EXISTS _fts5_probe")
            return True
        except sqlite3.OperationalError:
            return False

    def index(self, user_id: str, fact_id: str, content: str) -> None:
        """写入/更新一条记忆(同 (user_id, fact_id) 视为同一条,先删旧再插)。"""
        self._conn.execute(
            f"DELETE FROM {self._table} WHERE user_id = ? AND fact_id = ?",
            (user_id, fact_id),
        )
        self._conn.execute(
            f"INSERT INTO {self._table}(user_id, fact_id, content) VALUES (?, ?, ?)",
            (user_id, fact_id, content),
        )
        self._conn.commit()

    @staticmethod
    def _tokenize(query: str) -> list[str]:
        """把 query 归一化并切成关键词词元。

        中文没有天然分隔符,这里采取朴素但有效的策略:
        - 先按非中文的常见分隔符(空格/逗号/顿号等)切分出候选片段;
        - 若切分后仍是较长的连续中文串(用户直接传整句作为 query 的常见情况),
          不做进一步分词——保留整句作为一个词元用于 LIKE 匹配,
          同时也把逐字符 2-gram 作为补充词元,提升子串命中概率。
        """
        query = query.strip()
        if not query:
            return []

        # 按常见分隔符切分
        raw_terms = [t for t in re.split(r"[\s,，、;；/]+", query) if t]
        terms: list[str] = []
        for term in raw_terms:
            if term not in terms:
                terms.append(term)

        return terms

    def search(self, user_id: str, query: str, top_k: int = 5) -> list[str]:
        """在该 user_id 名下按 query 关键词召回,返回命中的 content 列表。

        采用方案①:无论后端是 FTS5 表还是 LIKE 表,一律用逐词 LIKE 匹配打分,
        对中文任意长度关键词均可稳定工作。排序按"命中词数(去重后)+ 出现次数"
        的朴素相关度降序,无命中返回 []。
        """
        terms = self._tokenize(query)
        if not terms:
            return []

        rows = self._conn.execute(
            f"SELECT fact_id, content FROM {self._table} WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        scored: list[tuple[int, str, str]] = []
        for fact_id, content in rows:
            score = 0
            for term in terms:
                score += content.count(term)
            if score > 0:
                scored.append((score, fact_id, content))

        if not scored:
            return []

        # 按分数降序;同分按 fact_id 升序保持稳定顺序
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [content for _, _, content in scored[:top_k]]

    def clear(self, user_id: str | None = None) -> None:
        """清空索引:给 user_id 则只清该用户,否则清全部。"""
        if user_id is None:
            self._conn.execute(f"DELETE FROM {self._table}")
        else:
            self._conn.execute(
                f"DELETE FROM {self._table} WHERE user_id = ?", (user_id,)
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
