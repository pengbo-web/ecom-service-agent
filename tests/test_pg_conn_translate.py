"""占位符翻译(PG3 的关键一招)。

PG1 刻意没做占位符统一,理由是"245 处机械替换风险实、收益零",正确时机是
"在 execute 边界做一次转换"。本模块就是那一处——它错了,**249 个占位符全错**,
而且症状是散落在各处的诡异 SQL 错误,极难定位。所以这里的用例密度刻意偏高。
"""

from __future__ import annotations

import pytest

from app.db.pg_conn import qmark_to_format as q


def test_basic_placeholders():
    assert q("SELECT * FROM t WHERE a = ? AND b = ?") == \
        "SELECT * FROM t WHERE a = %s AND b = %s"


def test_question_mark_inside_string_literal_is_kept():
    """字面量里的问号不能翻。

    粗暴 `sql.replace("?", "%s")` 会把它也换掉,产出一条参数个数对不上的 SQL。
    """
    out = q("SELECT * FROM t WHERE msg = '真的吗?' AND id = ?")
    assert "'真的吗?'" in out
    assert out.count("%s") == 1


def test_placeholder_next_to_literal_in_time_fragment():
    """SQLite 版 now_plus_param 产出的正是这种形状:占位符夹在字面量之间。

        datetime('now', '+' || ? || ' hours')

    `'+'` 与 `' hours'` 里的字符不能碰,中间那个 `?` 必须翻。
    """
    out = q("UPDATE t SET x = datetime('now', '+' || ? || ' hours') WHERE id = ?")
    assert "'+'" in out and "' hours'" in out
    assert out.count("%s") == 2


def test_percent_outside_literal_is_escaped():
    """psycopg 用 % 做占位符前缀,SQL 里原有的 % 必须转义成 %%。"""
    assert q("SELECT 10 % 3") == "SELECT 10 %% 3"


def test_percent_inside_like_literal_is_escaped():
    """`LIKE '%foo%'` 是项目里真实存在的形状(list_shared_context 的 prefix 匹配)。

    不转义会让 psycopg 把 `%f` 当成畸形占位符直接报错。
    """
    out = q("SELECT * FROM t WHERE key LIKE '%diagnosis%' AND id = ?")
    assert "'%%diagnosis%%'" in out
    assert out.count("%s") == 1


def test_escaped_single_quote_inside_literal():
    """SQL 里用 '' 表示字面量中的单引号,挖字面量时不能在那里断开。"""
    out = q("SELECT * FROM t WHERE s = 'it''s ok?' AND id = ?")
    assert "'it''s ok?'" in out
    assert out.count("%s") == 1


def test_order_of_operations_does_not_double_escape():
    """**顺序很重要**:先翻 `?` 再转义 `%` 会把刚生成的 `%s` 变成 `%%s`。

    这条用例就是钉那个顺序。
    """
    out = q("SELECT * FROM t WHERE a = ? AND b LIKE '%x%'")
    assert "%s" in out
    assert "%%s" not in out


def test_no_placeholders_is_unchanged_except_percent():
    assert q("SELECT 1") == "SELECT 1"


def test_multiple_literals_are_restored_in_order():
    out = q("SELECT 'a?', 'b?', ? FROM t")
    assert out.index("'a?'") < out.index("'b?'")
    assert out.count("%s") == 1


def test_lastrowid_raises_instead_of_returning_none():
    """psycopg 没有 lastrowid。

    **故意报错而不是返回 None**:返回 None 会让调用方静默拿到空主键,而那是一条
    会一路传到业务数据里的坏值。项目已统一走 RETURNING id(PG1 做的事)。
    """
    from app.db.pg_conn import _Cursor

    class _Fake:
        rowcount = 0

    with pytest.raises(AttributeError, match="RETURNING id"):
        _ = _Cursor(_Fake()).lastrowid


# ---------- 方言对象 ----------

def test_two_dialects_coexist_without_global_state():
    """两套后端必须能在**同一进程**里共存。

    双后端参数化测试是唯一能证明"语义一致"的手段,而它要同时持有两个 Database
    实例。用"模块级 set_backend()"那种全局可变状态会让两边互相打断,而且症状随
    执行顺序漂移——这条用例钉的就是"没有那种全局状态"。
    """
    from app.db.dialect import get_dialect

    s, p = get_dialect("sqlite"), get_dialect("pg")
    assert s.name == "sqlite" and p.name == "pg"
    assert "datetime" in s.now()
    assert "now()" in p.now()
    # 再取一次,前一个实例不受影响
    assert get_dialect("sqlite").name == "sqlite"
    assert s.now() != p.now()


def test_pg_time_fragments_produce_the_same_string_shape():
    """PG 的时间片段必须产出与 SQLite 同形的字符串。

    项目里所有时间列在 SQLite 下是 TEXT('YYYY-MM-DD HH:MM:SS'),上层(前端
    parseTs、list_event_chains 的调用方)都按那个格式处理。不统一形状的话,
    "换了后端所以时间字段格式变了"会漏到上层。
    """
    from app.db.dialect import get_dialect

    p = get_dialect("pg")
    for frag in (p.now(), p.now_minus(3, "hours"), p.now_plus_param("hours")):
        assert "YYYY-MM-DD HH24:MI:SS" in frag


def test_pg_now_minus_coerces_amount_to_int():
    """时长值是**插值**进 SQL 的(它是结构不是参数),强制转换必须是结构性的。"""
    from app.db.dialect import get_dialect

    p = get_dialect("pg")
    assert "make_interval(hours => 5)" in p.now_minus("5", "hours")  # type: ignore[arg-type]


def test_unknown_backend_falls_back_to_sqlite():
    """配置写错不该让数据层整体不可用。"""
    from app.db.dialect import get_dialect

    assert get_dialect("mysql").name == "sqlite"
    assert get_dialect("").name == "sqlite"
