"""给 `source` 字段落地**之前**的历史行补流量来源(一次性,先看后写)。

    python -m app.scripts.backfill_traffic_source            # 只打印方案,不写库
    python -m app.scripts.backfill_traffic_source --apply    # 真写

---

**为什么不在数据库迁移里顺手做掉。** 迁移把历史行一律补成 `unknown`,那是唯一
诚实的默认值:字段加上之前,表里**确实没有任何东西**能区分真实买家与压测。
而本脚本做的是**按 user_id 形态猜**——它是个启发式,不是事实。启发式不该藏在
一次静默的 schema 迁移里,它得有人看过、认过。

所以:默认只打印方案(改多少行、按哪条规则、样例 user_id 长什么样),
`--apply` 才写。规则表就在下面,一眼能读完。

**命不中任何规则的行保持 `unknown`。** 不兜底、不猜:一条来路不明的记录
宁可一直是"判不出",也不能凭空盖上"真实买家"的章——看门狗会拿那个章做自动回滚。

---

实测(本机库,976 轮轨迹 / 87 条归档):

    u1                                836 轮   测试固定用户
    数字 id                            62 轮   hmdp 真实买家
    ab*/ev*/trk*                       41 轮   压测 / 评测
    measure_user/loadchat/framecheck…  35 轮   历次人工走查
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.runtime_context import (  # noqa: E402
    SOURCE_DEV,
    SOURCE_EVAL,
    SOURCE_LIVE,
    SOURCE_LOADTEST,
    SOURCE_UNKNOWN,
)
from app.utils.console import enable_utf8_stdout  # noqa: E402

TABLES = ("skill_traces", "session_archive")

#: 规则表:`(正则, 来源, 这条规则凭什么)`。**按顺序匹配,先中先得。**
#:
#: 每一条都要能说出"凭什么",否则它就是在编。读不出理由的形态一律不写规则,
#: 让它留在 unknown 里。
RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^ab\d"), SOURCE_LOADTEST,
     "app/scripts/loadtest.py 的 A/B 并发用户名前缀"),
    (re.compile(r"^(load|bench)"), SOURCE_LOADTEST,
     "压测脚本用户名前缀(loadchat/lc*/lcd-*)"),
    (re.compile(r"^lc"), SOURCE_LOADTEST,
     "压测脚本用户名前缀(lc1 / lcd-1 / lcd-2 …)"),
    (re.compile(r"^ev\d"), SOURCE_EVAL,
     "评测沙箱用户名前缀"),
    (re.compile(r"^(trk|nat)\d"), SOURCE_EVAL,
     "评测/自然语言用例的用户名前缀"),
    (re.compile(r"^(u1|seller|measure_user|framecheck|forceload)"), SOURCE_DEV,
     "测试固定用户与历次人工走查造的账号"),
    (re.compile(r"^\d+$"), SOURCE_LIVE,
     "纯数字 = hmdp 真实用户 id(与 tb_order.user_id 同命名空间)"),
]


def classify(user_id: str | None) -> tuple[str, str]:
    """返回 `(来源, 理由)`;命不中任何规则 → `(unknown, "")`。"""
    uid = str(user_id or "").strip()
    if not uid:
        return SOURCE_UNKNOWN, ""
    for pattern, source, why in RULES:
        if pattern.match(uid):
            return source, why
    return SOURCE_UNKNOWN, ""


def plan(db) -> dict:
    """扫两张表,算出"哪些行会被改成什么",不写任何东西。"""
    conn = db.connect()
    try:
        out: dict = {}
        for table in TABLES:
            buckets: dict[str, dict] = {}
            rows = conn.execute(
                f"SELECT user_id, COUNT(*) AS n FROM {table} "
                "WHERE COALESCE(source, 'unknown') = 'unknown' "
                "GROUP BY user_id").fetchall()
            for row in rows:
                item = dict(row)
                uid = item.get("user_id")
                source, why = classify(uid)
                b = buckets.setdefault(source, {"rows": 0, "why": why, "samples": []})
                b["rows"] += int(item["n"])
                if len(b["samples"]) < 5:
                    b["samples"].append(str(uid))
            out[table] = buckets
        return out
    finally:
        conn.close()


def apply(db) -> dict:
    """按规则表回填。只动 `source` 仍是 unknown 的行,不覆盖已经标注过的。"""
    conn = db.connect()
    changed: dict[str, int] = {}
    try:
        for table in TABLES:
            rows = conn.execute(
                f"SELECT DISTINCT user_id FROM {table} "
                "WHERE COALESCE(source, 'unknown') = 'unknown'").fetchall()
            n = 0
            for row in rows:
                uid = dict(row).get("user_id")
                source, _ = classify(uid)
                if source == SOURCE_UNKNOWN:
                    continue        # 判不出就留着,不兜底
                cur = conn.execute(
                    f"UPDATE {table} SET source = ? WHERE user_id IS ? "
                    "AND COALESCE(source, 'unknown') = 'unknown'", (source, uid))
                n += cur.rowcount
            changed[table] = n
        conn.commit()
        return changed
    finally:
        conn.close()


def main(argv=None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="真的写库(默认只打印方案)")
    args = parser.parse_args(argv)

    from app.db import get_db
    db = get_db()

    proposal = plan(db)
    total = 0
    for table, buckets in proposal.items():
        print(f"\n{table}(仅 source=unknown 的行):")
        if not buckets:
            print("  没有待回填的行")
            continue
        for source in sorted(buckets, key=lambda s: -buckets[s]["rows"]):
            b = buckets[source]
            tag = "保持不变" if source == SOURCE_UNKNOWN else f"→ {source}"
            print(f"  {b['rows']:>5} 行  {tag:<14} 样例 user_id: "
                  f"{', '.join(b['samples'])}")
            if b["why"]:
                print(f"         依据:{b['why']}")
            if source != SOURCE_UNKNOWN:
                total += b["rows"]

    print(f"\n合计将改写 {total} 行。")
    if not args.apply:
        print("这是**方案预览**,没有写库。确认无误后加 --apply 执行。")
        print("命不中规则的行会保持 unknown —— 判不出就留着,不猜。")
        return 0

    changed = apply(db)
    print("已写入:", ", ".join(f"{t} {n} 行" for t, n in changed.items()))
    print("\n回填是**启发式**(按 user_id 形态猜),不是事实。此后新产生的行由"
          "运行期显式标注(见 app/agent/runtime_context.py)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
