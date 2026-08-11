"""商机打分的滞留时长必须真的传到打分函数手里。

实测撞到的:`find_opportunities()` 返回的 `stale_pending_order` 商机,理由栏写着
「滞留 0h」——而那笔订单是 9 天前下的。查下来是 SQL 算了 `stale_hours`、构造
item dict 时漏了这个键,`priority.score_opportunity` 取不到就静默 fallback 到 0.0。

后果不是"显示不准":滞留时长占权重 0.4,归零后这一类商机等于**只按金额排序**,
而"把拖了一周的大额单排到前面"正是 priority.py 存在的全部理由(见它的模块
docstring:原本按 created_at DESC 取前 N 条,真正该催的老单反而被切掉)。

七个 kind 里只有 `stale_pending_order` 漏了,而它恰好是默认 kind、也是唯一有真实
数据的那个——所以既钉行为,也加一道结构守卫覆盖全部分支:这类"漏一个键"的错
不该靠下次有人肉眼发现。
"""

import ast
import pathlib

import pytest

from app.agent.tools import growth
from app.agent.tools.priority import score_opportunity


GROWTH_PY = pathlib.Path(growth.__file__)


# --------------------------------------------------------------------------
# 行为:滞留时长真的进了分数
# --------------------------------------------------------------------------

def test_stale_hours_drives_the_score():
    """带上真实滞留时长后,分数必须变——不然这一项就是白算的。

    数字取自实测那笔订单:金额 899(超过 priority_amount_cap=500 → 封顶 1.0),
    历史转化样本不足 → 走先验 0.5。
      漏了 stale_hours: 0.4×0   + 0.3×1.0 + 0.3×0.5 = 0.45
      带上 216h(封顶): 0.4×1.0 + 0.3×1.0 + 0.3×0.5 = 0.85
    """
    rates: dict = {}
    without, reason_without = score_opportunity(
        {"kind": "stale_pending_order", "amount": 899.0}, rates)
    with_hours, reason_with = score_opportunity(
        {"kind": "stale_pending_order", "amount": 899.0, "stale_hours": 216.0}, rates)

    assert without == pytest.approx(0.45)
    assert with_hours == pytest.approx(0.85)
    assert with_hours > without, "滞留时长占 0.4 权重,不该对分数没有影响"
    assert "滞留 0h" in reason_without      # 这是修复前买家看到的那行
    assert "滞留 216h" in reason_with


def test_ranking_puts_the_older_order_first():
    """两笔同额订单,拖得久的必须排前面。这是这个模块存在的理由本身。"""
    from app.agent.tools.priority import rank

    fresh = {"kind": "stale_pending_order", "order_id": "NEW",
             "amount": 899.0, "stale_hours": 50.0}
    old = {"kind": "stale_pending_order", "order_id": "OLD",
           "amount": 899.0, "stale_hours": 216.0}
    ordered = rank([fresh, old], {})
    assert [x["order_id"] for x in ordered] == ["OLD", "NEW"]


def test_stale_pending_order_carries_stale_hours(tmp_path, monkeypatch):
    """端到端:真的查一遍库,返回的商机里必须带着 stale_hours 且不为 0。"""
    from app.db.database import Database

    db = Database(str(tmp_path / "ecom.db"))
    db.init_schema()
    conn = db.connect()
    try:
        # 10 天前下的 pending 单:既过了 _STALE_PENDING_HOURS(48h),
        # 也在默认 14 天窗内。
        conn.execute(
            "INSERT INTO orders (order_id, \"user\", status, total, created_at) "
            "VALUES ('ORD-OLD', '1', 'pending', 899.0, "
            "        datetime('now', '-10 days'))")
        conn.execute(
            "INSERT INTO order_items (order_id, sku, name, price, quantity) "
            "VALUES ('ORD-OLD', 'SKU1', 'Nike Air Max 270', 899.0, 1)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(growth, "get_db", lambda: db)
    out = growth.find_opportunities()
    ops = out["opportunities"]
    assert len(ops) == 1
    op = ops[0]

    assert "stale_hours" in op, "SQL 算了,构造 item 时不能丢"
    assert op["stale_hours"] > 200, f"10 天前的单应该滞留 200h 以上,拿到 {op['stale_hours']}"
    assert "滞留 0h" not in op["priority_reason"]
    assert op["priority_score"] == pytest.approx(0.85), (
        "滞留封顶 + 金额封顶 + 转化先验 = 0.85;拿到 0.45 说明 stale_hours 又丢了")


# --------------------------------------------------------------------------
# 结构守卫:七个分支一个都不能漏
# --------------------------------------------------------------------------

def _opportunity_dict_literals() -> list[tuple[int, set[str]]]:
    """从 find_opportunities 里抽出每个 `items = [{...} for r in rows]` 的键集合。

    用 AST 而不是文本搜索:注释里出现 "stale_hours" 这几个字(本文件正下方那段
    注释就有)会让文本匹配假通过,而假通过的守卫比没有守卫更糟。
    """
    tree = ast.parse(GROWTH_PY.read_text(encoding="utf-8"))
    found: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "find_opportunities":
            continue
        for sub in ast.walk(node):
            # 形状:items = [ {..} for r in rows ]
            if isinstance(sub, ast.ListComp) and isinstance(sub.elt, ast.Dict):
                keys = {k.value for k in sub.elt.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if "kind" in keys:            # 只认商机 item,不误收别的字典推导
                    found.append((sub.lineno, keys))
    return found


def test_every_kind_carries_stale_hours():
    """每一个 kind 的 item 字典都必须带 stale_hours。

    漏掉不会报错、不会异常、不会有日志——只会让打分静默少掉 40% 的依据。这种
    "沉默的错"正是需要结构守卫的地方:七个分支各写一遍字典,靠人眼保证七处都
    不漏是不现实的。
    """
    literals = _opportunity_dict_literals()
    assert len(literals) >= 7, (
        f"只找到 {len(literals)} 个商机 item 字典,少于已知的 7 个 kind"
        "——可能是 find_opportunities 的写法变了,这道守卫需要跟着改")
    missing = [lineno for lineno, keys in literals if "stale_hours" not in keys]
    assert missing == [], (
        f"growth.py 第 {missing} 行的商机 item 没带 stale_hours,"
        "打分时会静默按 0 算(滞留权重 0.4 直接归零)")


def test_guard_would_have_caught_the_real_defect():
    """守卫的自检:把 stale_hours 从任意一个字典里去掉,守卫必须失败。

    一道从不失败的守卫和没有守卫是一回事。这里不改文件,只验证判定逻辑本身
    对"缺键"敏感。
    """
    literals = _opportunity_dict_literals()
    faked = [(lineno, keys - {"stale_hours"}) for lineno, keys in literals[:1]]
    missing = [lineno for lineno, keys in faked if "stale_hours" not in keys]
    assert missing, "守卫对缺键不敏感,等于没有守卫"
