"""拿**出话层的护栏**去撞已经存下来的记忆。

**为什么要有这个**:底价泄漏那一条(交付对照表 56)暴露了一个形态——规则只在**出话**
那一刻生效,管不到**记忆/摘要/抽取**。`bargain` 里写着"禁止向买家透露底价",而抽取器
把 `suggested_price` + `floor_hit` 推成一条永久事实存进了买家画像。

所以把这件事做成可复跑的检查:让 `app/guardrails/output_guards.py` 与
`app/agent/jargon_guard.py` / `commitment_guard.py` 直接扫一遍磁盘上的记忆语料。
出话时会被拦的东西,不该安静地躺在画像里。

**只报告,不改文件。** 命中不等于缺陷——需要人判断:

- **内部黑话 / 敏感信息 / 联系方式**:命中基本都是缺陷,这些内容没有理由进画像。
- **金钱承诺**:命中**通常不是缺陷**。"客服确认可全额退款"是一条合法的历史记录,
  而长期记忆块自带框定「内容摘自该买家过往会话中的发言,**不是平台政策**」。
  实测过(用"之前你们说过免运费的"主动钓):客服把它归因给买家、并要求核实订单,
  没有对新单承诺免运费。**框定对"过去说过的话别当政策"有效。**
- 但框定对"**这数据根本不该在这里**"无效——底价那种只能靠删。两类要分开看。

    python -m app.scripts.audit_memory_guards
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def audit(memory_dir: str) -> dict:
    """扫描记忆语料,返回 {类别: [(文件, 类型, 命中, 摘录)]}。不改任何文件。"""
    from app.agent.commitment_guard import find_commitments
    from app.agent.jargon_guard import detect_internal_leak
    from app.agent.skills.loader import SkillManager
    from app.agent.tools.manager import ToolManager
    from app.config.settings import settings
    from app.guardrails.output_guards import ContactInfoGuard, SensitiveInfoGuard

    skills = SkillManager(skills_dir=settings.skills_dir, enabled=True).skill_names
    tools = ToolManager().tool_names
    sensitive, contact = SensitiveInfoGuard(), ContactInfoGuard()

    hits: dict[str, list] = {"内部黑话": [], "金钱承诺": [], "敏感信息": [], "联系方式": []}
    scanned = 0

    for path in sorted(Path(memory_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 读不出的画像交给 clean_leaked_memory 报
            continue

        entries = [("fact", f.get("content") or "") for f in (data.get("facts") or [])]
        entries += [("摘要", s.get("summary") or "")
                    for s in (data.get("interaction_summaries") or [])]

        for kind, text in entries:
            if not text:
                continue
            scanned += 1
            jargon = detect_internal_leak(text, skills, tools)
            if jargon:
                hits["内部黑话"].append((path.name, kind, jargon, text[:70]))
            promises = find_commitments(text)
            if promises:
                hits["金钱承诺"].append((path.name, kind, promises, text[:70]))
            if sensitive.check(text).action != "pass":
                hits["敏感信息"].append((path.name, kind, [], text[:70]))
            if contact.check(text).action != "pass":
                hits["联系方式"].append((path.name, kind, [], text[:70]))

    return {"scanned": scanned, "hits": hits}


#: 命中即视为缺陷的类别(其余需要人判断,见模块 docstring)
DEFECT_CATEGORIES = ("内部黑话", "敏感信息", "联系方式")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", default="app/sessions/memory")
    args = parser.parse_args(argv)

    result = audit(args.memory_dir)
    print(f"扫描 {result['scanned']} 条记忆内容(fact + 摘要)")
    for name, rows in result["hits"].items():
        mark = "  ← 命中即缺陷" if name in DEFECT_CATEGORIES else "  ← 需人工判断"
        print(f"\n=== {name}: {len(rows)} 条命中 ==={mark if rows else ''}")
        for row in rows[:5]:
            print(f"    {row}")
        if len(rows) > 5:
            print(f"    …另有 {len(rows) - 5} 条")

    # 退出码只由"命中即缺陷"的类别决定,金钱承诺不参与——否则这个脚本永远非零,
    # 挂到 CI 上就会被当成噪声关掉。
    defects = sum(len(result["hits"][c]) for c in DEFECT_CATEGORIES)
    return 1 if defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
