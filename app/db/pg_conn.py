"""PG 连接包装:让写给 SQLite 的 SQL 原样跑在 PostgreSQL 上。

**这是 PG3 的关键一招。** PG1 当时刻意没做占位符统一,理由写得很清楚:

> 245 个 `?` 改成 `%s` 是纯机械替换,在 PG 真正存在之前收益为零且无法验证,
> 而 245 处改动的回归风险是实的。正确时机是写 `PostgresDatabase` 时在
> `execute` 边界做一次转换——那是**一处**,且有真库可测。

本模块就是那"一处"。`Database` 拿到的连接对象暴露与 `sqlite3.Connection` 相同的
最小接口(`execute` / `executescript` / `commit` / `close` / 上下文管理器),内部:

1. 把 `?` 占位符翻成 `%s`——**只翻占位符,不动字符串字面量里的问号**;
2. 让 `fetchone()/fetchall()` 返回可按列名下标访问的行(psycopg 的 `dict_row`
   已经是 dict,而 `sqlite3.Row` 支持 `row["col"]`,两者在调用方看来一致);
3. `executescript` 在 PG 上就是多语句 execute(psycopg3 支持一次发多条)。

**不做 SQL 语义改写。** 方言差异(时间函数、自增主键、PRAGMA)由
`app/db/dialect.py` 的方言对象负责,这里只处理**驱动层**差异。两件事分开是刻意的:
混在一处会让"某条 SQL 为什么在 PG 上不一样"变得无从追查。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: 匹配**不在字符串字面量里**的 `?`。
#:
#: 为什么需要这个而不是 `sql.replace("?", "%s")`:项目里有 SQL 带字符串字面量
#: (如 `WHERE status = 'draft'`),而且 SQLite 版的 `now_plus_param` 产出的片段是
#: `datetime('now', '+' || ? || ' hours')`——那个 `?` 在字面量**之外**、必须翻,
#: 而 `'+'` 里的字符不能碰。粗暴 replace 还会把 `??`(PG 的 jsonb 操作符)弄坏。
#:
#: 手法:先把单引号字符串整段挖走(用占位标记替换),翻完 `?` 再填回去。比写一个
#: 正则一次搞定更容易读,也更容易在出错时定位。
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_PLACEHOLDER_MARK = "\x00STR%d\x00"


def qmark_to_format(sql: str) -> str:
    """把 `?` 占位符翻成 `%s`,保留字符串字面量原样。

    同时把已有的 `%` 转义成 `%%`:psycopg 用 `%` 做占位符前缀,SQL 里原本的
    `%`(如 `LIKE '%foo%'`)必须转义,否则 psycopg 会把它当成畸形占位符报错。
    **顺序很重要**:先挖字面量 → 再转义 `%` → 再翻 `?` → 最后填回字面量。
    如果先翻 `?` 再转义 `%`,刚生成的 `%s` 会被转成 `%%s`。
    """
    literals: list[str] = []

    def _stash(m: re.Match) -> str:
        literals.append(m.group(0))
        return _PLACEHOLDER_MARK % (len(literals) - 1)

    body = _STRING_LITERAL.sub(_stash, sql)
    body = body.replace("%", "%%")          # 字面量之外的 % 需要转义
    body = body.replace("?", "%s")
    for i, lit in enumerate(literals):
        # 字面量内部的 % 同样要转义:它最终也会经过 psycopg 的参数插值
        body = body.replace(_PLACEHOLDER_MARK % i, lit.replace("%", "%%"))
    return body


class _Cursor:
    """薄游标包装:只补齐 `rowcount` 与行访问形态。"""

    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        """psycopg 没有 lastrowid。

        故意**不模拟**它:项目已经统一走 `dialect.returning_id()` + `RETURNING id`
        (PG1 做的事),这里给一个明确的报错比返回 None 好——返回 None 会让调用方
        静默拿到空主键,而那是一条会一路传到业务数据里的坏值。
        """
        raise AttributeError(
            "PG 后端没有 lastrowid;请用 dialect.returning_id() + RETURNING id "
            "取回主键(见 app/db/dialect.py)")

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class PgConnection:
    """把 psycopg 连接包装成 `sqlite3.Connection` 的最小同形接口。"""

    def __init__(self, dsn: str):
        import psycopg
        from psycopg.rows import dict_row

        # autocommit=False:与 sqlite3 默认一致(调用方显式 commit)。
        # 项目里大量代码是"execute 若干条 → conn.commit()",行为必须保持一致,
        # 否则一个忘记 commit 的路径在 SQLite 上丢数据、在 PG 上却生效(或反之)。
        self._conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)

    def execute(self, sql: str, params=()):
        # 两步都在这里做,顺序固定:
        # ① 保留字加引号(`orders.user` —— `user` 是 PG 保留字,裸写语法错误);
        # ② 占位符 `?` → `%s`。
        #
        # 顺序不能反:`quote_reserved` 会挖字符串字面量再填回,而 `qmark_to_format`
        # 会把字面量里的 `%` 转义成 `%%`。先翻占位符再加引号,那些 `%%` 会被
        # 第二次挖填过程当成普通文本重新处理——虽然当前实现下结果相同,但依赖
        # 这种巧合不值得,把顺序钉死更省心。
        from app.db.dialect import PostgresDialect, quote_reserved

        sql = quote_reserved(sql, PostgresDialect())
        cur = self._conn.cursor()
        cur.execute(qmark_to_format(sql), tuple(params) if params else None)
        return _Cursor(cur)

    def executescript(self, script: str):
        """执行多语句脚本(建表用)。

        psycopg3 支持一次 execute 多条语句,但**不能带参数**——建表脚本本来也没有
        参数。这里不做 `?` 翻译:建表脚本里不该有占位符,若有那是调用方的 bug,
        让它以语法错误的形式暴露比静默翻译更好。
        """
        cur = self._conn.cursor()
        cur.execute(script)
        return _Cursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # 与 sqlite3 的上下文管理器语义对齐:异常时回滚,正常时提交。
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False
