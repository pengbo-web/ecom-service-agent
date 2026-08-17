"""真实数据层：SQLite 连接、建表与读写。"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.db import dialect

logger = logging.getLogger(__name__)


def _traffic_source() -> str:
    """当前这一轮流量的来源(见 `app/agent/runtime_context.py` 里的长注释)。

    取不到时按 `"unknown"` —— **不假装是真实流量**。写库这一侧宁可少认一条 live,
    也不能凭空给一条来路不明的记录盖上"真实买家"的章:看门狗会拿这个章去做
    自动回滚。

    延迟导入:`app.db` 在依赖链底层,顶层 import agent 层会成环。
    """
    try:
        from app.agent.runtime_context import get_traffic_source
        return get_traffic_source()
    except Exception:  # noqa: BLE001
        return "unknown"


class Database:
    """业务数据层。**一份实现,两套后端**(PG3)。

    为什么不写一个平行的 `PostgresDatabase` 类:那意味着把 96 个方法抄第二遍,
    而两份实现必然漂移——本项目已经反复吃过"一半组件做对、另一半漏了"的亏
    (会话锁降级、`list_user_orders` 的 success 判定、conftest 漏钉 `mcp_enabled`、
    ApeRAG 漏了代理绕行)。改成参数化之后,SQL 只有一处,方言差异收敛在
    `app/db/dialect.py`,驱动差异收敛在 `app/db/pg_conn.py`。

    `backend` 默认取 `settings.db_backend`(默认 `sqlite`),行为与改造前**逐字节
    相同**;显式传 `backend="pg"` 才走 PG。两套后端能在同一进程共存——双后端
    参数化测试是唯一能证明"语义一致"的手段,而它必须同时持有两个实例。
    """

    def __init__(self, db_path: Optional[str] = None,
                 backend: Optional[str] = None, dsn: Optional[str] = None):
        self.db_path = db_path or settings.db_path
        self.backend = (backend or getattr(settings, "db_backend", "sqlite") or "sqlite").lower()
        self.dsn = dsn or getattr(settings, "db_pg_dsn", "") or ""
        #: 方言实例(不是模块级全局状态,见 dialect.py 顶部的说明)
        self.d = dialect.get_dialect(self.backend)

    @property
    def is_pg(self) -> bool:
        return self.backend == "pg"

    def connect(self):
        """取一条连接。**并发基座在这里**:WAL + busy_timeout。

        为什么必须开 WAL:默认的 rollback journal 下写者与读者互斥,而协作
        worker 每处理一条事件要写好几次(共享上下文 / 发事件 / 落草稿 /
        结束事件)。一旦起草并行(见 collab.handle_insight)或起第二个 worker
        进程,第二个写者立刻拿到 `database is locked` 而不是排队——表现为
        "一并行就报错"。WAL 让读者不阻塞写者、写者不阻塞读者,写者之间仍
        串行但走排队。

        busy_timeout 是配套的另一半:WAL 只是让写者可以排队,**愿不愿意等**
        由它决定。默认值下遇到锁会很快放弃;显式给一个上限(可配),让短暂的
        写冲突自己消化掉,而不是冒泡成一次业务失败。

        PRAGMA 放在 connect() 而不是 init_schema():journal_mode 虽然是数据库
        文件的持久属性(设一次即可,已是 WAL 时这句是空操作),但 busy_timeout
        是**连接级**的,每条新连接都得重设;而且放这里能保证任何拿连接的路径
        都覆盖到,不会出现"某条路径绕过了初始化"。两句 PRAGMA 的开销可忽略。

        fail-soft:`:memory:` 内存库不支持 WAL(执行会失败),某些只读挂载也
        可能拒绝改 journal_mode。这两种情况下退回默认 journal 继续跑——并发
        能力受限,但不能因为设不了一个性能 PRAGMA 就让整个数据层不可用。
        """
        if self.is_pg:
            # PG 不需要 WAL/busy_timeout(那是 SQLite 的并发基座概念),也没有文件路径。
            # 连接包装把 `?` 翻成 `%s`(见 app/db/pg_conn.py:PG1 说的"在 execute
            # 边界做一次转换"就是那里)。
            from app.db.pg_conn import PgConnection

            if not self.dsn:
                raise RuntimeError(
                    "db_backend=pg 但未配置 db_pg_dsn。数据层是业务主链路,"
                    "这里**刻意抛而不降级**:静默回落 SQLite 会让两个实例各写各的库,"
                    "而那种数据分裂比启动失败难查得多。")
            return PgConnection(self.dsn)

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                f"PRAGMA busy_timeout={max(0, int(settings.db_busy_timeout_ms))}")
        except sqlite3.Error:
            logger.debug("设置 WAL/busy_timeout 失败(可能是内存库或只读挂载),"
                         "按默认 journal 继续", exc_info=True)
        return conn

    def init_schema(self) -> None:
        """建表。DDL 只写一份(SQLite 语法),PG 由 `dialect.translate_schema` 后处理。

        为什么不把 22 张表逐个改成 f-string:那要在一段 300 行、结构高度重复的
        DDL 里插 30 多处 `{...}`,可读性会塌掉,而且每次加表都得记着用 f-string
        ——漏一次就是一个**只在 PG 上炸**的错误。后处理是一处,规则可逐条测。
        """
        conn = self.connect()
        try:
            conn.executescript(dialect.translate_schema(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    stock INTEGER,
                    description TEXT,
                    specs TEXT,
                    floor_price REAL
                );
                -- 下单幂等键。`UNIQUE(user, key)` 是幂等的**全部机制**:并发下靠数据库
                -- 唯一约束定胜负,不靠"先查再插"(那正是本仓库反复修过的先读后写竞态)。
                --
                -- `order_id` 允许为 NULL:抢到键的请求先插一行占位,建完单再回填。
                -- 于是 NULL 表示"同一个键的请求正在处理中",与"已完成、这是结果"
                -- 可区分——前者要告诉客户端稍后重试,后者直接返回原订单。
                CREATE TABLE IF NOT EXISTS order_idempotency (
                    user TEXT NOT NULL,
                    key TEXT NOT NULL,
                    order_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(user, key)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user TEXT,
                    status TEXT,
                    total REAL,
                    created_at TEXT,
                    shipped_at TEXT,
                    tracking_number TEXT,
                    carrier TEXT,
                    estimated_delivery TEXT,
                    delivered_at TEXT,
                    shipping_address TEXT,
                    refund_reason TEXT,
                    refund_status TEXT,
                    refund_requested_at TEXT
                );
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    name TEXT,
                    sku TEXT,
                    quantity INTEGER,
                    price REAL
                );
                CREATE TABLE IF NOT EXISTS shipments (
                    tracking_number TEXT PRIMARY KEY,
                    carrier TEXT,
                    status TEXT
                );
                CREATE TABLE IF NOT EXISTS logistics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracking_number TEXT NOT NULL,
                    seq INTEGER,
                    time TEXT,
                    location TEXT,
                    description TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    member_level TEXT DEFAULT 'normal'
                );
                CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
                CREATE INDEX IF NOT EXISTS idx_events_tn ON logistics_events(tracking_number);
                CREATE TABLE IF NOT EXISTS session_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    user_id TEXT,
                    messages TEXT,
                    summary TEXT,
                    msg_count INTEGER,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bargain_sessions (
                    session_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    rounds INTEGER DEFAULT 0,
                    last_offer REAL,
                    updated_at TEXT,
                    PRIMARY KEY (session_id, product_id)
                );
                -- 议价成交记录。**与 `bargain_sessions` 是两回事**:那张表存的是
                -- 谈判过程(轮次、上一轮报价),按 session 键;这张表存的是**结果**
                -- ——"这个买家可以按这个价买这件商品"。
                --
                -- 按 `user + sku` 而不是 session:兑现发生在 `POST /api/order`,
                -- 那个请求里**没有 session_id**(买家是在商城点「立即购买」,
                -- 不是在对话里下单)。按 session 存等于存了个兑不了的凭证。
                --
                -- `consumed_at`/`order_id` 为 NULL 表示还没用掉。一次性:一笔成交只
                -- 兑一单,否则买家谈一次就能按底价无限买。
                CREATE TABLE IF NOT EXISTS bargain_deals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    order_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_bargain_deals_lookup
                    ON bargain_deals (user, sku, consumed_at);
                CREATE TABLE IF NOT EXISTS session_snapshots (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    messages TEXT,
                    summary TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, status);
                CREATE TABLE IF NOT EXISTS skill_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT,
                    skill_name TEXT NOT NULL,
                    tool_calls TEXT,
                    outcome TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_skill_traces_name
                    ON skill_traces(skill_name, id);
                CREATE TABLE IF NOT EXISTS skill_canaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name TEXT NOT NULL,
                    candidate_path TEXT NOT NULL,
                    percent INTEGER NOT NULL,
                    risk TEXT,
                    policy TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_skill_canaries_active
                    ON skill_canaries(skill_name, status);
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload TEXT,
                    source_agent TEXT,
                    target_agent TEXT NOT NULL,
                    correlation_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_target
                    ON agent_events(target_agent, status, priority, id);
                CREATE INDEX IF NOT EXISTS idx_agent_events_corr
                    ON agent_events(correlation_id, id);
                CREATE TABLE IF NOT EXISTS worker_heartbeats (
                    name TEXT PRIMARY KEY,
                    last_success_at TEXT,
                    last_error_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_context (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    source_agent TEXT,
                    correlation_id TEXT,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outreach_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_type TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    order_id TEXT,
                    content TEXT NOT NULL,
                    offer TEXT,
                    reason TEXT,
                    correlation_id TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    needs_review_reason TEXT,
                    created_by TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    sent_at TEXT,
                    status_at_send TEXT,
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    outcome_checked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outreach_status
                    ON outreach_drafts(status, id);
                CREATE TABLE IF NOT EXISTS shop_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    shop_name TEXT,
                    tone TEXT,
                    banned_words TEXT,
                    updated_by TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS turn_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT,
                    intent TEXT,
                    emotion TEXT NOT NULL DEFAULT 'neutral',
                    emotion_level INTEGER NOT NULL DEFAULT 0,
                    requires_human INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turn_signals_created
                    ON turn_signals(created_at);
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(order_id, sku)
                );
                CREATE INDEX IF NOT EXISTS idx_reviews_sku ON reviews(sku, created_at);
                CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at);
                CREATE TABLE IF NOT EXISTS carts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    added_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_carts_active_unique
                    ON carts(user_id, sku) WHERE status = 'active';
                CREATE TABLE IF NOT EXISTS coupon_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    draft_id INTEGER,
                    reason TEXT,
                    granted_by TEXT,
                    created_at TEXT NOT NULL,
                    used_at TEXT,
                    UNIQUE(code, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_coupon_grants_user ON coupon_grants(user_id);
                CREATE TABLE IF NOT EXISTS outreach_followups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    correlation_id TEXT,
                    step INTEGER NOT NULL DEFAULT 1,
                    max_steps INTEGER NOT NULL DEFAULT 3,
                    next_touch_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_active_unique
                    ON outreach_followups(user_id, kind) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_followups_due
                    ON outreach_followups(status, next_touch_at);
                """, self.d))
            # ---- 旧库列补齐(**仅 SQLite**)----
            # 这些 ALTER 是为了让**已经存在**的老 SQLite 文件补上后来新增的列。
            # PG 后端是全新库,建表语句里就带着全部列,这一整段没有意义;而且
            # `PRAGMA table_info` 是 SQLite 专有的,在 PG 上会直接语法错误。
            if not self.is_pg:
                # 兼容旧库：products 补 floor_price 列
                cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
                if "floor_price" not in cols:
                    conn.execute("ALTER TABLE products ADD COLUMN floor_price REAL")
                # 兼容旧库：orders 补 shipping_address 列
                ocols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
                if "shipping_address" not in ocols:
                    conn.execute("ALTER TABLE orders ADD COLUMN shipping_address TEXT")
                # 兼容旧库：users 补 member_level 列(券资格:会员等级)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
                if "member_level" not in cols:
                    conn.execute("ALTER TABLE users ADD COLUMN member_level TEXT DEFAULT 'normal'")
                    conn.commit()
                # 兼容旧库：conversations 补 updated_at 列(最后活跃时间,供工作台排序/显示)
                ccols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
                if "updated_at" not in ccols:
                    conn.execute("ALTER TABLE conversations ADD COLUMN updated_at TEXT")
                    conn.execute("UPDATE conversations SET updated_at = created_at WHERE updated_at IS NULL")
                    conn.commit()
                # 兼容旧库：skill_traces 补 variant 列(灰度 A/B 需区分 live/canary)
                stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)").fetchall()}
                if "variant" not in stcols:
                    conn.execute("ALTER TABLE skill_traces ADD COLUMN variant TEXT DEFAULT 'live'")
                    conn.execute("UPDATE skill_traces SET variant = 'live' WHERE variant IS NULL")
                    conn.commit()
                # 兼容旧库：skill_traces 补 skill_version 列(0=未知,历史行无从考证)
                stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)")}
                if "skill_version" not in stcols:
                    conn.execute("ALTER TABLE skill_traces ADD COLUMN skill_version INTEGER DEFAULT 0")
                    conn.execute("UPDATE skill_traces SET skill_version = 0 WHERE skill_version IS NULL")
                    conn.commit()
                # 兼容旧库：skill_traces 补 skill_fingerprint 列("unknown"=未知,历史行
                # 无从考证)。与 skill_version 并存而非取代它:整数版本号只在正式(live)
                # 目录转正/回滚时才递增,候选目录从不带 .version,灰度期读到的版本号
                # 因此永远是"文件不存在→1"——两批不同候选的轨迹无法靠版本号区分。
                # 指纹是被服务那棵树的内容哈希,不依赖任何人在候选创建时打标,天然
                # 覆盖 live/candidate 两种情况。
                stcols = {r[1] for r in conn.execute("PRAGMA table_info(skill_traces)")}
                if "skill_fingerprint" not in stcols:
                    conn.execute(
                        "ALTER TABLE skill_traces ADD COLUMN skill_fingerprint TEXT DEFAULT 'unknown'")
                    conn.execute(
                        "UPDATE skill_traces SET skill_fingerprint = 'unknown' "
                        "WHERE skill_fingerprint IS NULL")
                    conn.commit()
                # 兼容旧库:skill_traces / session_archive 补 source 列(流量来源)。
                #
                # **历史行一律 'unknown',不是 'live'。** 这是本次迁移里唯一要紧的
                # 一个决定:字段加上之前那 976 轮里,真实买家只有 62 轮,其余是
                # u1 / 压测 / 评测 / 人工走查,而当时**没有任何东西能把它们分开**。
                # 把它们补成 'live' 等于凭空断言"这些都是真实流量",而看门狗会拿
                # 这个断言去做自动回滚 —— 那正是加这个字段要防的事。
                # 判不出就写判不出;要给历史行分类,走 app/scripts/backfill_traffic_source.py
                # (显式规则表 + 先看后写)。
                for table in ("skill_traces", "session_archive"):
                    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if "source" not in cols:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN source TEXT DEFAULT 'unknown'")
                        conn.execute(
                            f"UPDATE {table} SET source = 'unknown' WHERE source IS NULL")
                        conn.commit()
                # 兼容旧库:outreach_drafts 补触达归因三列(N3:发送基线/判定结果/判定
                # 时间)。outcome 老行补 'pending'——它们从未被判定过,不能默认成
                # 任何一个具体结论,而 pending_attribution 只挑 outcome='pending' 的
                # 行,老行因此天然会被下一轮 worker 捞到重新走一遍判定。
                odcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(outreach_drafts)").fetchall()}
                if "status_at_send" not in odcols:
                    conn.execute("ALTER TABLE outreach_drafts ADD COLUMN status_at_send TEXT")
                if "outcome" not in odcols:
                    conn.execute(
                        "ALTER TABLE outreach_drafts ADD COLUMN outcome TEXT DEFAULT 'pending'")
                    conn.execute(
                        "UPDATE outreach_drafts SET outcome = 'pending' WHERE outcome IS NULL")
                if "outcome_checked_at" not in odcols:
                    conn.execute("ALTER TABLE outreach_drafts ADD COLUMN outcome_checked_at TEXT")
                conn.commit()

                # P2:待审草稿的去重下沉到数据层(并行起草的前提)。
                #
                # 在此之前,"同一买家同一订单不重复排队"完全靠 collab.handle_insight
                # 里的应用层 seen 集合 + 串行循环。串行时对;并行后两条诊断各自读到
                # 同一份快照,同一个买家会被排两条几乎相同的草稿——店主挨个批完就是
                # 给同一个人连发两条。本项目在 start_followup 的注释里已经把这条纪律
                # 写死过:并发下唯一可靠的判重方式是让约束顶上去,而不是先查后插。
                #
                # 约束只覆盖 status='draft'(等人看的那些),与 pending_outreach_targets
                # 的既有口径完全一致,不新造第二套语义:approved/sent 是"已经处理过的
                # 历史",不该永久封杀对同一订单的再次触达;rejected 同理,店主驳回过的
                # 内容换个说法重新排队是合理的。
                #
                # 建索引前必须先清存量重复:此前没有任何约束拦过它们,直接建唯一索引
                # 会在这里抛 IntegrityError,导致**服务起不来**。清理保留 id 最大的
                # 那条(最新的内容最贴近当前情境),其余置 rejected 而**不物理删除**
                # ——审计要能看到"这条曾经存在过、因为什么被收掉"。
                # 兼容旧库:agent_events 补 priority 列(默认 0 = 普通优先级)。
                # 历史行补 0 是对的:它们发生时系统里没有优先级概念,追认任何非零
                # 值都是编造。
                aecols = {r[1] for r in conn.execute("PRAGMA table_info(agent_events)")}
                if "priority" not in aecols:
                    conn.execute(
                        "ALTER TABLE agent_events ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
                    conn.execute("UPDATE agent_events SET priority = 0 WHERE priority IS NULL")
                    conn.commit()

            dup_keys = conn.execute(
                "SELECT user_id, order_id, opportunity_type FROM outreach_drafts "
                "WHERE status = 'draft' "
                "GROUP BY user_id, order_id, opportunity_type HAVING COUNT(*) > 1"
            ).fetchall()
            for k in dup_keys:
                conn.execute(
                    "UPDATE outreach_drafts SET status = 'rejected', "
                    "reviewed_by = 'system', reviewed_at = ?, "
                    "needs_review_reason = COALESCE(needs_review_reason || ' / ', '') || "
                    "'历史重复草稿,建唯一约束时自动收敛(保留最新一条)' "
                    "WHERE status = 'draft' AND user_id = ? AND order_id = ? "
                    "AND opportunity_type = ? AND id < ("
                    "  SELECT MAX(id) FROM outreach_drafts WHERE status = 'draft' "
                    "  AND user_id = ? AND order_id = ? AND opportunity_type = ?)",
                    (self._now(), k["user_id"], k["order_id"], k["opportunity_type"],
                     k["user_id"], k["order_id"], k["opportunity_type"]))
            if dup_keys:
                logger.warning("建唯一约束前收敛了 %s 组重复待审草稿(已置 rejected,"
                               "未删除)", len(dup_keys))
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_draft_unique "
                "ON outreach_drafts(user_id, order_id, opportunity_type) "
                "WHERE status = 'draft'")

            # ---------- 店主通知(参谋异常诊断主动推送)----------
            # 协作 Worker 的 handle_signal() 发现异常后写入,前端轮询读取。
            # 与 outreach_drafts 不同:通知是给人看的提醒,不是待审动作,
            # 没有审批流程,只有已读/未读状态。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seller_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'info',
                    read INTEGER NOT NULL DEFAULT 0,
                    suggested_question TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seller_notif_read "
                "ON seller_notifications(read, created_at DESC)")
            # 商品级推广静默。由总线的 guard 分支在收到 signal.anomaly 时写入
            # (见 collab.handle_guard),投递侧仲裁读取。
            #
            # 为什么要落表而不是在闸里现算"最近有没有该商品的异常":静默是一个
            # **对外动作被拦掉**的原因,它必须能被回溯——店主问"为什么这条没发
            # 出去",答案得指得出一行记录,而不是重新跑一遍当时的判断。
            # correlation_id 让这行记录能挂回产生它的那条协作链。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS promotion_pauses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    correlation_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    until TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_promo_pause_subject "
                "ON promotion_pauses(subject, until DESC)")
            conn.commit()
        finally:
            conn.close()

    # ---------- 读方法 ----------
    def _order_from_row(self, conn, row) -> dict:
        order = dict(row)
        items = conn.execute(
            "SELECT name, sku, quantity, price FROM order_items WHERE order_id = ? ORDER BY id",
            (order["order_id"],),
        ).fetchall()
        order["items"] = [dict(i) for i in items]
        return order

    def get_order(self, order_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            return self._order_from_row(conn, row) if row else None
        finally:
            conn.close()

    def list_orders(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
            return [self._order_from_row(conn, r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _product_from_row(row) -> dict:
        p = dict(row)
        p["specs"] = json.loads(p["specs"]) if p.get("specs") else {}
        return p

    def get_product(self, product_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
            return self._product_from_row(row) if row else None
        finally:
            conn.close()

    def all_products(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM products").fetchall()
            return [self._product_from_row(r) for r in rows]
        finally:
            conn.close()

    def get_logistics(self, tracking_number: str) -> Optional[dict]:
        conn = self.connect()
        try:
            ship = conn.execute(
                "SELECT * FROM shipments WHERE tracking_number = ?", (tracking_number,)
            ).fetchone()
            if not ship:
                return None
            events = conn.execute(
                "SELECT time, location, description FROM logistics_events "
                "WHERE tracking_number = ? ORDER BY seq",
                (tracking_number,),
            ).fetchall()
            return {
                "tracking_number": ship["tracking_number"],
                "carrier": ship["carrier"],
                "status": ship["status"],
                "events": [dict(e) for e in events],
            }
        finally:
            conn.close()

    # ---------- 写方法 ----------
    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def create_order(self, user: str, items: list[dict], total: float,
                     status: str = "pending", shipping_address: str = "") -> dict:
        """自助下单:把用户在商城/商品卡点『立即购买』的商品写入订单库。
        items: [{name, sku, quantity, price}]。返回新建订单(含 items),供前端与
        list_user_orders 读取。order_id 格式 ORD-YYYYMMDD-XXXX(日期+随机后缀)。"""
        import uuid
        now = self._now()
        oid = f"ORD-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO orders (order_id, user, status, total, created_at, shipping_address) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (oid, user, status, total, now, shipping_address),
            )
            conn.executemany(
                "INSERT INTO order_items (order_id, name, sku, quantity, price) VALUES (?, ?, ?, ?, ?)",
                [(oid, it.get("name"), it.get("sku", ""), it.get("quantity", 1), it.get("price", 0))
                 for it in items],
            )
            conn.commit()
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (oid,)).fetchone()
            return self._order_from_row(conn, row)
        finally:
            conn.close()

    # ---------- 下单幂等 ----------

    def claim_idempotency_key(self, user: str, key: str) -> tuple[str, str | None]:
        """抢占一个幂等键。返回 `(状态, order_id)`,状态三取一:

        - `"claimed"`:本次抢到了,调用方**应当**去建单,建完调 `finish_idempotency_key`;
        - `"done"`:这个键之前已经建过单,`order_id` 是那一笔——直接返回它,别再建;
        - `"in_flight"`:另一个请求正抢着这个键、还没建完(order_id 仍是 NULL),
          调用方应告诉客户端稍后重试,**绝不能自己再建一笔**。

        胜负由 `UNIQUE(user, key)` 判,不是"先 SELECT 再 INSERT"——后者在并发下两个
        请求会同时查到"没有",然后各建一笔(本仓库已经修过好几处同形状的先读后写)。
        `INSERT OR IGNORE` 的 rowcount 直接告诉我们是不是自己插进去的。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO order_idempotency (user, key, order_id, created_at) "
                "VALUES (?, ?, NULL, ?)", (user, key, self._now()))
            conn.commit()
            if cur.rowcount == 1:
                return "claimed", None
            row = conn.execute(
                "SELECT order_id FROM order_idempotency WHERE user = ? AND key = ?",
                (user, key)).fetchone()
            # row 理论上必然存在(插入被 IGNORE 说明有冲突行);真拿不到时按抢到处理,
            # 宁可重复建单也不要把买家卡在一个永远重试的状态上。
            if row is None:
                return "claimed", None
            oid = row["order_id"] if not isinstance(row, tuple) else row[0]
            return ("done", oid) if oid else ("in_flight", None)
        finally:
            conn.close()

    def finish_idempotency_key(self, user: str, key: str, order_id: str) -> None:
        """把建好的订单号回填到占位行上。只填**还没填过**的那行(条件更新)。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE order_idempotency SET order_id = ? "
                "WHERE user = ? AND key = ? AND order_id IS NULL",
                (order_id, user, key))
            conn.commit()
        finally:
            conn.close()

    def release_idempotency_key(self, user: str, key: str) -> None:
        """建单失败时释放占位行,让买家能用同一个键重试。

        不释放的话,一次失败的下单会把这个键永久钉在 `in_flight` 上——买家点重试
        拿到的永远是"正在处理中",而实际上什么都没建。**失败必须可重试。**
        """
        conn = self.connect()
        try:
            conn.execute(
                "DELETE FROM order_idempotency WHERE user = ? AND key = ? AND order_id IS NULL",
                (user, key))
            conn.commit()
        finally:
            conn.close()

    def purge_idempotency_keys(self, older_than_hours: int = 24) -> int:
        """清理过期键,返回删除行数。**离线调用**,不挂在请求路径上。"""
        from app.db import dialect

        # 走方言层而不是手写 `datetime('now', ...)`:时钟表达式只要有第二处,换库时
        # 一定会漏掉其中一份(`tests/test_db_dialect.py` 会拦住手写的——我第一版就是
        # 手写的,被那条测试逮住了)。
        conn = self.connect()
        try:
            cur = conn.execute(
                "DELETE FROM order_idempotency "
                f"WHERE created_at < {dialect.now_minus(int(older_than_hours), 'hours')}")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    #: 允许发起退款的状态。未支付(`unpaid`)**不在其中**——没收到的钱不存在"退回",
    #: 那种情况正确的动作是取消订单。已取消/已在退款中同样不在其中(重复申请)。
    REFUNDABLE_STATUSES = ("pending", "shipped", "delivered")

    def set_refund(self, order_id: str, reason: str) -> bool:
        """发起退款;**只对当前处于可退状态的行生效**,否则返回 False。

        改造前这条 UPDATE 只有 `WHERE order_id = ?`,状态机的守卫全在
        `app/agent/tools/refund.py` 的"先读 status 再写"里——那是个竞态。实测 16 个
        并发申请,**12 个都返回了成功**并各自对买家说了一句"退款申请已提交"。

        把状态条件下沉到这条 UPDATE 上,与 `pay_order` / `review_outreach_draft` /
        `mark_outreach_sent` 同一套幂等纪律:并发只有一个能赢,输的那些拿到 False,
        由调用方如实告诉买家"已有申请在处理中"。
        """
        placeholders = ",".join("?" * len(self.REFUNDABLE_STATUSES))
        conn = self.connect()
        try:
            cur = conn.execute(
                f"""UPDATE orders
                   SET status = 'refund_processing', refund_status = '审核中',
                       refund_reason = ?, refund_requested_at = ?
                   WHERE order_id = ? AND status IN ({placeholders})""",
                (reason, self._now(), order_id, *self.REFUNDABLE_STATUSES),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_order_status(self, order_id: str, status: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def archive_session(self, session_id: str, user_id: str,
                        messages: list, summary: Optional[str],
                        source: str | None = None) -> None:
        """会话冷归档:把完整会话写入 session_archive(审计/离线分析,永久留存)。

        `source` 与 `skill_traces` 同口径:归档是**门禁用例合成与失败改进的语料源**,
        一段压测对话被当成真实买家语料蒸馏进 SKILL.md,和一条压测轨迹被算进成功率
        一样糟 —— 只是后者立刻显形,前者要等到线上说错话才显形。
        """
        source = source or _traffic_source()
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO session_archive (session_id, user_id, messages, summary, "
                "msg_count, archived_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, json.dumps(messages, ensure_ascii=False),
                 summary, len(messages or []), self._now(), source),
            )
            conn.commit()
        finally:
            conn.close()

    def archive_session_if_changed(self, session_id: str, user_id: str,
                                   messages: list, summary: Optional[str]) -> bool:
        """仅当该会话内容有增长时才归档,返回是否真的写入。

        archive_session 是纯 INSERT,反复调用会造成同一会话多行归档 —— 那会让离线
        聚类过度加权同一段对话。故按 msg_count 比对最近一条归档:相同则跳过。
        保留"内容增长才追加一行"的语义(审计仍能看到演进过程),而不是覆盖。
        """
        count = len(messages or [])
        if count == 0:
            return False
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT msg_count FROM session_archive WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            if row is not None and (row["msg_count"] or 0) >= count:
                return False
        finally:
            conn.close()
        self.archive_session(session_id, user_id, messages, summary)
        return True

    def get_archived_session(self, session_id: str) -> Optional[dict]:
        """取该会话最近一条归档(测试/查询用)。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM session_archive WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_recent_archives(self, limit: int = 50,
                             sources: Optional[list[str]] = None) -> list[dict]:
        """H3 离线合成入口用:按 id DESC 取近 N 条会话归档,messages json.loads 成 list。

        坏 JSON（messages 字段无法解析）的记录直接跳过，不让单条脏数据崩离线脚本。

        `sources`:只取这些来源的归档。归档是**门禁用例合成与失败改进的语料源**,
        一段压测对话被当成真实买家语料蒸馏进 SKILL.md,和一条压测轨迹被算进成功率
        一样糟——只是后者立刻显形,前者要等到线上说错话才显形。
        """
        sql = "SELECT * FROM session_archive"
        params: list = []
        if sources:
            sql += (" WHERE COALESCE(source, 'unknown') IN "
                    f"({','.join('?' * len(sources))})")
            params.extend(sources)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["messages"] = json.loads(item["messages"]) if item["messages"] else []
                except (json.JSONDecodeError, TypeError):
                    continue
                results.append(item)
            return results
        finally:
            conn.close()

    # ---------- Skill 执行轨迹(G2:每轮"加载了哪个 skill/调了哪些工具/结局如何") ----------
    def record_skill_trace(self, session_id: str, user_id: str, skill_name: str,
                           tool_calls: list[dict], outcome: str,
                           variant: str = "live", skill_version: int = 0,
                           skill_fingerprint: str = "unknown",
                           source: str | None = None) -> None:
        """记录一轮 skill 执行轨迹。variant 区分现行版/灰度候选,供 A/B 判定。

        skill_version:本轮**加载那一刻**的技能目录版本号,0=未知(老调用方/历史行
        不传即落 0,不假装是第 1 版——同一 skill 转正两次后仍要能按版本分开归因)。

        skill_fingerprint:本轮**加载那一刻**被服务的那棵树的内容指纹,"unknown"=
        未知(老调用方/历史行不传即落 unknown,同样不瞎猜)。与 skill_version 互补:
        version 只在正式目录转正/回滚时递增,候选目录从不带 .version,灰度期永远
        读到"1"——指纹不依赖任何创建候选的地方打标,直接对被服务的树现算内容哈希,
        天然能把两批不同候选的轨迹分开归因(这正是本列存在的理由)。
        """
        source = source or _traffic_source()
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
                "outcome, created_at, variant, skill_version, skill_fingerprint, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, skill_name,
                 json.dumps(tool_calls or [], ensure_ascii=False), outcome,
                 self._now(), variant, skill_version, skill_fingerprint or "unknown",
                 source),
            )
            conn.commit()
        finally:
            conn.close()

    def skill_trace_source_counts(self) -> dict[str, int]:
        """全库轨迹按流量来源分布 `{source: n}`。给界面披露口径用。

        **界面上必须显示它。** 一个"成功率 28%"旁边如果没有"其中真实流量 N 轮",
        看的人无从判断这个数字讲的是线上还是压测。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT COALESCE(source, 'unknown') AS source, COUNT(*) AS n "
                "FROM skill_traces GROUP BY COALESCE(source, 'unknown')").fetchall()
        finally:
            conn.close()
        return {str(dict(r)["source"]): int(dict(r)["n"]) for r in rows}

    def skill_trace_counts(self, sources: Optional[list[str]] = None
                           ) -> dict[str, dict[str, int]]:
        """每个 skill 各结局的**全时段**条数:`{skill: {outcome: n}}`。

        **为什么不复用 `list_skill_traces` 再在内存里数。** 那个函数按 id DESC 取
        最近 N 条,窗口是所有 skill、所有结局**共用**的。失败远少于成功,一段正常
        运行就能把窗口填满——实测 track-order 真实成绩是 success 15 / tool_error 38
        (成功率 28%),而按最近 500 条数出来只剩 `success: 1`,界面上打出**实战
        成功率 100%**,旁边同时列着 38 次失败归因。两个数字互相打脸,而"100%"
        是彻底错的。

        取数是一次聚合(COUNT + GROUP BY),不搬行,几万条也是毫秒级——本来就没有
        理由为了这个去截断窗口。

        `sources`:只统计这些来源的轨迹(如 `["live"]`)。传 None = 不过滤(全部)。
        调用方**必须显式决定**要不要过滤,并把口径写在界面上:一个不带口径的
        成功率,读的人无从判断它讲的是线上还是压测。
        """
        sql = "SELECT skill_name, outcome, COUNT(*) AS n FROM skill_traces"
        params: list = []
        if sources:
            # COALESCE:老库补列时历史行已回填成 'unknown',但别的写入路径仍可能
            # 留下 NULL —— NULL 不等于任何值,会被 IN 静默漏掉,于是那些行既不算
            # live 也不算 unknown,凭空从统计里消失。
            sql += (" WHERE COALESCE(source, 'unknown') IN "
                    f"({','.join('?' * len(sources))})")
            params.extend(sources)
        sql += " GROUP BY skill_name, outcome"
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            item = dict(row)
            name = item.get("skill_name") or ""
            out.setdefault(name, {})[item.get("outcome") or "unknown"] = int(item["n"])
        return out

    def list_skill_traces(self, skill_name: Optional[str] = None,
                          outcomes: Optional[list[str]] = None,
                          limit: int = 200,
                          sources: Optional[list[str]] = None) -> list[dict]:
        """按 id DESC 取轨迹;可按 skill 名、结局、**流量来源**过滤。

        tool_calls 反序列化成 list,坏 JSON 的行跳过(与 list_recent_archives
        同口径,单条脏数据不崩离线脚本)。

        `sources` 默认 None = 不过滤。**判定类调用方必须显式传 `["live"]`**——
        看门狗在压测流量上算出的成功率会把一份没问题的 skill 自动回滚掉
        (见 runtime_context 里 SOURCE_* 的注释)。默认不过滤是刻意的:它保证
        既有调用方行为不被悄悄改掉,逼每一处都被显式检视一遍。
        """
        sql = "SELECT * FROM skill_traces"
        clauses: list[str] = []
        params: list = []
        if skill_name:
            clauses.append("skill_name = ?")
            params.append(skill_name)
        if outcomes:
            clauses.append(f"outcome IN ({','.join('?' * len(outcomes))})")
            params.extend(outcomes)
        if sources:
            clauses.append("COALESCE(source, 'unknown') IN "
                           f"({','.join('?' * len(sources))})")
            params.extend(sources)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["tool_calls"] = json.loads(item["tool_calls"]) if item["tool_calls"] else []
                except (json.JSONDecodeError, TypeError):
                    continue
                results.append(item)
            return results
        finally:
            conn.close()

    # ---------- Skill 灰度登记(分级授权:候选按会话接管部分流量) ----------
    def start_canary(self, skill_name: str, candidate_path: str, percent: int,
                     risk: str, policy: str) -> None:
        """登记一个活跃灰度。同 skill 已有活跃记录先置 superseded,保证同时只有一个。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE skill_canaries SET status = 'superseded', finished_at = ? "
                "WHERE skill_name = ? AND status = 'active'", (self._now(), skill_name))
            conn.execute(
                "INSERT INTO skill_canaries (skill_name, candidate_path, percent, risk, "
                "policy, status, started_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (skill_name, candidate_path, percent, risk, policy, self._now()))
            conn.commit()
        finally:
            conn.close()

    def get_active_canary(self, skill_name: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM skill_canaries WHERE skill_name = ? AND status = 'active' "
                "ORDER BY id DESC LIMIT 1", (skill_name,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_active_canaries(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM skill_canaries WHERE status = 'active' "
                "ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def finish_canary(self, skill_name: str, status: str) -> bool:
        """结束该 skill 的活跃灰度(status: promoted / rolled_back)。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE skill_canaries SET status = ?, finished_at = ? "
                "WHERE skill_name = ? AND status = 'active'",
                (status, self._now(), skill_name))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_shipping_address(self, order_id: str, address: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE orders SET shipping_address = ? WHERE order_id = ?",
                (address, order_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_stock(self, product_id: str, stock: int) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE products SET stock = ? WHERE product_id = ?", (stock, product_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---------- 议价状态 ----------
    def get_bargain_state(self, session_id: str, product_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT rounds, last_offer FROM bargain_sessions "
                "WHERE session_id = ? AND product_id = ?",
                (session_id, product_id),
            ).fetchone()
            return {"rounds": row["rounds"], "last_offer": row["last_offer"]} if row else None
        finally:
            conn.close()

    def bump_bargain_state(self, session_id: str, product_id: str, offer: float) -> None:
        conn = self.connect()
        try:
            now = self._now()
            conn.execute(
                """INSERT INTO bargain_sessions (session_id, product_id, rounds, last_offer, updated_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(session_id, product_id)
                   DO UPDATE SET rounds = rounds + 1, last_offer = ?, updated_at = ?""",
                (session_id, product_id, offer, now, offer, now),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 议价成交(与上面的谈判过程分开,见建表处的说明) ----------

    def record_bargain_deal(self, user: str, sku: str, price: float,
                            ttl_hours: int = 24) -> int:
        """记一笔成交:该买家可在 `ttl_hours` 内按 `price` 买这件商品。返回记录 id。

        **同一 (user, sku) 只保留最新一笔未兑现的**:买家可能谈了一轮不满意、
        再谈一轮谈得更低,留着旧的那笔没有意义,而且会让"到底按哪个价"变成一个
        要看时间戳才能回答的问题。
        """
        from app.db import dialect

        # 非正的 ttl 会让 `datetime('now','+-1 hours')` 求值成 NULL,撞上 expires_at
        # 的 NOT NULL——报出来是一条看不出原因的 IntegrityError。这里明确拒绝。
        if int(ttl_hours) <= 0:
            raise ValueError(f"议价成交有效期必须为正小时数,收到 {ttl_hours}")

        conn = self.connect()
        try:
            conn.execute(
                "DELETE FROM bargain_deals WHERE user = ? AND sku = ? AND consumed_at IS NULL",
                (user, sku))
            # 用方言层的 RETURNING 而不是 `cur.lastrowid`:后者是 sqlite3 驱动特有的,
            # psycopg 没有(`tests/test_db_dialect.py` 会拦住——我第一版就写的 lastrowid)。
            cur = conn.execute(
                dialect.returning_id(
                    "INSERT INTO bargain_deals (user, sku, price, created_at, expires_at) "
                    f"VALUES (?, ?, ?, {dialect.now()}, {dialect.now_plus_param('hours')})"),
                (user, sku, round(float(price), 2), int(ttl_hours)))
            new_id = cur.fetchone()[0]
            conn.commit()
            return int(new_id)
        finally:
            conn.close()

    def active_bargain_deal(self, user: str, sku: str) -> Optional[dict]:
        """取该买家在这件商品上**未兑现且未过期**的成交价;没有则 None。"""
        from app.db import dialect

        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id, price, expires_at FROM bargain_deals "
                f"WHERE user = ? AND sku = ? AND consumed_at IS NULL AND expires_at > {dialect.now()} "
                "ORDER BY id DESC LIMIT 1",
                (user, sku)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def consume_bargain_deal(self, deal_id: int, order_id: str) -> bool:
        """把成交标记为已兑现。**条件更新**:只对还没兑现的那行生效。

        与 `pay_order` / `set_refund` 同一套幂等纪律——并发下单时只有一个能兑到,
        另一个拿 False 并按标价走。"先查再改"在这里等于让同一笔成交兑出两单。
        """
        from app.db import dialect

        conn = self.connect()
        try:
            cur = conn.execute(
                f"UPDATE bargain_deals SET consumed_at = {dialect.now()}, order_id = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                (order_id, int(deal_id)))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def clear_bargain_state(self, session_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute("DELETE FROM bargain_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    # ---------- 会话生命周期(服务端签发 conversation_id + open/closed 状态机) ----------
    def create_conversation(self, user_id: str) -> dict:
        import uuid
        cid = "c-" + uuid.uuid4().hex[:16]
        now = datetime.now().isoformat(timespec="seconds")
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO conversations (conversation_id, user_id, status, created_at) "
                "VALUES (?, ?, 'open', ?)", (cid, user_id, now))
            conn.commit()
        finally:
            conn.close()
        return {"conversation_id": cid, "user_id": user_id, "status": "open", "created_at": now}

    def get_conversation(self, conversation_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?",
                               (conversation_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def close_conversation(self, conversation_id: str, reason: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE conversations SET status='closed', closed_at=?, close_reason=? "
                "WHERE conversation_id = ? AND status='open'",
                (datetime.now().isoformat(timespec="seconds"), reason, conversation_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def latest_open_conversation(self, user_id: str) -> Optional[dict]:
        """取该用户最近**活跃**的 open 会话(按最后消息时间),登录即续上上次聊到的那条,
        而非最新创建的空会话——保证历史连续可见。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? AND status='open' "
                "ORDER BY COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT 1",
                (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def latest_conversation(self, user_id: str) -> Optional[dict]:
        """取该用户最近活跃的会话(**任意状态**),作为其唯一"规范会话"——
        单一连续会话模型下,登录/发消息都复用它(关了就重开),永不碎片化。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? "
                "ORDER BY COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT 1",
                (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def reopen_conversation(self, conversation_id: str) -> None:
        """重开一条已关闭的会话(单一连续会话:不新建,续用同一条)。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE conversations SET status='open', closed_at=NULL, close_reason=NULL "
                "WHERE conversation_id=?", (conversation_id,))
            conn.commit()
        finally:
            conn.close()

    def list_conversations(self, user_id: str, limit: int = 20) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (user_id, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_all_conversations(self, limit: int = 50) -> list[dict]:
        """跨用户列会话:进行中(open)优先,再按最后活跃时间倒序。供坐席工作台聚合。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations "
                "ORDER BY (status='open') DESC, COALESCE(updated_at, created_at) DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def touch_conversation(self, conversation_id: str) -> None:
        """更新会话最后活跃时间(每轮对话/人工回复时调用),供工作台排序与显示。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (datetime.now().isoformat(timespec="seconds"), conversation_id))
            conn.commit()
        finally:
            conn.close()

    # ---------- 用户(存在性校验:先创建才可用) ----------
    def get_user(self, user_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT user_id, name, member_level FROM users WHERE user_id = ?",
                (user_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["member_level"] = d.get("member_level") or "normal"
            return d
        finally:
            conn.close()

    def create_user(self, user_id: str, name: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO users (user_id, name) VALUES (?, ?)",
                (user_id, name))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def count_user_orders(self, user_id: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM orders WHERE user = ?",
                               (user_id,)).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def set_member_level(self, user_id: str, level: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE users SET member_level = ? WHERE user_id = ?",
                         (level, user_id))
            conn.commit()
        finally:
            conn.close()

    # ---------- 会话冷快照(S1:热会话过期后历史回显兜底,与审计用 session_archive 分离) ----------
    def upsert_session_snapshot(self, session_id: str, user_id: str,
                                messages: list, summary) -> None:
        """会话冷快照:每会话恒一行最新态(upsert)。热会话过期后供历史回显兜底。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO session_snapshots (session_id, user_id, messages, summary, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "user_id=excluded.user_id, messages=excluded.messages, "
                "summary=excluded.summary, updated_at=excluded.updated_at",
                (session_id, user_id, json.dumps(messages or [], ensure_ascii=False),
                 summary, self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session_snapshot(self, session_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT session_id, user_id, messages, summary, updated_at "
                "FROM session_snapshots WHERE session_id = ?", (session_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["messages"] = json.loads(d["messages"]) if d["messages"] else []
            except (ValueError, TypeError):
                d["messages"] = []
            return d
        finally:
            conn.close()

    def delete_session_snapshot(self, session_id: str) -> None:
        """删除某会话冷快照(重置对话时清历史,避免过期回显残留旧消息)。"""
        conn = self.connect()
        try:
            conn.execute("DELETE FROM session_snapshots WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    # ---------- 多 Agent 协作总线(持久化 append-only 事件) ----------
    def publish_event(self, event_type: str, payload: dict, source_agent: str,
                      target_agent: str, correlation_id: str,
                      priority: int = 0, status: str = "pending") -> int:
        """发布一条协作事件,返回自增 id。

        总线是**持久化**的:进程重启不丢事件,且 correlation_id 把一条协作链
        (信号→洞察→草稿→发送)串起来,全链可回溯审计。

        priority 越大越先被认领(见 claim_events);默认 0 = 普通。取值由总线
        路由表声明(见 app/multi_agent/routing.py 的 Subscription.priority),
        不由发布方现场决定——"这条有多急"是编排层的判断,不是生产方的判断。

        `status` 只为**一种**非常规写入而存在:`'no_subscriber'` 的终止记录
        (见 `bus.publish`)。它不是一条待办,而是一条"这条链到此为止"的墓碑,
        所以刻意**不**落成 pending。

        这个取值进不了任何一条工作流查询,三处都按 status/target 精确作用域:
          claim_events    `target_agent = ? AND status = 'pending'`
          reclaim_stale   `status = 'processing'`
          list/count_failed `status = 'failed'`
        `list_events`(时间线)不带 status 过滤,所以墓碑**只出现在给人看的地方**
        ——这正是它存在的全部目的。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO agent_events (event_type, payload, source_agent, "
                    "target_agent, correlation_id, status, priority, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"),
                (event_type, json.dumps(payload or {}, ensure_ascii=False),
                 source_agent, target_agent, correlation_id, str(status),
                 int(priority), self._now()))
            # 先取回再 commit:RETURNING 的结果在游标里,提交会重置它。
            new_id = int(cur.fetchone()["id"])
            conn.commit()
            return new_id
        finally:
            conn.close()

    def claim_events(self, target_agent: str, limit: int = 20) -> list[dict]:
        """**原子认领**该 Agent 的待处理事件:pending → processing,并返回认领到的行。

        幂等的关键:UPDATE 带 `status='pending'` 条件,两个 worker 并发时只有一个
        能把某行改成 processing,另一个的 rowcount 为 0 拿不到它。绝不能改成
        "先 SELECT 再 UPDATE"——那样同一条异常会产出两份洞察/两份草稿。

        坏 JSON 的 payload 行跳过(与 list_skill_traces 同口径),单条脏数据不拖垮
        整个消费循环;但它已被置为 processing,不会反复卡住队列。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                # 优先级高的先认领;**同优先级仍严格 FIFO**(id ASC)——这一点
                # 不能丢:同一类事件之间的先来后到是可预期性的来源,乱序会让
                # "为什么这条比那条晚处理"变得无法解释。
                "SELECT id FROM agent_events WHERE target_agent = ? AND status = 'pending' "
                "ORDER BY priority DESC, id ASC LIMIT ?", (target_agent, limit)).fetchall()
            claimed: list[dict] = []
            for row in rows:
                cur = conn.execute(
                    "UPDATE agent_events SET status = 'processing', consumed_at = ? "
                    "WHERE id = ? AND status = 'pending'", (self._now(), row["id"]))
                if cur.rowcount == 0:
                    continue          # 已被别的 worker 认领
                full = conn.execute(
                    "SELECT * FROM agent_events WHERE id = ?", (row["id"],)).fetchone()
                item = dict(full)
                try:
                    item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
                except (json.JSONDecodeError, TypeError):
                    # 脏行已置 processing,不会反复卡队列;但不能悄悄消失——
                    # 这是一次真实的数据损坏,必须留痕供排查。
                    logger.warning(
                        "claim_events: agent_events id=%s target_agent=%s "
                        "correlation_id=%s payload 无法解析为 JSON,已置 processing "
                        "但跳过返回(不会再被 claim_events 捞到,需人工核查)",
                        item.get("id"), item.get("target_agent"),
                        item.get("correlation_id"))
                    continue
                claimed.append(item)
            conn.commit()
            return claimed
        finally:
            conn.close()

    def finish_event(self, event_id: int, status: str) -> bool:
        """结束一条事件(status: done / failed)。只对 processing 的行生效。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_events SET status = ? WHERE id = ? AND status = 'processing'",
                (status, event_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reclaim_stale_events(self, older_than_seconds: int = 300) -> int:
        """回收滞留在 processing 超过阈值的事件:processing → pending。

        worker 认领(claim_events)后若在 finish_event 之前崩溃,该行会永久卡在
        processing——没有超时/恢复机制的话,重启的 worker 再也捞不到它。本方法
        按 consumed_at 与阈值比较,把过期的 processing 行放回 pending,下一次
        claim_events 即可重新认领。

        条件 UPDATE(status = 'processing' AND consumed_at <= 阈值)与
        claim_events/finish_event 同一套幂等纪律:只改状态仍是 processing 且确实
        过期的行,并发调用互不冲突;done/failed 行的 status 不是 processing,
        永远不会被本方法触碰。返回被回收的行数。
        """
        from datetime import timedelta
        threshold = (datetime.now() - timedelta(seconds=older_than_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_events SET status = 'pending', consumed_at = NULL "
                "WHERE status = 'processing' AND consumed_at IS NOT NULL "
                "AND consumed_at <= ?",
                (threshold,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def list_failed_events(self, limit: int = 50) -> list[dict]:
        """列出处理失败的协作事件(status='failed')。

        为什么需要一个专门的入口:`consume()` 刻意**不自动重试** failed 事件
        (避免一条坏事件无限循环),注释里写的是"留在表里供人工在时间线上看到
        并决定"。但时间线端点必须先知道 `correlation_id` 才查得到——也就是说
        在有这个方法之前,一条失败的协作链**没有任何人会发现**:没有告警、
        没有面板、没有列出入口。"留给人工决定"事实上是"留给没人"。

        payload 保持原始 JSON 文本不解析:这些行本来就可能是因为 payload 有
        问题才失败的,再解析一次只会在展示路径上重现同一个异常。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, event_type, source_agent, target_agent, correlation_id, "
                "       payload, created_at, consumed_at "
                "FROM agent_events WHERE status = 'failed' "
                "ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_failed_events(self) -> int:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_events WHERE status = 'failed'").fetchone()
            return int(row["n"] or 0)
        finally:
            conn.close()

    def retry_failed_event(self, event_id: int) -> bool:
        """把一条失败事件放回待处理队列(failed → pending),供人工决定重试。

        条件更新(只对仍是 failed 的行生效),与 claim_events/finish_event 同一
        套幂等纪律:连点两次、两个运营同时点,只有第一次真的改到状态。
        `consumed_at` 一并清空,否则 `reclaim_stale_events` 会看到一条"很久以前
        就被认领"的 pending 行——虽然它只挑 processing,但留着一个语义已经失效
        的时间戳只会让后来者读不懂这张表。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_events SET status = 'pending', consumed_at = NULL "
                "WHERE id = ? AND status = 'failed'", (event_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def record_worker_heartbeat(self, name: str, ok: bool, error: str = "") -> None:
        """记一次 worker 心跳。ok=True 更新 last_success_at,否则记 last_error。

        为什么要有它:`reclaim_stale_events` 能救"worker 认领后崩在半路"的事件,
        但救不了"worker 进程整个死了"——那种情况下没有任何人去调 reclaim,协作
        静默停摆,而买家链路一切正常,不会有任何症状暴露出来。心跳是这件事唯一
        的可观测信号。

        fail-soft:心跳写不进去绝不能反过来影响那一轮真正的协作工作。
        """
        conn = self.connect()
        try:
            now = self._now()
            if ok:
                conn.execute(
                    "INSERT INTO worker_heartbeats (name, last_success_at, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                    "last_success_at = excluded.last_success_at, updated_at = excluded.updated_at",
                    (name, now, now))
            else:
                conn.execute(
                    "INSERT INTO worker_heartbeats (name, last_error_at, last_error, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                    "last_error_at = excluded.last_error_at, "
                    "last_error = excluded.last_error, updated_at = excluded.updated_at",
                    (name, now, (error or "")[:500], now))
            conn.commit()
        except Exception:  # noqa: BLE001 心跳失败不得影响本轮协作
            logger.warning("写 worker 心跳失败 name=%s", name, exc_info=True)
        finally:
            conn.close()

    def get_worker_heartbeat(self, name: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM worker_heartbeats WHERE name = ?", (name,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def no_subscriber_stats(self, limit: int = 20) -> dict:
        """无订阅者终止记录的统计与样例(见 `bus.STATUS_NO_SUBSCRIBER`)。

        为什么值得单独有一个出口:这类记录数**不是**故障指标,但它是"哪些信号
        产出的结论没有任何下游"的唯一量化线索。实测形态是 462 条
        tool_error_rate_high 的诊断无人订阅——参谋归因了、写进了共享上下文,
        然后链就到此为止。那不是 bug,但它是一个明确的产品缺口:一个工具失败率
        告警的正确下游是工程处置,而那个 Agent 不存在。

        **按 event_type 分组而不是只给总数**:总数只会告诉运维"有一堆链断了",
        分组才指得出断在哪一类事件上——而那正是"该不该给它加个订阅者"这个决定
        需要的信息。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT event_type, COUNT(*) AS n FROM agent_events "
                "WHERE status = ? GROUP BY event_type ORDER BY n DESC",
                ("no_subscriber",)).fetchall()
            by_type = [{"event_type": r["event_type"], "count": int(r["n"])}
                       for r in rows]
            recent = conn.execute(
                "SELECT * FROM agent_events WHERE status = ? "
                "ORDER BY id DESC LIMIT ?",
                ("no_subscriber", max(1, int(limit)))).fetchall()
            items = []
            for row in recent:
                item = dict(row)
                # payload 反序列化与 list_events 同一姿态:坏 JSON 退成空 dict,
                # 不让一条脏数据把整个可见性端点打成 500。
                try:
                    item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
                except (json.JSONDecodeError, TypeError):
                    item["payload"] = {}
                items.append(item)
            return {"total": sum(x["count"] for x in by_type),
                    "by_event_type": by_type, "recent": items}
        finally:
            conn.close()

    def list_pending_for_target(self, target_agent: str, limit: int = 50) -> list[dict]:
        """某个 target 的待处理事件(不认领,只看)。

        为「人工闸」而加。路由表把 `action.drafts_ready` / `result.outreach_*`
        都投给 `human`,声明是"需要人来看的事件投给它"——但 worker 只消费
        analyst 与 growth 两个 target,**human 的事件没有任何消费方,也没有任何
        界面列出它们**。实测积压 325 条,永远停在 pending。
        与 failed 事件曾经的处境完全一样:"留给人工"事实上是"留给没人"。

        用 `ORDER BY priority DESC, id ASC` 与 `claim_events` 同序:人看到的顺序
        应当与系统认为的轻重缓急一致,不该一个按时间倒序、一个按优先级。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE target_agent = ? AND status = 'pending' "
                "ORDER BY priority DESC, id ASC LIMIT ?",
                (target_agent, limit)).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
                except (json.JSONDecodeError, TypeError):
                    item["payload"] = {}
                out.append(item)
            return out
        finally:
            conn.close()

    def count_pending_for_target(self, target_agent: str) -> int:
        conn = self.connect()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) c FROM agent_events "
                "WHERE target_agent = ? AND status = 'pending'",
                (target_agent,)).fetchone()["c"])
        finally:
            conn.close()

    def acknowledge_event(self, event_id: int) -> bool:
        """人工确认一条待处理事件(pending → done)。条件更新,连点两次只第一次生效。

        与 `finish_event` 分开:那个只对 `processing` 生效(worker 认领后的收尾),
        而人工闸的事件**从来不会被认领**——没有任何 worker 消费 human。
        复用它会静默失败(0 行受影响却返回 False),看起来像"点了没反应"。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_events SET status = 'done', consumed_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (self._now(), event_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_events(self, correlation_id: Optional[str] = None,
                    limit: int = 100) -> list[dict]:
        """按 id DESC 列事件(可按协作链过滤),供时间线可视化与审计。"""
        sql = "SELECT * FROM agent_events"
        params: list = []
        if correlation_id:
            sql += " WHERE correlation_id = ?"
            params.append(correlation_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
                except (json.JSONDecodeError, TypeError):
                    item["payload"] = {}
                results.append(item)
            return results
        finally:
            conn.close()

    def list_event_chains(self, limit: int = 30) -> list[dict]:
        """按 correlation_id 聚合出最近的协作链清单。

        为什么必须有这个:`list_events(correlation_id=...)` 要求调用方**先知道**
        correlation_id,而在此之前没有任何地方列出过它——时间线端点因此事实上
        不可达,和失败事件"留在表里供人工决定"却没有列表入口是同一个缺陷
        (见 `/api/admin/collab/health` 的注释)。一个查得到但没人找得着入口的
        视图,等于不存在。

        聚合发生在 SQL 里而不是取回全部事件再在 Python 里 group:事件表随时间
        无上界增长,先取回再聚合迟早会把整张表拉进内存。

        `agents` 用 group_concat 拼出这条链碰过哪些 Agent(去重在上层做——
        SQLite 的 `group_concat(DISTINCT x)` 不支持自定义分隔符)。
        没有 correlation_id 的事件(旧数据/直投)归不进任何链,直接排除:
        把它们混成一条名为空串的"链"只会造出一条假的、包含无关事件的时间线。
        """
        sql = f"""
            SELECT correlation_id,
                   COUNT(*)                              AS events,
                   MIN(created_at)                       AS started_at,
                   MAX(COALESCE(consumed_at, created_at)) AS last_at,
                   {self.d.count_if("status = 'failed'")}                AS failed,
                   {self.d.count_if("status = 'pending'")}               AS pending,
                   {self.d.count_if("status = 'skipped'")}               AS skipped,
                   -- 这里**刻意不统计"降级"**。降级信息只存在于 shared_context,
                   -- 不在事件表里:降级的诊断按路由规则不唤醒营销,而 resolve()
                   -- 返回空目标时 publish() 压根不插入任何事件行——降级诊断在
                   -- 总线上不留一丝痕迹。实测 insight.diagnosis 事件只有 2 条,
                   -- 而 shared_context 里有 6 条诊断、其中 3 条降级。
                   -- 在这里加一列 `SUM(payload.degraded)` 只会得到恒为 0 的假信号,
                   -- 比不做更糟。降级统计走 /collab/health 的 degraded 段。
                   {self.d.group_concat('source_agent')}  AS sources,
                   {self.d.group_concat('target_agent')}  AS targets,
                   -- 这条链**走到了哪几步**。没有它,前端只能显示"1 个事件",
                   -- 看不出这条链是刚起头还是已经走完——而"卡在哪一步"正是
                   -- 运维看这个页面唯一想知道的事。用 group_concat 拼事件类型,
                   -- 前端按规范链的阶段序还原进度(见 CollabView 的 STAGES)。
                   {self.d.group_concat('event_type')}    AS event_types,
                   -- 每一步的最新状态:同一类事件可能既有 done 又有 pending
                   -- (扇出成多行),拼起来交给前端按"最坏状态优先"归并。
                   {self.d.group_concat('status')}        AS statuses
              FROM agent_events
             WHERE correlation_id IS NOT NULL AND correlation_id != ''
             GROUP BY correlation_id
             ORDER BY MAX(id) DESC
             LIMIT ?
        """
        conn = self.connect()
        try:
            rows = conn.execute(sql, (limit,)).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                raw_targets = (item.get("targets") or "").split(",")
                agents = [a for a in
                          (item.pop("sources") or "").split(",") +
                          (item.pop("targets") or "").split(",") if a]
                # dict.fromkeys 而不是 set:保留首次出现顺序,链上的 Agent 顺序
                # 本身就是给人看的信息(谁先动手),排序打乱它就没意义了。
                item["agents"] = list(dict.fromkeys(agents))
                # 事件类型与状态**按位置一一对应**(同一个 GROUP BY 里的两个
                # group_concat 顺序一致),前端靠这个配对还原每一步的状态。
                # 不在这里配对成对象:SQL 层只负责取,聚合语义留给调用方,
                # 免得这张表的形状被一个展示需求绑死。
                item["event_types"] = [t for t in (item.pop("event_types") or "").split(",") if t]
                item["statuses"] = [t for t in (item.pop("statuses") or "").split(",") if t]
                # targets 也要按位置留一份:一条事件处于 pending 时,前端要说清
                # **在等谁处理**。少了它只能说"等待<这一步>",而那会把"归因结果
                # 已产出、等人看"说成"等待归因"——方向正好反了(实测踩过)。
                item["targets"] = [t for t in raw_targets if t]
                out.append(item)
            return out
        finally:
            conn.close()

    # ---------- 共享上下文池(跨 Agent 可读写,带来源与 TTL) ----------
    def set_shared_context(self, key: str, value: dict, source_agent: str,
                           correlation_id: str, ttl_seconds: int = 86400) -> None:
        """写入共享上下文。必带 source_agent:读到的一方要知道这条是谁写的。"""
        from datetime import datetime, timedelta
        expires = (datetime.now() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO shared_context (key, value, source_agent, correlation_id, "
                "updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "source_agent = excluded.source_agent, "
                "correlation_id = excluded.correlation_id, "
                "updated_at = excluded.updated_at, expires_at = excluded.expires_at",
                (key, json.dumps(value or {}, ensure_ascii=False), source_agent,
                 correlation_id, self._now(), expires))
            conn.commit()
        finally:
            conn.close()

    def get_shared_context(self, key: str) -> Optional[dict]:
        """读共享上下文。已过期或坏 JSON 一律返回 None(读侧不能拿到半截数据)。"""
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM shared_context WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            if item.get("expires_at") and item["expires_at"] <= self._now():
                return None
            try:
                item["value"] = json.loads(item["value"]) if item["value"] else {}
            except (json.JSONDecodeError, TypeError):
                return None
            return item
        finally:
            conn.close()

    def list_shared_context(self, prefix: str = "", limit: int = 50,
                            correlation_id: Optional[str] = None) -> list[dict]:
        """按 key 前缀列未过期的共享上下文(供控制台展示"当前共享了什么")。

        `correlation_id` 非空时**在 SQL 里**过滤到那一条协作链。这不是可有可无的
        优化:LIMIT 作用在 `ORDER BY updated_at DESC` 之上,而 shared_context 每
        条被升级的会话、每个异常商品都会写一行,很快就超过 limit;若先取最近
        limit 行再在 Python 里筛链路,稍旧一点的协作链就会拿到空的 shared 列表
        ——行还在库里,时间线却说"这条链没共享过任何上下文"。
        """
        conn = self.connect()
        sql = ("SELECT * FROM shared_context WHERE key LIKE ? AND "
               "(expires_at IS NULL OR expires_at > ?)")
        params: list = [f"{prefix}%", self._now()]
        if correlation_id:
            sql += " AND correlation_id = ?"
            params.append(correlation_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["value"] = json.loads(item["value"]) if item["value"] else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                results.append(item)
            return results
        finally:
            conn.close()

    # ---------- 触达草稿(营销 Agent 只产草稿,发送必须人工批准) ----------
    def create_outreach_draft(self, opportunity_type: str, user_id: str,
                              order_id: str, content: str, offer: dict,
                              reason: str, correlation_id: str, created_by: str,
                              needs_review_reason: str = "") -> Optional[int]:
        """落一条触达草稿(status 恒为 draft)。返回 draft_id;**已有同类待审草稿
        时返回 None**(不是错误)。

        **本方法是营销 Agent 唯一的写路径**:它永远只能产 draft,发送发生在
        审批端点里。needs_review_reason 非空表示命中了承诺类敏感词,人工要重点看。

        P2:同一 (user_id, order_id, opportunity_type) 在 status='draft' 下由
        `idx_outreach_draft_unique` 保证至多一条,撞了捕获 IntegrityError 返回
        None——与 `start_followup` 完全同一套返回约定(None = "已有一条,本次不
        重复排",调用方据此计入跳过而不是失败)。
        为什么让约束顶上而不是先查后插:并行起草时两个线程会读到同一份"当前
        待审"快照,先查后插必然漏。应用层的去重集合仍然保留,但它从此只是"省一次
        无谓的 LLM 调用"的优化,不再是正确性的唯一防线。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO outreach_drafts (opportunity_type, user_id, order_id, "
                    "content, offer, reason, correlation_id, status, needs_review_reason, "
                    "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)"),
                (opportunity_type, user_id, order_id, content,
                 json.dumps(offer or {}, ensure_ascii=False), reason, correlation_id,
                 needs_review_reason, created_by, self._now()))
            new_id = int(cur.fetchone()["id"])   # RETURNING:先取回再 commit
            conn.commit()
            return new_id
        except Exception as exc:  # noqa: BLE001 只吞唯一约束冲突,其余照抛
            if not self.d.is_duplicate_key(exc):
                raise
            # 唯一索引拒绝:该买家该订单该商机类型已经有一条待审草稿。
            # 不是错误,是去重生效——与 start_followup 同一返回约定。
            return None
        finally:
            conn.close()

    def _draft_from_row(self, row) -> Optional[dict]:
        item = dict(row)
        try:
            item["offer"] = json.loads(item["offer"]) if item["offer"] else {}
        except (json.JSONDecodeError, TypeError):
            return None
        return item

    def list_outreach_drafts(self, status: Optional[str] = None,
                             limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM outreach_drafts"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [d for d in (self._draft_from_row(r) for r in rows) if d]
        finally:
            conn.close()

    def pending_outreach_targets(self) -> set:
        """当前还挂在待审队列里的触达对象集合 `{(user_id, order_id)}`。

        给起草侧做去重用:同一个买家、同一个订单已经躺着一条等人审的草稿时,
        再排一条近乎重复的进去没有任何新增信息,只会稀释审批队列——而店主的
        注意力正是这条链上最稀缺的资源,他挨个批下去就等于给同一个人连发几条。

        只看 `status='draft'`(等人看的那些)。已 approved/sent 的不在其中:那是
        "已经处理过的历史",不该永久封杀对同一个订单的再次触达;rejected 同理,
        店主明确驳回过的内容,下一轮换个说法重新排队是合理的。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT user_id, order_id FROM outreach_drafts WHERE status = 'draft'"
            ).fetchall()
            return {((r["user_id"] or ""), (r["order_id"] or "")) for r in rows}
        finally:
            conn.close()

    def get_outreach_draft(self, draft_id: int) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM outreach_drafts WHERE id = ?",
                               (draft_id,)).fetchone()
            return self._draft_from_row(row) if row else None
        finally:
            conn.close()

    def review_outreach_draft(self, draft_id: int, status: str,
                              reviewed_by: str) -> bool:
        """审批草稿(status: approved / rejected)。**只对 draft 状态生效**。

        条件更新是防重发的关键:两次点"批准"只有第一次拿到 True,发送端点据此
        判断本次是否真的该发,不会给同一个买家发两遍。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status = ?, reviewed_by = ?, reviewed_at = ? "
                "WHERE id = ? AND status = 'draft'",
                (status, reviewed_by, self._now(), draft_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def recent_sent_outreach(self, user_id: str,
                             within_hours: float) -> Optional[dict]:
        """该买家最近一条**仍在承接窗口内**的已投递触达;没有则 None。

        为什么是"反查"而不是"存一份会话上下文":触达投递走的是
        `_append_agent_reply`——把消息追加进买家自己的客服会话。买家下一轮回复时,
        需要的全部信息(商机类型、关联订单、券码)都已经在 `outreach_drafts` 那一行
        里了,再复制一份到会话状态里只会多出一个会漂移的副本。

        `within_hours` 是**承接窗口**,不是频次下限(那是
        `outreach_min_interval_hours`,两个数各管一件事):过了这个窗口,买家这一轮
        大概率是新问题而不是对那条触达的回应,继续把触达情境注进 prompt 会让客服
        莫名其妙地提起一件顾客早就忘了的事。

        时间比较用字符串(与 `last_outreach_sent_at` 同一理由:两边都是
        `%Y-%m-%d %H:%M:%S`,字典序等于时间序)。
        """
        uid = (user_id or "").strip()
        if not uid or float(within_hours) <= 0:
            return None
        from datetime import datetime, timedelta
        earliest = (datetime.now() - timedelta(hours=float(within_hours))
                    ).strftime("%Y-%m-%d %H:%M:%S")
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM outreach_drafts WHERE user_id = ? "
                "AND status = 'sent' AND sent_at IS NOT NULL AND sent_at > ? "
                "ORDER BY sent_at DESC LIMIT 1", (uid, earliest)).fetchone()
            if not row:
                return None
            item = dict(row)
            # offer 是 JSON 文本列;坏 JSON 退成空 dict 而不是抛——这条读在买家
            # 会话热路径上,一条脏数据不该让买家这一轮失败。
            try:
                item["offer"] = json.loads(item["offer"]) if item.get("offer") else {}
            except (json.JSONDecodeError, TypeError):
                item["offer"] = {}
            return item
        finally:
            conn.close()

    def last_outreach_sent_at(self, user_id: str) -> Optional[str]:
        """该买家最近一条**已投递**营销消息的时间;从未发过返回 None。

        只认 `status='sent'`。draft/approved 都不算——只有真的投递出去的消息才
        构成"刚被打扰过"这个事实,而 approved 有可能投递失败后被退回 draft
        (见 `revert_outreach_to_pending`),把它算进来会让一次失败的投递白白
        冻结这个买家一小时。

        用 MAX() 而不是 ORDER BY ... LIMIT 1:`sent_at` 上没有索引,两种写法都
        是全表扫这个用户的行,MAX 少一次排序,且不依赖行序。
        """
        uid = (user_id or "").strip()
        if not uid:
            return None
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT MAX(sent_at) AS t FROM outreach_drafts "
                "WHERE user_id = ? AND status = 'sent' AND sent_at IS NOT NULL",
                (uid,)).fetchone()
            return (dict(row).get("t") or None) if row else None
        finally:
            conn.close()

    def add_promotion_pause(self, subject: str, until: str, kind: str = "",
                            reason: str = "", correlation_id: str = "") -> int:
        """写一条商品级推广静默记录,返回新行 id。

        不做"同商品已有静默就跳过"的去重:一条链一条记录,重复的静默是**幂等
        的**(生效期取最晚的那条,见 `active_promotion_pauses`),而合并写入会
        丢掉"这次是哪条链要求静默的"这个回溯线索。表本身极小(商品数 ×
        异常次数),不值得为省几行牺牲可追溯性。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO promotion_pauses "
                    "(subject, kind, reason, correlation_id, created_at, until) "
                    "VALUES (?, ?, ?, ?, ?, ?)"),
                (str(subject or ""), str(kind or ""), str(reason or ""),
                 str(correlation_id or ""), self._now(), str(until)))
            new_id = cur.fetchone()[0]
            conn.commit()
            return int(new_id)
        finally:
            conn.close()

    def active_promotion_pauses(self, now: Optional[str] = None) -> list[dict]:
        """当前仍生效(until > now)的静默记录,晚到期的在前。

        **只按时间过滤,不在 SQL 里按商品过滤**:静默记录的 subject 来自
        `order_items.sku`(如 `HMDP-1`),而调用方手里的商品标识可能是别的渠道
        格式(hmdp product.id `1`)。这个仓库已经在 `render_buyer_hints` 上栽过
        一次同样的跤——裸字符串相等永远不成立,而且**不报错、只是永远匹配不上**。
        所以商品比较交给调用方用 `product_ref.same_item` 归一后做,SQL 这层只
        负责"还没过期"这一个确定性条件。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM promotion_pauses WHERE until > ? "
                "ORDER BY until DESC", (now or self._now(),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_outreach_sent(self, draft_id: int) -> bool:
        """标记已发送。只对 approved 生效,保证"批准过"才可能"已发送"。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status = 'sent', sent_at = ? "
                "WHERE id = ? AND status = 'approved'", (self._now(), draft_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def revert_outreach_to_pending(self, draft_id: int) -> bool:
        """投递失败时把草稿从 approved 退回 draft,清空审批痕迹,可重新批准。

        **只对 approved 状态生效**(与 review_outreach_draft / mark_outreach_sent
        同款条件更新):调用方在拿到这次投递失败之前,刚把这条草稿从 draft
        条件更新为 approved 并认领了"本次处理权",所以这里预期的前置状态必是
        approved——用条件更新而不是无条件 UPDATE,是为了让"退回没有真的生效"
        这件事本身可判定(rowcount==0),不装作退回一定成功。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status = 'draft', reviewed_by = NULL, "
                "reviewed_at = NULL WHERE id = ? AND status = 'approved'",
                (draft_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---------- 触达转化归因(N3:发送记基线,到期判定有没有推进) ----------
    def set_outreach_baseline(self, draft_id: int, status_at_send: str) -> bool:
        """记基线:发送那一刻目标订单的状态(无关联订单的商机传空串)。

        调用方(approve_draft)只应在消息**真正投递成功**之后调用一次——一条
        被 revert 回 draft 的草稿从未真正发出,不该带任何基线,这条规矩由
        调用时机保证,而不是这里加状态守卫:草稿此刻是否已经是 sent 不是本方法
        关心的事,它只管"把这个值写进这一行"。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET status_at_send = ? WHERE id = ?",
                (status_at_send or "", draft_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def pending_attribution(self, older_than_hours: int, limit: int = 100) -> list[dict]:
        """到期可判定的已发送触达:`status='sent'` 且 `outcome` 仍是 `'pending'`、
        发送时间早于 `older_than_hours` 之前。

        `outcome='pending'` 这个过滤条件本身就是"归因只跑一次"的数据层保证:
        一旦某一行被判过(outcome 变成 converted/no_change),它就再也不会出现
        在这个查询结果里——幂等是数据的属性,不依赖 worker 有没有额外记"这条
        处理过没有"。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM outreach_drafts WHERE status = 'sent' AND outcome = 'pending' "
                "AND sent_at IS NOT NULL AND sent_at <= "
                f"{self.d.now_minus(max(1, int(older_than_hours)), 'hours')} "
                "ORDER BY id ASC LIMIT ?",
                (max(1, int(limit)),)
            ).fetchall()
            return [d for d in (self._draft_from_row(r) for r in rows) if d]
        finally:
            conn.close()

    def set_outreach_outcome(self, draft_id: int, outcome: str) -> bool:
        """写归因判定结果(outcome: converted / no_change)。**只对仍是 pending 的
        行生效**——与 review_outreach_draft/mark_outreach_sent 同一套条件更新
        纪律:第二次对同一行调用(重跑 worker、并发 worker)拿到 rowcount=0,
        `pending_attribution` 从此也查不到它,保证一条草稿只被计入一次统计。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_drafts SET outcome = ?, outcome_checked_at = ? "
                "WHERE id = ? AND outcome = 'pending'",
                (outcome, self._now(), draft_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def outreach_stats(self, window_days: int) -> dict:
        """窗口内触达转化统计:已发送数 / 已转化数 / 转化率。

        分母是**已发送**(status='sent')的草稿数,不区分 outcome 是否已判定——
        还没到判定窗口的草稿也算"发过"，只是还不知道有没有效果，不能从分母里
        悄悄抹掉（否则窗口刚开始的一段时间转化率会被人为拉高）。除零返回 0.0
        而非 None——与 review_stats/emotion_distribution 同口径，消费方是前端
        展示，null 会诱导误读成"没数据"以外的东西。
        """
        conn = self.connect()
        try:
            w = self.d.now_minus(max(1, int(window_days)), "days")
            row = conn.execute(
                f"SELECT COUNT(*) AS sent, "
                f"SUM(CASE WHEN outcome = 'converted' THEN 1 ELSE 0 END) AS converted "
                f"FROM outreach_drafts WHERE status = 'sent' AND sent_at >= {w}"
            ).fetchone()
            sent = int(row["sent"] or 0)
            converted = int(row["converted"] or 0)
            return {
                "window_days": int(window_days),
                "sent": sent,
                "converted": converted,
                "conversion_rate": (converted / sent) if sent else 0.0,
            }
        finally:
            conn.close()

    # ---------- 店铺人格(单店:CHECK (id = 1) 把"单行"写进 schema) ----------
    def get_shop_profile(self) -> dict:
        """读店铺人格。无行(从未设置过)返回空 dict——由调用方(load_profile)
        决定空值时的默认语气,这里不掺入任何默认值。"""
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM shop_profile WHERE id = 1").fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def set_shop_profile(self, fields: dict, updated_by: str) -> None:
        """写店铺人格(单行 upsert,id 恒为 1)。只覆盖调用方传入的字段,
        未传的字段保留旧值(用 COALESCE 落到已有行,首次写入落到默认空值)。"""
        conn = self.connect()
        try:
            existing = conn.execute("SELECT * FROM shop_profile WHERE id = 1").fetchone()
            base = dict(existing) if existing else {}
            shop_name = fields.get("shop_name", base.get("shop_name", ""))
            tone = fields.get("tone", base.get("tone", ""))
            banned_words = fields.get("banned_words", base.get("banned_words", ""))
            conn.execute(
                "INSERT INTO shop_profile (id, shop_name, tone, banned_words, "
                "updated_by, updated_at) VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET shop_name = excluded.shop_name, "
                "tone = excluded.tone, banned_words = excluded.banned_words, "
                "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (shop_name, tone, banned_words, updated_by, self._now()))
            conn.commit()
        finally:
            conn.close()

    # ---------- 情绪信号(N2:按每一轮落库,与 skill_traces 分表——skill_traces
    # 只在本轮加载过 skill 时才有行,而情绪要按每一轮统计,塞进去会让分母失真) ----------
    def record_turn_signal(self, session_id: str, user_id: str, intent: str,
                           emotion: str, emotion_level: int,
                           requires_human: bool) -> None:
        """记录一轮的意图/情绪信号。旁路埋点:调用方(chat.py)负责 fail-soft,
        本方法本身只管写入,不做兜底判断。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO turn_signals (session_id, user_id, intent, emotion, "
                "emotion_level, requires_human, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, intent, emotion, int(emotion_level),
                 1 if requires_human else 0, self._now()),
            )
            conn.commit()
        finally:
            conn.close()

    def list_turn_signals(self, limit: int = 200) -> list[dict]:
        """按 id DESC 取最近若干轮信号(测试/排查用)。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM turn_signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def emotion_distribution(self, window_days: int = 7) -> dict:
        """窗口内情绪分布:三档计数 + 激烈(angry)占比。

        除零返回 0.0 而非 None——与 shop_analytics._rate 同口径,消费方是 LLM,
        null 会诱导模型现编数字。
        """
        conn = self.connect()
        try:
            w = self.d.now_minus(max(1, int(window_days)), "days")
            rows = conn.execute(
                f"SELECT emotion, COUNT(*) AS n FROM turn_signals "
                f"WHERE created_at >= {w} GROUP BY emotion"
            ).fetchall()
            counts = {"neutral": 0, "unhappy": 0, "angry": 0}
            for r in rows:
                if r["emotion"] in counts:
                    counts[r["emotion"]] = int(r["n"])
            total = sum(counts.values())
            angry_rate = (counts["angry"] / total) if total else 0.0
            return {"window_days": int(window_days), "total": total,
                    "counts": counts, "angry_rate": angry_rate}
        finally:
            conn.close()

    # ---------- 评价(N4:买家评已签收订单,参谋只读分析差评) ----------
    def create_review(self, order_id: str, user_id: str, sku: str, rating: int,
                      content: str) -> Optional[int]:
        """买家提交一条评价。返回新建评价 id;不满足条件一律返回 None(不抛)。

        两条硬约束在插入前就拦下,而不是只靠 UNIQUE 约束兜底:
        ①只有该订单**确实是这个买家的**、且状态为 delivered(已签收)才可评——
        没收到货就能评分是假数据,这条不能只靠前端隐藏按钮防,否则一次直接
        调接口就绕过去了;
        ②一个订单的一个 sku 只能评一次,这条交给 `reviews(order_id, sku)` 的
        UNIQUE 约束兜底(并发下唯一可靠的判重方式),命中时捕获
        IntegrityError 同样返回 None——调用方(API 端点)据此给出统一的
        "不能重复评价"中文提示,而不是让 500 冒出去。
        """
        conn = self.connect()
        try:
            order = conn.execute(
                "SELECT user, status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if order is None or order["status"] != "delivered" or order["user"] != user_id:
                return None
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO reviews (order_id, user_id, sku, rating, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)"),
                (order_id, user_id, sku, int(rating), content or "", self._now()),
            )
            new_id = int(cur.fetchone()["id"])   # RETURNING:先取回再 commit
            conn.commit()
            return new_id
        except Exception as exc:  # noqa: BLE001 只吞唯一约束冲突,其余照抛
            if not self.d.is_duplicate_key(exc):
                raise
            return None
        finally:
            conn.close()

    def list_reviews(self, sku: Optional[str] = None, window_days: Optional[int] = None,
                     limit: int = 50) -> list[dict]:
        """按 id DESC 取评价,可选按 sku / 窗口天数过滤。"""
        conn = self.connect()
        try:
            sql = "SELECT * FROM reviews"
            clauses: list[str] = []
            params: list = []
            if sku:
                clauses.append("sku = ?")
                params.append(sku)
            if window_days is not None:
                clauses.append(f"created_at >= {self.d.now_minus(max(1, int(window_days)), 'days')}")
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, int(limit)))
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def review_stats(self, window_days: int) -> dict:
        """窗口内全店评价统计:总数/均分/差评率(rating<=2)。除零返回 0.0
        而非 None——与 shop_analytics._rate 同口径,消费方是 LLM 与前端展示,
        null 会诱导模型现编数字。

        **决策(review finding 2)**:这里只按 `reviews` 表统计,不 JOIN
        `orders` 排除后来被退款(refund_processing/refunded)的订单——一条
        评价一旦写下,就是买家在收货那一刻的真实体验,后续退款是另一个独立
        事件,不会让"当时东西有问题"这件事变成没发生过。反过来想:如果退款
        能让对应的评价从差评率里消失,店主就有了一个现成的洗白手段——先让
        买家写下差评,再批一笔退款把这条差评从统计里"退掉",差评率和它驱动
        的告警(`anomaly_scan` 的 bad_review_rate_high)反而失去意义。所以
        这是刻意保留、不是遗漏:退款只改 `orders.status`,`reviews` 表的行
        永远不因订单后续状态而增减或被过滤。`tests/test_reviews.py::
        test_refunded_order_review_still_counts_toward_bad_rate` 钉住这条
        行为,后续若要改成排除,必须显式改这条测试,不能悄悄漂移。
        """
        conn = self.connect()
        try:
            w = self.d.now_minus(max(1, int(window_days)), "days")
            row = conn.execute(
                f"SELECT COUNT(*) AS total, COALESCE(AVG(rating), 0) AS avg_rating, "
                f"SUM(CASE WHEN rating <= 2 THEN 1 ELSE 0 END) AS bad "
                f"FROM reviews WHERE created_at >= {w}"
            ).fetchone()
            total = int(row["total"] or 0)
            bad = int(row["bad"] or 0)
            return {
                "window_days": int(window_days),
                "total": total,
                "avg_rating": float(row["avg_rating"] or 0.0) if total else 0.0,
                "bad_rate": (bad / total) if total else 0.0,
            }
        finally:
            conn.close()

    def reviewable_items(self, user_id: str) -> list[dict]:
        """该买家当前可评价的订单项:已签收(delivered) 且该 (order_id, sku)
        尚未评过价。没收到货就能评分是假数据,故只从 delivered 订单里挑;
        已评过的 (order_id, sku) 用 NOT EXISTS 排除,评完即从列表消失。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT o.order_id AS order_id, oi.sku AS sku, oi.name AS name, "
                "       o.delivered_at AS delivered_at "
                "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                "WHERE o.user = ? AND o.status = 'delivered' "
                "  AND NOT EXISTS (SELECT 1 FROM reviews r "
                "                  WHERE r.order_id = o.order_id AND r.sku = oi.sku) "
                "ORDER BY o.created_at DESC, oi.id",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def reviewed_pairs(self, user_id: str) -> set:
        """该买家已评过的 (order_id, sku) 集合。

        给"订单在 hmdp、评价在本地"这条组合路径用:hmdp 没有评价这个概念
        (它只有 blog 评论),所以评价合理地留在 agent 侧;但**哪些订单可评**
        必须由买家实际看到的那份订单决定,而不是由本地订单表决定——否则
        demo 买家的订单全在 hmdp,`reviewable_items` 永远返回空,整个评价功能
        对默认模式不可达。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT order_id, sku FROM reviews WHERE user_id = ?", (user_id,)
            ).fetchall()
            return {(r["order_id"], r["sku"]) for r in rows}
        finally:
            conn.close()

    # ---------- 购物车(N5:真实购物车,供弃单挽回商机的数据地基) ----------
    def add_to_cart(self, user_id: str, sku: str, quantity: int) -> None:
        """加购:同一用户同一 sku 若已有 active 行则累加数量并刷新 added_at,
        否则新插入一行。`idx_carts_active_unique` 这条部分索引只约束 active
        行,允许同一 sku 在"已转化/已弃单"的历史行之外再开一条新的 active 行
        (重新加回购物车)。加购不是"设置为某个数量"而是"再加这么多",
        与真实购物车"多次点加购会累加"的直觉一致。"""
        conn = self.connect()
        try:
            now = self._now()
            qty = max(1, int(quantity or 1))
            row = conn.execute(
                "SELECT id FROM carts WHERE user_id = ? AND sku = ? AND status = 'active'",
                (user_id, sku)).fetchone()
            if row:
                conn.execute(
                    "UPDATE carts SET quantity = quantity + ?, added_at = ? WHERE id = ?",
                    (qty, now, row["id"]))
            else:
                conn.execute(
                    "INSERT INTO carts (user_id, sku, quantity, added_at, status) "
                    "VALUES (?, ?, ?, ?, 'active')",
                    (user_id, sku, qty, now))
            conn.commit()
        finally:
            conn.close()

    def list_cart(self, user_id: str) -> list[dict]:
        """该用户当前活跃(active)的购物车行,按最近加购时间倒序。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, user_id, sku, quantity, added_at, status FROM carts "
                "WHERE user_id = ? AND status = 'active' ORDER BY added_at DESC",
                (user_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def remove_from_cart(self, user_id: str, sku: str) -> bool:
        """移除某个 sku 的 active 购物车行(物理删除,不是状态流转——它从未
        转化成订单,留着历史行没有意义)。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "DELETE FROM carts WHERE user_id = ? AND sku = ? AND status = 'active'",
                (user_id, sku))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_cart_converted(self, user_id: str, skus: list[str]) -> None:
        """下单成功后,把该用户这些 sku 的 active 购物车行标记为 converted。

        只改状态、不物理删除:一来这些行从此不再出现在"我的购物车"/弃单商机
        (二者都只看 status='active'),二来保留历史行,以后想统计"购物车转化率"
        才有数据可看。"""
        skus = [s for s in (skus or []) if s]
        if not skus:
            return
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in skus)
            conn.execute(
                f"UPDATE carts SET status = 'converted' "
                f"WHERE user_id = ? AND status = 'active' AND sku IN ({placeholders})",
                (user_id, *skus))
            conn.commit()
        finally:
            conn.close()

    def set_cart_quantity(self, user_id: str, sku: str, quantity: int) -> bool:
        """把某个 sku 的购物车数量**设置**为一个具体值(而不是累加)——与
        `add_to_cart` 的累加语义互补,一个负责"加",一个负责"设为",避免同一
        件事有两个隐式入口。

        两条硬约束:
        ① quantity 必须是正整数,非正数直接拒绝(返回 False),不做静默删除;
        "设为 0"与"移除"是两件事,移除请显式调用 `remove_from_cart`。
        ② 只对该用户名下已存在的 active 行生效——sku 不在购物车里不算"设置"
        的对象,不会隐式创建一行(新增走 `add_to_cart` 那条唯一路径)。"""
        if quantity is None or int(quantity) <= 0:
            return False
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE carts SET quantity = ?, added_at = ? "
                "WHERE user_id = ? AND sku = ? AND status = 'active'",
                (int(quantity), self._now(), user_id, sku))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def abandoned_carts(self, hours: int = 48, limit: int = 100) -> list[dict]:
        """超过阈值仍处于 active 的购物车行——"加购未下单"商机的数据来源。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                f"SELECT id, user_id, sku, quantity, added_at FROM carts "
                f"WHERE status = 'active' "
                f"  AND added_at <= {self.d.now_minus(max(1, int(hours)), 'hours')} "
                f"ORDER BY added_at ASC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- 真实未支付态(N5):unpaid → pending 的条件更新 ----------
    def pay_order(self, order_id: str, user_id: str) -> bool:
        """买家为自己的未支付订单完成支付。**只能由订单所有者调用**,且只对
        当前仍是 unpaid 的行生效——这条条件更新同时挡住两件事:
        ①付别人的单(user 不匹配,rowcount=0);②重复支付(已经是 pending 之后
        再调,status 条件不满足,rowcount=0),与 review_outreach_draft/
        mark_outreach_sent 等既有条件更新同一套幂等纪律。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE orders SET status = 'pending' "
                "WHERE order_id = ? AND user = ? AND status = 'unpaid'",
                (order_id, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---------- 优惠券发放(N6:发券碰真金白银且不可撤销,只能由审批端点在
    # 人工点过批准之后调用——见 app.agent.coupons.grants.issue_for_draft) ----------
    def grant_coupon(self, code: str, user_id: str, draft_id: Optional[int],
                     reason: str, granted_by: str) -> Optional[int]:
        """发放一张优惠券,返回新建发放记录 id。

        同一优惠券对同一买家只能发一次,交给 `UNIQUE(code, user_id)` 约束
        兜底判重(与 create_review 同一套姿态:并发下唯一可靠的判重方式是
        让数据库约束顶上去,而不是先 SELECT 再 INSERT)。命中重复捕获
        IntegrityError 返回 None,不抛——调用方(issue_for_draft)据此把
        "重复发放"当成一次可判定的失败,而不是让异常冒泡成裸 500。
        """
        conn = self.connect()
        try:
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO coupon_grants (code, user_id, draft_id, reason, "
                    "granted_by, created_at) VALUES (?, ?, ?, ?, ?, ?)"),
                (code, user_id, draft_id, reason, granted_by, self._now()))
            new_id = int(cur.fetchone()["id"])   # RETURNING:先取回再 commit
            conn.commit()
            return new_id
        except Exception as exc:  # noqa: BLE001 只吞唯一约束冲突,其余照抛
            if not self.d.is_duplicate_key(exc):
                raise
            return None
        finally:
            conn.close()

    def list_user_grants(self, user_id: str) -> list[dict]:
        """该用户收到过的所有券发放记录(按 id DESC),供 query_coupons 的
        `granted` 字段与审批端点/审计使用。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM coupon_grants WHERE user_id = ? ORDER BY id DESC",
                (user_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_grant(self, code: str, user_id: str) -> Optional[dict]:
        """按 `(code, user_id)` 精确取那一行发放记录(`UNIQUE(code, user_id)`
        保证至多一行)。

        `grant_coupon` 命中约束返回 `None` 后,调用方(`issue_for_draft`)靠
        这一行里的 `draft_id` 区分"这是同一条草稿上一次已经真的发放过,这次
        重试只是撞见了自己",还是"这张券已经被别的草稿发给了这个买家,是
        真正的重复"——前者要放行继续投递,后者仍须拒绝。
        """
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM coupon_grants WHERE code = ? AND user_id = ?",
                (code, user_id)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ---------- 跟进序列(N7:持续沟通=序列自动推进,不是自动发送) ----------
    # 五条终止条件里,只有"同一买家同一 kind 只能有一条 active 链"这一条由
    # 本层(数据库唯一约束)兜底——其它四条(商机消失/上次触达已转化/仲裁拒绝/
    # 达到步数上限)交给 app.multi_agent.followup 判定,那里才看得到业务口径。
    def start_followup(self, user_id: str, kind: str, correlation_id: str,
                       max_steps: int = 3, interval_hours: int = 48) -> Optional[int]:
        """开一条新的跟进链,首次到期时间为当下 + interval_hours。

        同一买家同一 kind 只能有一条 active 链,交给 `idx_followups_active_unique`
        这条部分索引兜底判重(与 create_review/grant_coupon 同一套姿态:并发下
        唯一可靠的判重方式是让约束顶上去,而不是先查后插),命中冲突捕获
        IntegrityError 返回 None,不抛。"""
        conn = self.connect()
        try:
            now = self._now()
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO outreach_followups (user_id, kind, correlation_id, step, "
                    "max_steps, next_touch_at, status, created_at, updated_at) "
                    f"VALUES (?, ?, ?, 1, ?, {self.d.now_plus_param('hours')}, "
                    "'active', ?, ?)"),
                (user_id, kind, correlation_id, max(1, int(max_steps)),
                 max(0, int(interval_hours)), now, now))
            new_id = int(cur.fetchone()["id"])   # RETURNING:先取回再 commit
            conn.commit()
            return new_id
        except Exception as exc:  # noqa: BLE001 只吞唯一约束冲突,其余照抛
            if not self.d.is_duplicate_key(exc):
                raise
            return None
        finally:
            conn.close()

    def due_followups(self, limit: int = 20) -> list[dict]:
        """到期可推进的 active 跟进链(next_touch_at 已过)。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM outreach_followups WHERE status = 'active' "
                f"AND next_touch_at <= {self.d.now()} "
                "ORDER BY next_touch_at ASC LIMIT ?", (max(1, int(limit)),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def advance_followup(self, fid: int, interval_hours: int = 48) -> bool:
        """推进一步:step + 1;超过 max_steps 则收尾为 done,否则重排下次触达
        时间到未来。**只对 active 状态生效**(与 review_outreach_draft 等既有
        条件更新同一套幂等纪律)。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT step, max_steps FROM outreach_followups "
                "WHERE id = ? AND status = 'active'", (fid,)).fetchone()
            if row is None:
                return False
            new_step = int(row["step"]) + 1
            now = self._now()
            if new_step > int(row["max_steps"]):
                conn.execute(
                    "UPDATE outreach_followups SET step = ?, status = 'done', "
                    "updated_at = ? WHERE id = ?", (new_step, now, fid))
            else:
                conn.execute(
                    "UPDATE outreach_followups SET step = ?, "
                    f"next_touch_at = {self.d.now_plus_param('hours')}, "
                    "updated_at = ? WHERE id = ?",
                    (new_step, max(0, int(interval_hours)), now, fid))
            conn.commit()
            return True
        finally:
            conn.close()

    def stop_followup(self, fid: int, reason: str) -> bool:
        """终止一条跟进链并记下终止原因。**只对 active 状态生效**——链一旦
        done/stopped 就不该再被第二次判定改写理由。stop_reason 必须落库,
        否则一条链停了但店主看不出为什么,等同于一个静默的 bug。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE outreach_followups SET status = 'stopped', stop_reason = ?, "
                "updated_at = ? WHERE id = ? AND status = 'active'",
                (reason, self._now(), fid))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def active_followup(self, user_id: str, kind: str) -> Optional[dict]:
        """该买家该 kind 当前的 active 链(至多一条,由唯一约束保证)。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM outreach_followups WHERE user_id = ? AND kind = ? "
                "AND status = 'active' ORDER BY id DESC LIMIT 1",
                (user_id, kind)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_followup(self, fid: int) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM outreach_followups WHERE id = ?", (fid,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_followups(self, status: Optional[str] = None,
                       limit: int = 100) -> list[dict]:
        """列跟进链,供管理端「跟进链」小节展示。默认不筛状态——已终止的链
        也要能看到(带着终止原因),否则店主看不出"为什么不再跟了"。"""
        sql = "SELECT * FROM outreach_followups"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- 店主通知 CRUD ----------

    def add_notification(self, kind: str, title: str, summary: str = "",
                         severity: str = "info",
                         suggested_question: str = "") -> int:
        """写入一条店主通知。返回新行 ID。

        fail-soft 由调用方(collab.handle_signal)保证:写入失败只记 warning,
        不打断诊断主流程。这里抛异常即可,不吞。
        """
        conn = self.connect()
        try:
            # 用方言层的 RETURNING 而不是 `cur.lastrowid`:后者是 sqlite3 驱动
            # **特有**的,psycopg 没有 —— PG 后端上这里会直接抛。
            # `tests/test_db_dialect.py::test_driver_specific_apis_do_not_leak`
            # 就是拦这个的(仓库里另外两处 INSERT 取 id 也都栽过同一跤,
            # 见 bargain_deals / agent_events 两处的注释)。
            cur = conn.execute(
                self.d.returning_id(
                    "INSERT INTO seller_notifications "
                    "(kind, title, summary, severity, suggested_question, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)"),
                (kind, title, summary, severity, suggested_question, self._now()),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
        finally:
            conn.close()

    def list_notifications(self, unread_only: bool = False,
                           limit: int = 20) -> list[dict]:
        """列通知,默认按时间倒序。unread_only=True 时只返回未读。"""
        sql = "SELECT * FROM seller_notifications"
        params: list = []
        if unread_only:
            sql += " WHERE read = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_unread_notifications(self) -> int:
        """未读通知计数(前端角标用)。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM seller_notifications WHERE read = 0"
            ).fetchone()
            return int(row["n"] or 0)
        finally:
            conn.close()

    def mark_notification_read(self, nid: int) -> bool:
        """标记单条通知已读。返回是否真的改了(行存在且之前未读)。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE seller_notifications SET read = 1 WHERE id = ? AND read = 0",
                (nid,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_all_notifications_read(self) -> int:
        """一键全部已读。返回实际更新的行数。"""
        conn = self.connect()
        try:
            cur = conn.execute(
                "UPDATE seller_notifications SET read = 1 WHERE read = 0"
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
