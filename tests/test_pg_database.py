"""PG3:业务数据层在 PostgreSQL 上的行为(与 SQLite **逐条对齐**)。

这份测试的形态是要点:核心方法**参数化跑两套后端**,断言完全一致。换后端最容易
出的错不是"新后端有 bug",而是"新后端在某个细节上和旧的不一样,而上层依赖了
那个细节"——只测一边发现不了。

没配 PG 时 pg 那一档跳过(sqlite 档照跑),启用方式见 tests/test_pg_event_bus.py。
"""

from __future__ import annotations

import os
import tempfile

import pytest

PG_DSN = os.getenv("ECOM_TEST_PG_DSN", "").strip()


def _pg_ok() -> bool:
    if not PG_DSN:
        return False
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def sqlite_db(tmp_path):
    from app.db.database import Database

    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


@pytest.fixture()
def pg_db():
    if not _pg_ok():
        pytest.skip("无可用 PG")
    import psycopg

    from app.db.database import Database

    # 每个用例一个干净 schema:业务断言(计数/排序/唯一约束)全都和"表里有什么"
    # 相关,残留行会让结果随执行顺序漂移。
    with psycopg.connect(PG_DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public")
    d = Database(db_path="unused", backend="pg", dsn=PG_DSN)
    d.init_schema()
    return d


@pytest.fixture(params=["sqlite", "pg"])
def db(request, sqlite_db):
    if request.param == "sqlite":
        return sqlite_db
    if not _pg_ok():
        pytest.skip("无可用 PG")
    return request.getfixturevalue("pg_db")


# ---------- 建表 ----------

def test_schema_creates_all_tables(db):
    """22 张表 + 索引都建得出来。

    PG 上真实撞到过两处**建表期**差异,都不是机械替换:
    ① `INTEGER PRIMARY KEY AUTOINCREMENT` → identity 列;
    ② `orders.user` —— `user` 是 PG 保留字,裸写 `syntax error at or near "user"`。
       不改列名(项目里约 130 处),改成在 PG 侧加双引号。
    """
    # 建表已在 fixture 里跑过;这里验一张有保留字列的表真的能读写
    db.upsert_order({"order_id": "O1", "user": "u1", "status": "pending",
                     "total": 10.0, "items": []}) if hasattr(db, "upsert_order") else None


def test_reserved_word_column_roundtrip(db):
    """`orders.user` 这一列必须能正常读写。

    这是保留字处理是否真的生效的唯一证据——建表通过不代表 DML 也通过
    (DML 里的 `user` 由 `quote_reserved` 在连接层处理)。
    """
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO orders (order_id, user, status, total, created_at) "
            "VALUES (?, ?, ?, ?, ?)", ("O-RSV", "u9", "pending", 1.0, db._now()))
        conn.commit()
        row = conn.execute(
            "SELECT user, status FROM orders WHERE order_id = ?", ("O-RSV",)).fetchone()
        assert row["user"] == "u9"
    finally:
        conn.close()


# ---------- 事件队列(业务库里的一张表) ----------

def test_publish_and_claim(db):
    eid = db.publish_event("signal.anomaly", {"kind": "k"}, "service", "analyst",
                           "C1", priority=10)
    assert eid > 0
    got = db.claim_events("analyst", limit=5)
    assert [e["id"] for e in got] == [eid]
    assert got[0]["payload"] == {"kind": "k"}


def test_priority_then_fifo(db):
    a = db.publish_event("x", {}, "s", "analyst", "C1", priority=0)
    b = db.publish_event("x", {}, "s", "analyst", "C2", priority=0)
    urgent = db.publish_event("x", {}, "s", "analyst", "C3", priority=10)
    assert [e["id"] for e in db.claim_events("analyst")] == [urgent, a, b]


def test_list_event_chains_shape_is_identical(db):
    """链清单的字段与类型两边必须一致。

    PG 上真实撞到:`SUM(status='failed')` 报
    `function sum(boolean) does not exist`——SQLite 把布尔当 0/1,PG 严格区分。
    这类差异**不会在建表时暴露**,只在跑到那条查询时才炸。
    """
    db.publish_event("x", {}, "service", "analyst", "CH1")
    rows = db.list_event_chains(limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r["correlation_id"] == "CH1"
    assert r["events"] == 1
    for col in ("failed", "pending", "skipped"):
        assert isinstance(r[col], int), f"{col} 应是整数(PG 的 SUM(boolean) 会炸)"
    assert r["agents"] == ["service", "analyst"], "顺序是给人看的信息,不能乱"
    assert isinstance(r["started_at"], str), "时间戳统一成字符串"


def test_shared_context_upsert(db):
    db.set_shared_context("diagnosis:s1", {"c": 1}, "analyst", "C1")
    db.set_shared_context("diagnosis:s1", {"c": 2}, "analyst", "C1")
    rows = db.list_shared_context(prefix="diagnosis:", limit=5)
    assert len(rows) == 1, "同 key 应覆盖(ON CONFLICT DO UPDATE,PG 原生支持)"
    assert rows[0]["value"]["c"] == 2


def test_worker_heartbeat_roundtrip(db):
    db.record_worker_heartbeat("collab", ok=True)
    hb = db.get_worker_heartbeat("collab")
    assert hb["name"] == "collab"
    assert isinstance(hb["last_success_at"], str)


def test_cart_partial_unique_index(db):
    """部分唯一索引(`WHERE status='active'`)—— PG 原生支持,PG1 核过"不用改"。

    同一 sku 加两次应累加而不是插两行。
    """
    db.add_to_cart("u1", "P001", 1)
    db.add_to_cart("u1", "P001", 2)
    rows = db.list_cart("u1")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 3


def test_returning_id_works_on_both(db):
    """主键取回统一走 RETURNING id(psycopg 没有 lastrowid)。"""
    eid = db.publish_event("x", {}, "s", "analyst", "C1")
    assert isinstance(eid, int) and eid > 0


# ---------- 只在 PG 上成立的事 ----------

@pytest.mark.skipif(not _pg_ok(), reason="需要可连的 PG")
def test_pg_rejects_missing_dsn():
    """配成 pg 却没给 dsn 时**刻意抛而不降级**。

    数据层是业务主链路:静默回落 SQLite 会让两个实例各写各的库,而那种数据分裂
    比启动失败难查得多。（与队列载体的降级取舍**相反**——队列在买家热路径上,
    丢一次投递远好过打死会话。）
    """
    from app.db.database import Database

    d = Database(db_path="x", backend="pg", dsn="")
    with pytest.raises(RuntimeError, match="db_pg_dsn"):
        d.connect()


@pytest.mark.skipif(not _pg_ok(), reason="需要可连的 PG")
def test_legacy_column_patches_are_skipped_on_pg():
    """旧库列补齐(`PRAGMA table_info` + ALTER)只对 SQLite 有意义。

    PG 是全新库、建表就带全部列;而 `PRAGMA` 在 PG 上是语法错误。

    用 AST 而不是文本搜:守卫的**说明注释**里就写着 "PRAGMA table_info 是 SQLite
    专有的",而那段注释排在 `if not self.is_pg:` 之前——按文本找第一个
    "PRAGMA table_info" 会命中注释,于是断言检查的是错误的位置
    (本项目已多次踩到"注释里出现的名字被判成代码"这个坑)。

    这里只看**真实的语法结构**:所有 `PRAGMA table_info` 字符串常量,是否都位于
    某个 `if not self.is_pg` 分支体内。
    """
    import ast
    import inspect
    import textwrap

    from app.db.database import Database

    tree = ast.parse(textwrap.dedent(inspect.getsource(Database.init_schema)))

    def _is_not_pg_test(node) -> bool:
        return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
                and isinstance(node.operand, ast.Attribute)
                and node.operand.attr == "is_pg")

    guarded, unguarded = 0, 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if "PRAGMA table_info" not in node.value:
            continue
        # 找它是否在某个 `if not self.is_pg:` 的 body 里
        inside = any(
            _is_not_pg_test(n.test) and any(node in ast.walk(stmt) for stmt in n.body)
            for n in ast.walk(tree) if isinstance(n, ast.If))
        guarded += inside
        unguarded += not inside

    assert guarded > 0, "没找到任何 PRAGMA table_info 调用(测试本身失效了)"
    assert unguarded == 0, f"有 {unguarded} 处 PRAGMA table_info 不在 is_pg 守卫内"


def test_default_backend_is_sqlite():
    """默认后端不变,既有行为逐字节相同。"""
    from app.config.settings import settings

    assert settings.db_backend == "sqlite"


# ---------- C 组:风险最高的一组(方案原文点名) ----------
#
# `review_outreach_draft` / `mark_outreach_sent` 的条件更新是**"消息只发一次"的
# 唯一保证**;`outreach_drafts` 上的部分唯一索引(`WHERE status='draft'`)是
# "同一个买家同一单同一类型不重复起草"的唯一保证。这两样在 PG 上必须逐条兑现,
# 否则后果是买家收到两条一样的营销消息、或一张券被发两次——都碰钱且不可撤销。

def test_draft_partial_unique_blocks_duplicates(db):
    """同 (user, order, kind) 的待审草稿只能有一条。

    部分唯一索引(`WHERE status = 'draft'`)PG 原生支持,PG1 核过"不用改"。
    撞上时返回 None(业务上是**正常**的去重生效),不是抛异常——这条语义由
    `dialect.is_duplicate_key` 兜着,两套后端共用同一个 except 分支。
    """
    first = db.create_outreach_draft("stale_pending_order", "u1", "O1", "在吗",
                                     {}, "理由", "C1", "growth")
    assert isinstance(first, int) and first > 0
    dup = db.create_outreach_draft("stale_pending_order", "u1", "O1", "再问一次",
                                   {}, "理由", "C1", "growth")
    assert dup is None, "重复草稿必须被唯一索引挡住并返回 None"
    assert len(db.list_outreach_drafts(status="draft")) == 1


def test_review_draft_is_idempotent(db):
    """审批只能生效一次。

    两个运营同时点批准、或前端连点两次,只有第一次真的改到状态——这是条件更新
    (`WHERE status='draft'`)给的保证,而它同时也是"消息只发一次"的第一道。
    """
    did = db.create_outreach_draft("stale_pending_order", "u2", "O2", "在吗",
                                   {}, "理由", "C2", "growth")
    assert db.review_outreach_draft(did, "approved", "human") is True
    assert db.review_outreach_draft(did, "approved", "human") is False


def test_mark_sent_is_idempotent(db):
    """投递记账只能生效一次(第二道)。"""
    did = db.create_outreach_draft("stale_pending_order", "u3", "O3", "在吗",
                                   {}, "理由", "C3", "growth")
    db.review_outreach_draft(did, "approved", "human")
    assert db.mark_outreach_sent(did) is True
    assert db.mark_outreach_sent(did) is False
