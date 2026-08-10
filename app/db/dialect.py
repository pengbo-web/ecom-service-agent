"""SQL 方言层:把与具体数据库绑定的东西收敛到一处。

**这一步不引入 PostgreSQL,今天就该合并。** 收敛本身有三项独立于迁移的价值:

1. **时钟只有一处。** `datetime('now', '-N hours')` 此前散在 24 个地方
   (database.py 9 处、growth.py 13 处、shop_analytics.py 还自带一份重复的
   `_window_clause`)。而本项目有一个已知的时钟不一致:`Database._now()` 写的是
   **本地时间**,SQL 里的 `datetime('now')` 是 **UTC**——一条刚好卡在阈值边缘的
   记录会出现"WHERE 认为已滞留 25h、Python 侧算出 -7h"这种自相矛盾。散在 24 处
   时这个问题无从下手;收敛之后它变成"改这一个文件"。
2. **注入面收敛。** 这些片段的时长值是**插值**进 SQL 的(它们是结构而非参数,
   没法用占位符)。今天每个调用点各自写 `max(1, int(days))` 来保证安全——靠的是
   24 次约定,漏一次就是一个可注入点。收进来之后强制转换是**结构性**的,想漏也漏不掉。
3. **驱动特有 API 不再外泄。** `cur.lastrowid` 与 `sqlite3.IntegrityError` 是
   sqlite3 驱动的东西;psycopg 两者都没有。换成 `RETURNING` + 项目自己的
   `DuplicateKey`,调用方从此不认识任何具体驱动。

**刻意没做:占位符统一。** 245 个 `?` 改成 `%s` 是纯机械替换,在 PG 真正存在之前
**收益为零且无法验证**,而 245 处改动的回归风险是实的。正确时机是写
`PostgresDatabase` 时在 `execute` 边界做一次转换——那是**一处**,且有真库可测。

---

**用法约束(重要)**:本模块的函数返回的是**SQL 片段**,直接拼进语句。所以:
- 传进来的数值一律 `int()` 强制转换,不接受任何字符串;
- 列名参数(`col`)只能来自代码里的字面量,**绝不允许**来自请求或库里的数据。
  这一点靠调用点自律,本模块无法校验——但全部调用点都在本仓库内,可审计。
"""

from __future__ import annotations

import re


class DuplicateKey(Exception):
    """唯一约束冲突。

    存在的理由:调用方需要区分"撞了唯一约束"(业务上是**正常**的去重生效,返回
    None)与"真的出错了"。此前这个判断直接 `except sqlite3.IntegrityError`,
    把调用方绑死在 sqlite3 驱动上;psycopg 抛的是 `psycopg.errors.UniqueViolation`,
    换库时每个调用点都要改。

    注意它只包唯一约束冲突,不包外键、非空等其它完整性错误——那些是真 bug,
    应该继续往上抛,不该被"去重生效"的分支静默吃掉。
    """


def is_duplicate_key(exc: BaseException) -> bool:
    """判断一个驱动异常是不是唯一约束冲突。

    SQLite 的 `IntegrityError` 涵盖唯一约束、外键、非空、CHECK 等多种情况,
    只能靠消息文本区分——这是 sqlite3 驱动的局限,不是这里想偷懒。判据取
    "UNIQUE constraint failed",与 SQLite 的实际输出一致。

    换 PG 时这里加一个分支即可:`isinstance(exc, psycopg.errors.UniqueViolation)`,
    那边有明确的 SQLSTATE(23505),不需要读消息。
    """
    import sqlite3

    if isinstance(exc, sqlite3.IntegrityError):
        return "unique constraint failed" in str(exc).lower()
    return False


# ---------------------------------------------------------------------------
# 时间表达式
# ---------------------------------------------------------------------------
#
# 全部基于**数据库自己的时钟**(SQLite 的 `datetime('now')` = UTC),而不是
# Python 侧的 `datetime.now()`。这不是偏好:筛选行的 WHERE 与计算滞留时长的
# 表达式必须用同一个时钟,否则一条卡在阈值边缘的记录会被两边判成不同的结果。


def now() -> str:
    """当前时刻。"""
    return "datetime('now')"


def now_minus(amount: int, unit: str = "hours") -> str:
    """当前时刻往前 N 个单位(hours / days)。用于"多久之前"的窗口下界。"""
    return f"datetime('now', '-{int(amount)} {unit}')"


def now_plus_param(unit: str = "hours") -> str:
    """当前时刻往后 N 个单位,**N 由占位符传入**(值来自变量而非常量时用它)。

    返回的片段里含一个 `?`,调用方要在参数元组里对应传值。SQLite 用字符串拼接
    的方式支持这种"参数化的 interval",PG 则写成 `now() + (%s || ' hours')::interval`
    ——两边都能表达,但语法不同,所以值得单独占一个函数。
    """
    return f"datetime('now', '+' || ? || ' {unit}')"


def elapsed_hours(col: str) -> str:
    """某个时间列到现在经过了多少小时(保留两位小数)。

    `col` 必须是代码里的字面量列名(见模块顶部的用法约束)。
    """
    return f"ROUND((julianday('now') - julianday({col})) * 24.0, 2)"


# ---------------------------------------------------------------------------
# 自增主键取回
# ---------------------------------------------------------------------------


def returning_id(sql: str) -> str:
    """给 INSERT 语句加上 `RETURNING id`。

    取代 `cur.lastrowid`:后者是 sqlite3 驱动特有的,psycopg 没有。SQLite 从
    3.35 起支持 RETURNING(本项目实测环境 3.45.3),两边语法一致。

    只做字符串拼接、不解析 SQL:调用方保证传进来的是一条 INSERT,且主键列名为
    `id`。表里主键不叫 `id` 的(如 `products.product_id`)不要用这个函数。
    """
    return sql.rstrip().rstrip(";") + " RETURNING id"


# ---------------------------------------------------------------------------
# PG3:方言对象化
# ---------------------------------------------------------------------------
#
# 上面那些模块级函数只产 SQLite 片段。PG3 要让**两套后端在同一进程里共存**
# (双后端参数化测试是唯一能证明"语义一致"的手段,而它必须同时持有两个
# Database 实例),所以不能用"模块级 set_backend()"那种全局可变状态——那会让
# 两套后端在测试里互相打断,而且症状是随执行顺序漂移的。
#
# 改成:每个后端一个方言实例,`Database` 持有 `self.d`。模块级函数保留不动
# (`SqliteDialect` 直接委托给它们),因为 growth.py / shop_analytics.py 还在直接
# 用它们——那两个模块属于 PG3 的后续分组,现在改会把改动面摊太大。


class SqliteDialect:
    """SQLite 方言。全部委托给上面的模块级函数,保证唯一口径。"""

    name = "sqlite"
    #: 占位符风格。`Database` 的连接包装按它决定要不要把 `?` 转成 `%s`。
    paramstyle = "qmark"

    now = staticmethod(now)
    now_minus = staticmethod(now_minus)
    now_plus_param = staticmethod(now_plus_param)
    elapsed_hours = staticmethod(elapsed_hours)
    returning_id = staticmethod(returning_id)

    @staticmethod
    def group_concat(col: str) -> str:
        """把一组值拼成逗号分隔的串。

        实测唯一用处是 `list_event_chains` 拼"这条链碰过哪些 Agent"。
        SQLite 叫 `group_concat`,PG 叫 `string_agg` 且**必须显式给分隔符**。
        """
        return f"group_concat({col})"

    @staticmethod
    def count_if(cond: str) -> str:
        """按条件计数。

        SQLite 把布尔当 0/1,所以 `SUM(status='failed')` 直接能用;PG 严格区分
        boolean 与数值,同一句会报 `function sum(boolean) does not exist`。
        统一成这个函数,两边都写得出。
        """
        return f"SUM({cond})"

    #: 自增主键的列定义。建表语句里用它替换硬编码的 AUTOINCREMENT。
    autoincrement_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    @staticmethod
    def is_duplicate_key(exc: BaseException) -> bool:
        return is_duplicate_key(exc)


class PostgresDialect:
    """PostgreSQL 方言。

    几处**不是机械替换**的地方,单独说明:

    - `now()`:PG 的 `now()` 返回 timestamptz。项目里所有时间列在 SQLite 下是
      TEXT(`'YYYY-MM-DD HH:MM:SS'`),而上层(前端、`list_event_chains` 的调用方、
      `parseTs`)都按那个字符串格式处理。所以这里统一用
      `to_char(..., 'YYYY-MM-DD HH24:MI:SS')` 产出**同样形状的字符串**,
      让"换了后端所以时间字段格式变了"这件事不漏到上层。
    - `elapsed_hours`:SQLite 用 `julianday` 差值 × 24;PG 用 `EXTRACT(EPOCH FROM
      age)` / 3600。两边都保留两位小数,保证阈值比较行为一致。
    - `now_plus_param`:SQLite 靠字符串拼接支持"参数化 interval";PG 要写成
      `now() + make_interval(...)`,且**参数位置不同**——SQLite 版把 `?` 放在
      字符串拼接里,PG 版放进函数调用。片段形状不同,所以它值得单独占一个函数
      (PG1 的注释已经预判到这一点)。
    """

    name = "pg"
    paramstyle = "format"          # psycopg 用 %s

    autoincrement_pk = "bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

    #: 时间列在 PG 下仍存成文本,**刻意不改成 timestamptz**。
    #: 理由:PG3 的约束是"行为零变化、不趁机改建模"。时间列一旦变类型,
    #: 249 处占位符之外还要审所有做字符串比较的 WHERE(如 `created_at >= ?`
    #: 传进来的是 Python 侧格式化的字符串),那是另一个量级的改动面。
    #: 等 PG3 全绿之后再单独做类型收敛(那时有双后端测试兜着)。
    timestamp_type = "text"

    @staticmethod
    def now() -> str:
        return "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"

    @staticmethod
    def now_minus(amount: int, unit: str = "hours") -> str:
        return (f"to_char(now() - make_interval({unit} => {int(amount)}), "
                "'YYYY-MM-DD HH24:MI:SS')")

    @staticmethod
    def now_plus_param(unit: str = "hours") -> str:
        # 片段里含一个占位符,与 SQLite 版契约一致(调用方在参数元组里对应传值)
        return (f"to_char(now() + make_interval({unit} => ?), "
                "'YYYY-MM-DD HH24:MI:SS')")

    @staticmethod
    def elapsed_hours(col: str) -> str:
        return (f"ROUND(EXTRACT(EPOCH FROM (now() - {col}::timestamp)) "
                "/ 3600.0, 2)")

    @staticmethod
    def group_concat(col: str) -> str:
        """PG 的 `string_agg` **必须显式给分隔符**(SQLite 的 group_concat 默认逗号)。

        分隔符必须与 SQLite 一致:调用方(`list_event_chains`)按 `,` split,
        换个分隔符会让 agents 列表整条变成一个字符串,而那不会报错——只会让
        协作链上的 Agent 顺序显示成一坨。
        """
        return f"string_agg({col}, ',')"

    @staticmethod
    def count_if(cond: str) -> str:
        """PG 里布尔不能直接 SUM,要显式转成 0/1。

        实测报错:`function sum(boolean) does not exist`。这类差异**不会在建表时
        暴露**,只在跑到那条查询时才炸——所以它值得单独占一个方言函数,而不是在
        调用点各写一遍 CASE。
        """
        return f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"

    @staticmethod
    def returning_id(sql: str) -> str:
        return returning_id(sql)

    @staticmethod
    def is_duplicate_key(exc: BaseException) -> bool:
        """PG 有明确的 SQLSTATE(23505),不需要读消息文本。

        仍然兼容 SQLite 的判据:同一个 `Database` 类两套后端共用异常处理分支,
        判据函数必须对两边都成立,否则换后端时每个 `except` 都要改。
        """
        try:
            from psycopg import errors as pg_errors

            if isinstance(exc, pg_errors.UniqueViolation):
                return True
        except ImportError:
            pass
        return is_duplicate_key(exc)


#: 项目里用作列名、但在 PG 里是**保留字**的词。
#:
#: 实测只有一个:`orders.user`。裸写会 `syntax error at or near "user"`。
#: 不改列名(它在项目里约 130 处),改成在 PG 侧加双引号——`"user"` 在 PG 与
#: SQLite 里都是合法标识符,所以两套后端能共用同一份 SQL。
#:
#: 新增表时若又用了保留字,加进这个集合即可;但更好的做法是别用。
RESERVED_COLUMNS = ("user",)


def quote_reserved(sql: str, d) -> str:
    """给 DML 里的保留字列名加引号(PG 专用,SQLite 原样返回)。

    只匹配**独立单词**且**不在字符串字面量里**的出现。`user_id`、`users`、
    `ctx_user_id` 这些不该被碰——`\\b` 边界能挡住它们(`user_id` 里 `user` 后面
    紧跟 `_`,不是词边界)。

    注意它**不处理** `current_user` / `session_user` 这类 PG 内建函数——项目里
    没有用到,若将来用了要在这里排除,否则会被加引号变成列引用。
    """
    if getattr(d, "name", "sqlite") != "pg":
        return sql

    literals: list[str] = []

    def _stash(m: re.Match) -> str:
        literals.append(m.group(0))
        return f"\x00Q{len(literals) - 1}\x00"

    body = re.sub(r"'(?:[^']|'')*'", _stash, sql)
    for word in RESERVED_COLUMNS:
        # 已经带引号的不再加(幂等):否则重复调用会产出 ""user""
        body = re.sub(rf'(?<!")\b{word}\b(?!")', f'"{word}"', body)
    for i, lit in enumerate(literals):
        body = body.replace(f"\x00Q{i}\x00", lit)
    return body


def translate_schema(script: str, d) -> str:
    """把写给 SQLite 的建表脚本翻成目标方言。

    **为什么做后处理而不是把 22 张表逐个改成 f-string**:那意味着在一段 300 行、
    结构高度重复的 DDL 里插 30 多处 `{...}`,可读性会塌掉,而且每次加表都要记得
    用 f-string——漏一次就是一个只在 PG 上炸的错误。后处理是**一处**,而且这里
    的替换规则可以逐条写清、逐条测试。

    SQLite 后端**原样返回**(连字符串都不动),保证既有行为逐字节不变。

    翻译规则(只有这些,不做通用 SQL 改写):

    1. `INTEGER PRIMARY KEY AUTOINCREMENT` → PG 的 identity 列;
    2. SQLite 的 `INTEGER` / `REAL` 列类型在 PG 下换成 `bigint` / `double precision`
       ——PG 没有 `REAL` 之外的别名问题,但 `INTEGER` 在 PG 是 4 字节,而项目里
       用它存过毫秒时间戳,统一放宽到 bigint 更安全;
    3. `WITHOUT ROWID`、`PRAGMA` 等 SQLite 专有子句删掉;
    4. 部分唯一索引(`CREATE UNIQUE INDEX ... WHERE ...`)**不动**——PG 原生支持,
       这是 PG1 就核过的两处"不用改"之一。
    """
    if getattr(d, "name", "sqlite") != "pg":
        return script

    out = script
    # ① 自增主键
    out = out.replace("INTEGER PRIMARY KEY AUTOINCREMENT", d.autoincrement_pk)
    # ①b 保留字列名。实测只有一个:`orders.user`(`user` 是 PG 保留字,
    #     裸写会 `syntax error at or near "user"`)。
    #
    #     **不改列名**:它在项目里出现约 130 处(SQL 与 Python 混杂),改名要动
    #     所有读写路径,而 PG3 的约束是"行为零变化、这是基础设施替换不是重构机会"。
    #     改成加双引号——`"user"` 在 PG 里是合法标识符,而 SQLite 也接受双引号
    #     标识符,所以**两套后端的 SQL 仍可共用一份**。
    #     只处理 DDL 里的列定义;DML 里的 `user` 由 `quote_reserved` 处理。
    for word in RESERVED_COLUMNS:
        out = re.sub(rf"(?m)^(\s+){word}(\s+(?:TEXT|bigint|double precision|BOOLEAN))",
                     rf'\1"{word}"\2', out)
    # ② 列类型。只替换**独立单词**,避免把 `INTEGER PRIMARY KEY`(已在①处理)
    #    或列名里含 integer 的情况误伤。
    out = re.sub(r"\bINTEGER\b(?!\s+PRIMARY)", "bigint", out)
    out = re.sub(r"\bREAL\b", "double precision", out)
    # ③ SQLite 专有子句
    out = re.sub(r"\bWITHOUT\s+ROWID\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"^\s*PRAGMA[^;]*;\s*$", "", out, flags=re.MULTILINE | re.IGNORECASE)
    return out


def get_dialect(backend: str):
    """按后端名取方言实例。未知后端回落 SQLite 并不报错——配置写错不该让数据层
    整体不可用,而 SQLite 是本项目的默认后端。"""
    return PostgresDialect() if (backend or "").lower() == "pg" else SqliteDialect()
