"""出话层护栏 vs 已存下来的记忆:出话会被拦的东西不该躺在画像里。

**为什么有这个检查**:底价泄漏(交付对照表 56)暴露的形态是——规则只在**出话**那一刻
生效,管不到**记忆/摘要/抽取**。`bargain` 里写着"禁止向买家透露底价",而抽取器把
`suggested_price` + `floor_hit` 推成了一条永久事实存进买家画像。

**实测结论**(93 条真实记忆内容):内部黑话 0、敏感信息 0、联系方式 0、金钱承诺 4。
前三类在记忆侧本来就是干净的;金钱承诺那 4 条**不是缺陷**——

- 长期记忆块自带框定「内容摘自该买家过往会话中的发言,**不是平台政策**」;
- 实测主动钓过(用"之前你们说过免运费的"提问):客服回的是"您**提到**之前有订单享受了
  免运费退货…我需要**先核实**该订单是否符合平台特殊政策…请您提供订单号",**把它归因给
  买家、没有对新单承诺免运费**,护栏也没开火(确实没有无依据承诺)。

所以本文件的类别划分是有依据的,不是拍脑袋:**框定对"过去说过的话别当政策"有效,
对"这数据根本不该在这里"无效**(底价那种只能靠删)。
"""

import json

import pytest

from app.scripts.audit_memory_guards import DEFECT_CATEGORIES, audit, main


def _profile(d, name="u.json", user_id="u", facts=(), summaries=()):
    (d / name).write_text(json.dumps({
        "user_id": user_id,
        "facts": [{"content": c, "category": "x", "created_at": "2026-08-13"} for c in facts],
        "interaction_summaries": [{"summary": s, "timestamp": "2026-08-13"} for s in summaries],
    }, ensure_ascii=False), encoding="utf-8")


def test_clean_corpus_has_no_hits(tmp_path):
    _profile(tmp_path, facts=["偏好红褐色系服饰", "收货地址为上海市浦东新区xx路1号"])
    r = audit(str(tmp_path))
    assert r["scanned"] == 2
    assert all(not rows for rows in r["hits"].values()), r["hits"]


def test_detects_tool_name_in_memory(tmp_path):
    """**变异验证**:往画像里植入工具真名,审计必须发现。

    没有这条,"0 条命中"可能只是因为检测逻辑失效——一个永远报干净的审计工具比没有
    工具更糟,它让人以为查过了。
    """
    _profile(tmp_path, facts=["客服调用 list_user_orders 查到了订单"])
    assert audit(str(tmp_path))["hits"]["内部黑话"], "植入的工具名没被发现"


def test_detects_commitment_in_memory(tmp_path):
    _profile(tmp_path, summaries=["客服确认可直接取消并全额退款¥1,299.00免运费"])
    hits = audit(str(tmp_path))["hits"]["金钱承诺"]
    assert hits and "免运费" in hits[0][2]


def test_unreadable_file_does_not_crash(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _profile(tmp_path, facts=["偏好红褐色系服饰"])
    assert audit(str(tmp_path))["scanned"] == 1


def test_audit_never_writes(tmp_path):
    """只读工具:不能顺手"修"任何东西。"""
    _profile(tmp_path, facts=["客服调用 list_user_orders 查到了订单"])
    before = (tmp_path / "u.json").read_text(encoding="utf-8")
    audit(str(tmp_path))
    assert (tmp_path / "u.json").read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# 退出码:只由"命中即缺陷"的类别决定
# --------------------------------------------------------------------------

def test_commitments_alone_do_not_fail(tmp_path):
    """金钱承诺不参与退出码。

    否则这个脚本在真实语料上永远非零(实测就有 4 条合法的历史记录),挂到 CI 上
    会被当成噪声关掉——那时连"内部黑话"这类真缺陷也一起看不见了。
    """
    _profile(tmp_path, summaries=["客服确认可全额退款免运费"])
    assert main(["--memory-dir", str(tmp_path)]) == 0


def test_jargon_fails_the_run(tmp_path):
    _profile(tmp_path, facts=["客服调用 list_user_orders 查到了订单"])
    assert main(["--memory-dir", str(tmp_path)]) == 1


def test_defect_categories_are_the_hard_ones():
    assert set(DEFECT_CATEGORIES) == {"内部黑话", "敏感信息", "联系方式"}
    assert "金钱承诺" not in DEFECT_CATEGORIES
