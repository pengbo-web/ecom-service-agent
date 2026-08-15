"""事实一致性双锚点:防止多轮自我改进把**硬事实**一点点改没。

---

**为什么单一锚点不够。** 论文(SkillEvo)治理层的这一处设计很精妙:

    S₀     (生产基线,自进化开始前那一版) → 检测**跨轮累积的事实丢失**
    S_{t-1}(上一轮转正的那一版)          → 检测**本轮新引入的错误**

只拿 S_{t-1} 比,每一轮的删除都只有一点点,轮轮都"看起来没问题",十轮之后
「超过 7 天不支持退货」已经不见了,而没有任何一次转正被拦下来。
只拿 S₀ 比,又分不清"这是本轮删的"还是"上一轮就没了",**修复方向是模糊的**。

两个 diff 一起看才有可操作的结论:
- `S₀ → S_t` 丢掉的事实 = 要**恢复**的;
- `S_{t-1} → S_t` 变动的部分 = 本轮的动作,**不能一律回退**——否则合法的新知识
  也被抹掉了,自进化就退化成"永远不许改"。

---

**什么算"事实"。** 刻意只认两类,都精确、都真的危险:

1. **带单位的数值** —— `7天`、`15 天`、`¥199`、`50%`、`3次`。政策数字是 SKILL.md
   里最不能丢的东西:客服照着它对用户做承诺。
2. **反引号里的工具名** —— `` `query_order` ``。丢一个工具引用,通常意味着流程
   丢了一步,而模型是照着这份文档决定调什么的。

不认散文、不认关键词、不做语义比对。判据必须**确定**:这是一道会拦下转正的闸,
它自己不能是概率性的。宁可漏判几种事实丢失,也不要因为一次正当的措辞调整而
把一个好候选拦在门外——那样的闸,人第二次就会开始用 --force 绕过去,
于是它对真正的事实丢失也失效了。

---

**它拦什么、不拦什么。**

拦:相对 **S₀** 丢了硬事实(且候选里没有以任何形式重新出现)。
不拦:新增事实(合法的新知识)、纯措辞改动、相对 S_{t-1} 的变动。

膨胀率只**报**不拦:论文无治理时 Bloat 是 16.2%,有治理是 2.8%。本项目 skill
是单文件,几百行的文档涨 20% 并不必然是坏事,拦下来只会制造噪声。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 带单位的数值。单位表故意写死而不用通配:`(\d+)\s*\S+` 会把「第 3 步」
#: 「共 5 条」这种排版数字也收进来,一次目录重排就报一堆假的事实丢失。
#
#: 前瞻的字符类**必须写成 ASCII**,不能用 `\w`:Python 正则里汉字也是 `\w`,
#: 于是「超过7天」的 `7` 前面是「过」,`(?<![\w.])` 直接判负,整条规则对中文
#: 一个都匹配不上——而这份文档几乎全是中文。同一个坑本轮已经踩过一次
#: (归属过滤的 `\b`),记在这里:**判据里凡是"非单词字符"的写法,在中文语境
#: 下都要重新想一遍**。
_NUMERIC_FACT_RE = re.compile(
    r"(?<![0-9A-Za-z_.])(\d+(?:\.\d+)?)\s*"
    r"(个工作日|工作日|小时|分钟|天|日|次|折|元|%|％)")
#: 金额前缀写法:`¥199` / `￥1,299`
_MONEY_RE = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")
#: 反引号里的 snake_case 工具名(与 validator.referenced_tools 同一口径)
_TOOL_RE = re.compile(r"`([a-z][a-z0-9_]*)`")

#: 相对 S₀ 的行数增长超过这个比例就在报告里点名。只报不拦,见模块 docstring。
BLOAT_WARN_RATIO = 0.15


def extract_facts(text: str) -> set[str]:
    """抽出这份文本里的硬事实。归一化后返回,便于跨措辞比对。

    归一化是必需的:`7天` 与 `7 天` 是同一个事实,把它们当成两个会让每一次
    排版调整都报成"丢了 7天、新增了 7 天"。
    """
    facts: set[str] = set()
    for num, unit in _NUMERIC_FACT_RE.findall(text or ""):
        facts.add(f"{_norm_num(num)}{unit}")
    for amount in _MONEY_RE.findall(text or ""):
        facts.add(f"¥{_norm_num(amount.replace(',', ''))}")
    for tool in _TOOL_RE.findall(text or ""):
        if "_" in tool:          # 与 validator 同口径:不含下划线的当普通单词
            facts.add(f"`{tool}`")
    return facts


def _norm_num(raw: str) -> str:
    """`7` / `7.0` / `07` 归一成同一个键。"""
    try:
        value = float(raw)
    except ValueError:
        return raw
    return str(int(value)) if value == int(value) else str(value)


def check(candidate: str, baseline: str, previous: str = "") -> dict:
    """双锚点比对。`baseline` = S₀,`previous` = S_{t-1}(留空则退化成单锚点)。

    返回:

    - `lost_since_baseline`:**唯一会拦下转正的一项**。S₀ 里有、候选里没有的硬事实。
    - `lost_since_previous`:本轮删掉的。它是 `lost_since_baseline` 的子集或交集,
      单独列出来是为了让修复方向明确——在这一项里的,是**本轮**动的手。
    - `lost_before_this_round`:S₀ 有、S_{t-1} 已经没有的。**这一项拦不住也不该拦**
      (它上一轮就没了,拦本轮的候选是找错了人),但必须报:它说明前面某一轮
      漏掉了,而只看 S_{t-1} 的话这条永远不会显形。
    - `added` / `bloat_ratio` / `ok` / `reason`。
    """
    cand_facts = extract_facts(candidate)
    base_facts = extract_facts(baseline)
    prev_facts = extract_facts(previous) if previous else base_facts

    lost_baseline = sorted(base_facts - cand_facts)
    lost_previous = sorted(prev_facts - cand_facts)
    # S₀ 有、上一轮已经没有 —— 不是本轮造成的,但只看 S_{t-1} 时永远看不见它。
    lost_before = sorted((base_facts - prev_facts) - cand_facts)
    added = sorted(cand_facts - base_facts - prev_facts)

    base_lines = len([ln for ln in (baseline or "").splitlines() if ln.strip()])
    cand_lines = len([ln for ln in (candidate or "").splitlines() if ln.strip()])
    bloat = ((cand_lines - base_lines) / base_lines) if base_lines else 0.0

    ok = not lost_baseline
    if ok:
        reason = "事实一致性通过"
        if added:
            reason += f";新增 {len(added)} 项事实(合法新知识,未拦截)"
    else:
        this_round = [f for f in lost_baseline if f in lost_previous]
        earlier = [f for f in lost_baseline if f not in lost_previous]
        parts = [f"相对生产基线丢失 {len(lost_baseline)} 项硬事实: {', '.join(lost_baseline)}"]
        if this_round:
            parts.append(f"其中本轮删除: {', '.join(this_round)}")
        if earlier:
            parts.append(f"更早的轮次就已丢失(不是本候选造成的): {', '.join(earlier)}")
        reason = ";".join(parts)

    if abs(bloat) >= BLOAT_WARN_RATIO:
        reason += f";正文相对基线{'膨胀' if bloat > 0 else '缩减'} {abs(bloat) * 100:.0f}%"

    return {
        "ok": ok, "reason": reason,
        "lost_since_baseline": lost_baseline,
        "lost_since_previous": lost_previous,
        "lost_before_this_round": lost_before,
        "added": added,
        "bloat_ratio": round(bloat, 4),
        "baseline_facts": len(base_facts), "candidate_facts": len(cand_facts),
    }


# --------------------------------------------------------------------------
# 从磁盘取两个锚点
# --------------------------------------------------------------------------

def baseline_text(skill_name: str, definitions_dir: str, archive_dir: str) -> tuple[str, str]:
    """取 S₀(生产基线)的正文与它的来源说明。

    S₀ = `_archive/<name>/` 下**时间戳最早**的那一份(备份目录名是
    `YYYYMMDD-HHMMSS`,字典序即时间序)。那是这个 skill 第一次被自进化改动**之前**
    的样子——`backup_current` 在每次转正前把当时的 live 存进去,所以最早那一份
    就是起点。

    一次都没转正过时没有归档,此时 **live 本身就是 S₀**,双锚点退化成单锚点。
    这不是缺陷:第一轮本来就没有"跨轮累积"可言。来源说明里如实写出来。
    """
    archive_root = Path(archive_dir) / skill_name
    if archive_root.is_dir():
        snaps = sorted(p for p in archive_root.iterdir()
                       if p.is_dir() and (p / "SKILL.md").exists())
        if snaps:
            return snaps[0].joinpath("SKILL.md").read_text(encoding="utf-8"), \
                f"归档最早快照 {snaps[0].name}"

    live = Path(definitions_dir) / skill_name / "SKILL.md"
    if live.exists():
        return live.read_text(encoding="utf-8"), "现行版本(尚无归档,双锚点退化为单锚点)"
    return "", "无(全新 skill,无基线可比)"


def previous_text(skill_name: str, definitions_dir: str) -> tuple[str, str]:
    """取 S_{t-1}(上一轮)= 当前 live 目录里那份,它正要被替换掉。"""
    live = Path(definitions_dir) / skill_name / "SKILL.md"
    if live.exists():
        return live.read_text(encoding="utf-8"), "现行版本"
    return "", "无(全新 skill)"


def check_candidate(skill_name: str, candidate_text: str, definitions_dir: str,
                    archive_dir: str) -> dict:
    """转正前的事实一致性检查(`promote()` 的调用入口)。

    全新 skill(既没有 live 也没有归档)一律放行:没有基线时"丢失"这个概念
    不成立,拦下来只是在拦一个我们无从判断的东西。返回里如实标 `applicable=False`
    ——**不是**打一个看起来通过了的 `ok=True` 就完事。
    """
    baseline, base_src = baseline_text(skill_name, definitions_dir, archive_dir)
    previous, prev_src = previous_text(skill_name, definitions_dir)
    if not baseline:
        return {"applicable": False, "ok": True,
                "reason": f"无生产基线可比({base_src}),事实一致性检查不适用",
                "baseline_source": base_src, "previous_source": prev_src,
                "lost_since_baseline": [], "lost_since_previous": [],
                "lost_before_this_round": [], "added": [], "bloat_ratio": 0.0}

    result = check(candidate_text, baseline, previous)
    result["applicable"] = True
    result["baseline_source"] = base_src
    result["previous_source"] = prev_src
    return result
