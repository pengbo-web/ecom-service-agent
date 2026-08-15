"""事实一致性双锚点:防止多轮自我改进把硬事实**一点点**改没。

这是本轮唯一一道**会拦下转正**的新闸,所以它的设计取向和别处相反:宁可漏判,
不可误判。一道会误伤的闸,人第二次就会开始用 `--force` 绕过去,于是它对真正的
事实丢失也一起失效了。

两个锚点各管一件事:
- `S₀`(归档里最早那份)管**跨轮累积的丢失** —— 只和上一版比,每轮删一点点,
  轮轮都"看起来没问题",十轮之后「超过 7 天不支持退货」已经不见了。
- `S_{t-1}`(现行版本)管**本轮动了什么** —— 只和 S₀ 比,分不清是本轮删的还是
  上一轮就没了,修复方向是模糊的。
"""

import pytest

from app.agent.skills.fact_consistency import (
    BLOAT_WARN_RATIO,
    check,
    check_candidate,
    extract_facts,
)


# --------------------------------------------------------------------------
# 抽取:只认精确的两类
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("超过7天不支持退货", {"7天"}),
    ("超过 7 天不支持退货", {"7天"}),          # 空格不该变成另一个事实
    ("运费 ¥15 由买家承担", {"¥15"}),
    ("满￥1,299 免运费", {"¥1299"}),
    ("最高可退 50%", {"50%"}),
    ("先调用 `query_order` 核对", {"`query_order`"}),
    ("15个工作日内到账", {"15个工作日"}),
])
def test_extract_facts_normalises(text, expected):
    assert extract_facts(text) == expected


@pytest.mark.parametrize("text", [
    "第 3 步:确认收货地址",       # 排版序号不是政策数字
    "共 5 条注意事项",
    "字段 `body` 必填",           # 不含下划线,与 validator 同口径,当普通单词
    "请礼貌回应用户",
])
def test_layout_numbers_and_plain_words_are_not_facts(text):
    """**误判比漏判危险。** `(\\d+)\\s*\\S+` 这种宽判据会把「第 3 步」也收进来,
    一次目录重排就报一堆假的事实丢失,而那道闸会拦下一个完全正常的候选。"""
    assert extract_facts(text) == set()


# --------------------------------------------------------------------------
# 拦什么
# --------------------------------------------------------------------------

def test_losing_a_policy_number_is_blocked():
    r = check(candidate="退货流程:先核对订单", baseline="超过7天不支持退货,请先核对订单")
    assert r["ok"] is False
    assert r["lost_since_baseline"] == ["7天"]
    assert "7天" in r["reason"]


def test_losing_a_tool_reference_is_blocked():
    """丢一个工具引用通常意味着流程丢了一步,而模型是照着这份文档决定调什么的。"""
    r = check(candidate="查订单后回复用户",
              baseline="先调用 `query_order`,再调用 `query_logistics`")
    assert r["ok"] is False
    assert r["lost_since_baseline"] == ["`query_logistics`", "`query_order`"]


def test_rewording_that_keeps_the_fact_passes():
    """一次正当的措辞调整不该被拦。这正是"宁可漏判"的取向要保护的东西。"""
    r = check(candidate="超过 7 天的订单平台不支持退货",
              baseline="超过7天不支持退货")
    assert r["ok"] is True


def test_adding_facts_is_allowed_and_reported():
    """新增是合法的新知识,不拦——但要报,否则"这一版多了什么"没人看得见。"""
    r = check(candidate="超过7天不支持退货;运费 ¥15", baseline="超过7天不支持退货")
    assert r["ok"] is True and r["added"] == ["¥15"]
    assert "新增 1 项事实" in r["reason"]


# --------------------------------------------------------------------------
# 双锚点:单锚点会漏掉的那种退化
# --------------------------------------------------------------------------

def test_gradual_erosion_that_a_single_previous_anchor_would_miss():
    """**双锚点存在的全部理由。**

    三轮改进,每一轮只删一个事实:
      S₀   : 7天 / ¥15 / `query_order`
      第1轮: 删掉 ¥15        —— 只和上一版比,"少了一个",不痛不痒
      第2轮: 删掉 `query_order` —— 同上
    只拿 S_{t-1} 比,两轮都不会被拦;和 S₀ 比,两项丢失一次性显形。
    """
    s0 = "超过7天不支持退货,运费 ¥15,先调用 `query_order`"
    round1 = "超过7天不支持退货,先调用 `query_order`"
    round2 = "超过7天不支持退货"

    # 只看上一版:第 2 轮"只"少了一个工具名
    assert check(round2, baseline=round1)["lost_since_baseline"] == ["`query_order`"]
    # 双锚点:相对 S₀ 的两项一起显形
    r = check(round2, baseline=s0, previous=round1)
    assert r["ok"] is False
    assert r["lost_since_baseline"] == ["`query_order`", "¥15"]


def test_reason_separates_this_round_from_earlier_rounds():
    """修复方向必须明确:哪些是**本轮**动的手,哪些是更早就没了的。
    混成一句"丢了 N 项"会让人去改一个本轮根本没碰的地方。"""
    s0 = "超过7天不支持退货,运费 ¥15,先调用 `query_order`"
    prev = "超过7天不支持退货,先调用 `query_order`"      # ¥15 上一轮就没了
    cand = "超过7天不支持退货"                            # 本轮删的是 query_order

    r = check(cand, baseline=s0, previous=prev)
    assert "本轮删除: `query_order`" in r["reason"]
    assert "更早的轮次就已丢失" in r["reason"] and "¥15" in r["reason"]


def test_earlier_loss_alone_is_still_blocked_but_attributed():
    """上一轮就丢了、本轮没再丢 —— 仍然不放行(事实确实不在线上了),
    但 reason 必须写明不是本候选造成的,否则会有人去改一份没问题的候选。"""
    s0 = "超过7天不支持退货,运费 ¥15"
    prev = cand = "超过7天不支持退货"
    r = check(cand, baseline=s0, previous=prev)
    assert r["ok"] is False
    assert r["lost_since_previous"] == []          # 本轮什么都没删
    assert r["lost_before_this_round"] == ["¥15"]


def test_single_anchor_degrades_gracefully():
    """`previous` 留空 = 退化成单锚点(第一轮本来就没有"跨轮"可言)。"""
    r = check("超过7天不支持退货", baseline="超过7天不支持退货,运费 ¥15")
    assert r["ok"] is False and r["lost_since_previous"] == ["¥15"]


# --------------------------------------------------------------------------
# 膨胀率:只报不拦
# --------------------------------------------------------------------------

def test_bloat_is_reported_not_blocked():
    """论文无治理时 Bloat 是 16.2%。但本项目 skill 是单文件,几百行的文档涨 20%
    并不必然是坏事 —— 拦下来只会制造噪声,而噪声会让整道闸失去可信度。"""
    r = check("超过7天不支持退货\n" + "\n".join(f"补充说明 {i}" for i in range(20)),
              baseline="超过7天不支持退货")
    assert r["ok"] is True
    assert r["bloat_ratio"] > BLOAT_WARN_RATIO
    assert "膨胀" in r["reason"]


def test_shrinkage_is_also_reported():
    r = check("超过7天不支持退货",
              baseline="超过7天不支持退货\n" + "\n".join(f"说明 {i}" for i in range(20)))
    assert "缩减" in r["reason"]


# --------------------------------------------------------------------------
# 取锚点 & 全新 skill
# --------------------------------------------------------------------------

def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_baseline_is_the_earliest_archive_snapshot(tmp_path):
    """S₀ 是**最早**那一份,不是最近那一份。取错了,双锚点就等于两个 S_{t-1}。"""
    defs, arch = tmp_path / "defs", tmp_path / "arch"
    _write(defs / "s" / "SKILL.md", "现行:无事实")
    _write(arch / "s" / "20260101-000000" / "SKILL.md", "最早:超过7天不支持退货")
    _write(arch / "s" / "20260601-000000" / "SKILL.md", "较近:无事实")

    r = check_candidate("s", "候选:无事实", str(defs), str(arch))
    assert r["applicable"] is True
    assert r["lost_since_baseline"] == ["7天"]
    assert "20260101-000000" in r["baseline_source"]


def test_no_archive_falls_back_to_live_as_baseline(tmp_path):
    """一次都没转正过时没有归档,live 本身就是 S₀。这不是缺陷,要如实说出来。"""
    defs, arch = tmp_path / "defs", tmp_path / "arch"
    _write(defs / "s" / "SKILL.md", "超过7天不支持退货")
    r = check_candidate("s", "无事实", str(defs), str(arch))
    assert r["ok"] is False
    assert "退化为单锚点" in r["baseline_source"]


def test_brand_new_skill_is_not_applicable_rather_than_passing(tmp_path):
    """全新 skill 没有基线,"丢失"这个概念不成立。

    但返回的是 `applicable=False`,**不是**一个看起来通过了的 `ok=True` 就完事——
    "没检查"和"检查通过"在报告里必须是两回事(与 gate 的 evaluable 同一条)。
    """
    r = check_candidate("brand-new", "全新内容 7天",
                        str(tmp_path / "defs"), str(tmp_path / "arch"))
    assert r["applicable"] is False and r["ok"] is True
    assert "不适用" in r["reason"]


# --------------------------------------------------------------------------
# 接进 promote()
# --------------------------------------------------------------------------

def _skill(text, name="s"):
    return f"---\nname: {name}\ndescription: 测试用技能描述\n---\n{text}\n"


def test_promote_blocks_a_candidate_that_dropped_a_policy_number(tmp_path):
    from app.scripts.promote_skill import promote

    defs, cands, arch = (tmp_path / "defs", tmp_path / "cands", tmp_path / "arch")
    _write(defs / "s" / "SKILL.md", _skill("超过7天不支持退货,先调用 `query_order`"))
    _write(cands / "s" / "SKILL.md", _skill("先调用 `query_order`"))

    r = promote("s", str(defs), str(cands), str(arch),
                gate_result={"promote": True}, force=False, timestamp="t1")
    assert r["promoted"] is False
    assert "事实一致性未通过" in r["reason"] and "7天" in r["reason"]
    # 没通过就绝不能碰正式目录
    assert "7天" in (defs / "s" / "SKILL.md").read_text(encoding="utf-8")


def test_force_alone_does_not_bypass_the_fact_gate(tmp_path):
    """**差点做错的地方。**

    `force` 的既有含义是"放行评测门禁"。而界面上的「转正上线」按钮**永远**带
    force=true(它默认不跑门禁,后端对 gate=None fail-closed,不带 force 一步都
    走不了)。把事实一致性也挂在 force 上,这道闸在人最常走的那条路上从来不生效
    —— 一道只在 CLI 上有效的闸不叫闸。
    """
    from app.scripts.promote_skill import promote

    defs, cands, arch = (tmp_path / "defs", tmp_path / "cands", tmp_path / "arch")
    _write(defs / "s" / "SKILL.md", _skill("超过7天不支持退货"))
    _write(cands / "s" / "SKILL.md", _skill("退货请联系客服"))

    r = promote("s", str(defs), str(cands), str(arch), gate_result=None,
                force=True, timestamp="t1")
    assert r["promoted"] is False
    assert "事实一致性未通过" in r["reason"]


def test_allow_fact_loss_lets_it_through_but_says_so_in_the_reason(tmp_path):
    """独立开关放行它:人明确知道自己在删哪几条硬事实。
    但**照样记进 reason** —— 那是这次转正唯一的留痕。"""
    from app.scripts.promote_skill import promote

    defs, cands, arch = (tmp_path / "defs", tmp_path / "cands", tmp_path / "arch")
    _write(defs / "s" / "SKILL.md", _skill("超过7天不支持退货"))
    _write(cands / "s" / "SKILL.md", _skill("退货请联系客服"))

    r = promote("s", str(defs), str(cands), str(arch), gate_result=None,
                force=True, timestamp="t1", allow_fact_loss=True)
    assert r["promoted"] is True
    assert "已显式放行事实一致性" in r["reason"] and "7天" in r["reason"]


def test_promote_passes_when_facts_are_preserved(tmp_path):
    from app.scripts.promote_skill import promote

    defs, cands, arch = (tmp_path / "defs", tmp_path / "cands", tmp_path / "arch")
    _write(defs / "s" / "SKILL.md", _skill("超过7天不支持退货"))
    _write(cands / "s" / "SKILL.md", _skill("超过 7 天的订单不支持退货,请先致歉再解释"))

    r = promote("s", str(defs), str(cands), str(arch),
                gate_result={"promote": True}, force=False, timestamp="t1")
    assert r["promoted"] is True, r["reason"]
    assert r["fact_check"]["ok"] is True


def test_the_real_track_order_history_has_no_false_positive():
    """**对仓库里真实的三版归档跑一遍。**

    误判在这里的代价最高:一条会拦下转正的闸,如果对真实历史就报错,它上线第一天
    就会被人用 --force 绕过去。实测三版演进从未丢过事实,只多了一个「2小时」。
    """
    from pathlib import Path

    defs = "app/agent/skills/definitions"
    arch = f"{defs}/_archive"
    live = Path(defs) / "track-order" / "SKILL.md"
    if not live.exists() or not (Path(arch) / "track-order").is_dir():
        pytest.skip("本机没有 track-order 的归档历史")

    r = check_candidate("track-order", live.read_text(encoding="utf-8"), defs, arch)
    assert r["ok"] is True, r["reason"]
    assert r["lost_since_baseline"] == []
