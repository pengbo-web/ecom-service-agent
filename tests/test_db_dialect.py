"""SQL 方言层:时钟表达式、唯一约束判定、RETURNING 主键。

这一层存在的意义是"换库时改动面可指认"。所以这份测试钉的不是某个片段长什么样,
而是**三条不变式**:片段只有一处产出、数值一律强制转换、驱动特有 API 不外泄。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from app.db import dialect
from app.db.database import Database


# ---------- 时钟表达式 ----------

def test_now_minus_renders_expected_fragment():
    assert dialect.now_minus(24, "hours") == "datetime('now', '-24 hours')"
    assert dialect.now_minus(7, "days") == "datetime('now', '-7 days')"


def test_numeric_coercion_is_structural_not_conventional():
    """数值强制转换必须在**这里**发生,而不是靠 24 个调用点各写一次 int()。

    这些片段是拼进 SQL 的(它们是结构不是参数,没法用占位符),所以"值必须是
    整数"是一条安全性质。散在调用点时它靠约定维持,漏一次就是一个注入点;
    收进来之后想漏也漏不掉。
    """
    assert dialect.now_minus("24", "hours") == "datetime('now', '-24 hours')"
    with pytest.raises(ValueError):
        dialect.now_minus("24 hours'); DROP TABLE orders;--", "hours")


def test_elapsed_hours_uses_the_database_clock():
    """滞留时长必须用**数据库自己的时钟**算。

    本项目 `Database._now()` 写的是本地时间,而 WHERE 里的 `datetime('now')`
    是 UTC。在 Python 侧算时间差会与"筛出这一行的那个条件"用两个不同的时钟——
    一条卡在阈值边缘的订单会出现"WHERE 认为已滞留 25h、打分认为 -7h"。
    """
    frag = dialect.elapsed_hours("o.created_at")
    assert "julianday('now')" in frag and "julianday(o.created_at)" in frag


def test_time_fragments_have_a_single_source():
    """全仓库不允许再出现手写的 `datetime('now'`(dialect.py 与注释除外)。

    此前它散在 24 个地方,`shop_analytics.py` 甚至自带一份与 `database.py`
    一模一样的实现。时钟表达式只要有第二处,换库时就一定会漏掉其中一份。
    """
    import ast
    import pathlib

    hits = []
    for path in pathlib.Path("app").rglob("*.py"):
        if path.name == "dialect.py":
            continue
        src = path.read_text(encoding="utf-8")
        if "datetime('now'" not in src:
            continue
        # 用 AST 而不是逐行文本判断:这些字样大量出现在**解释性 docstring**里
        # (说明为什么必须用数据库时钟),按行首字符过滤盖不住多行文档串的正文。
        # 只看真正会进 SQL 的字面量:普通字符串常量与 f-string 的字面片段。
        tree = ast.parse(src)
        docstrings = {id(d) for d in ast.walk(tree)
                      if isinstance(d, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                        ast.AsyncFunctionDef))
                      for d in [ast.get_docstring(d, clean=False)] if d}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "datetime('now'" in node.value and id(node.value) not in docstrings:
                    hits.append(f"{path}:{node.lineno}")
    assert not hits, f"这些地方绕过了方言层直接写时钟表达式: {hits}"


# ---------- 唯一约束判定 ----------

def test_is_duplicate_key_only_matches_unique_violations():
    """只包唯一约束冲突。外键/非空/CHECK 是真 bug,必须继续往上抛,
    不能被"去重生效"那条分支静默吃掉。"""
    assert dialect.is_duplicate_key(
        sqlite3.IntegrityError("UNIQUE constraint failed: reviews.order_id"))
    assert not dialect.is_duplicate_key(
        sqlite3.IntegrityError("NOT NULL constraint failed: orders.user"))
    assert not dialect.is_duplicate_key(
        sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
    assert not dialect.is_duplicate_key(RuntimeError("something else"))


def test_driver_specific_apis_do_not_leak(tmp_path):
    """`cur.lastrowid` 与 `sqlite3.IntegrityError` 不该再出现在业务代码里
    ——psycopg 两者都没有。"""
    import pathlib
    bad = []
    for p in pathlib.Path("app").rglob("*.py"):
        if p.name == "dialect.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if ".lastrowid" in line or "sqlite3.IntegrityError" in line:
                bad.append(f"{p}:{i}")
    assert not bad, f"驱动特有 API 外泄: {bad}"


# ---------- RETURNING ----------

def test_returning_id_appends_clause():
    assert dialect.returning_id("INSERT INTO t (a) VALUES (?)") == \
        "INSERT INTO t (a) VALUES (?) RETURNING id"
    assert dialect.returning_id("INSERT INTO t (a) VALUES (?);").endswith("RETURNING id")


def test_returning_id_works_end_to_end(tmp_path):
    """真库验证:RETURNING 取回的 id 与实际插入的行一致。"""
    d = Database(db_path=str(tmp_path / "ret.db"))
    d.init_schema()
    eid = d.publish_event("t.ev", {"k": 1}, "service", "analyst", "C1")
    assert isinstance(eid, int) and eid > 0
    assert d.list_events(correlation_id="C1")[0]["id"] == eid


def test_duplicate_insert_returns_none_not_raises(tmp_path):
    """唯一约束冲突走 `is_duplicate_key` → 返回 None,而不是把 sqlite3 异常
    冒泡给调用方。"""
    d = Database(db_path=str(tmp_path / "dup.db"))
    d.init_schema()
    assert isinstance(
        d.create_outreach_draft("unpaid_order", "u1", "O1", "x", {}, "", "C1", "growth"), int)
    assert d.create_outreach_draft(
        "unpaid_order", "u1", "O1", "y", {}, "", "C2", "growth") is None


def test_non_duplicate_integrity_error_still_raises(tmp_path, monkeypatch):
    """非唯一约束的完整性错误必须照抛——静默吞掉会把真 bug 变成"去重生效"。"""
    d = Database(db_path=str(tmp_path / "raise.db"))
    d.init_schema()
    real = d.connect

    class _Conn:
        def __init__(self, c):
            self._c = c

        def execute(self, *a, **k):
            raise sqlite3.IntegrityError("NOT NULL constraint failed: x.y")

        def __getattr__(self, n):
            return getattr(self._c, n)

    monkeypatch.setattr(d, "connect", lambda: _Conn(real()))
    with pytest.raises(sqlite3.IntegrityError):
        d.create_outreach_draft("unpaid_order", "u1", "O1", "x", {}, "", "C1", "growth")
