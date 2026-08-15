"""门禁用例自动合成:从**真实会话**里生成 skill 的门禁用例,解开"新 skill 永远
转不了正"这个死结。

---

**死结长什么样。** 实测链条(`--start-all` 每一轮都走同一条):

    新建 skill 无对照组 → risk=medium → 策略 gate_then_watch
      → gate_then_watch 要求先过离线门禁
      → 离线门禁要求评测集里有用例在 related_skills 里点名它
      → 没有任何机制为新 skill 产生用例
      → 每轮都打 gate_unavailable,永远如此

跑本模块之前的实测:`draft-outreach-campaign` / `daily-business-report` /
`refund-attribution` / `product-health-check` 四个**现行** skill 的门禁用例数
全是 **0** —— 它们连"改进后转正"都走不通,更不用说新蒸馏的候选。

---

**为什么这里一次 LLM 都不调。**

方案原稿写的是"每条会话 → LLM 抽成 EvalCase"。真动手时发现不需要:本仓库已经
把每一项断言的**事实来源**都准备好了,让模型再猜一遍只会引入它猜错的可能。

    turns             = 买家在那一轮说的原话        (归档会话,逐字)
    expected_tools    = 那一轮实际调用过的工具名    (assistant 消息里的 tool_calls)
    expected_keywords = 人工坐席回复里的已知词      (`keywords_from_reply`,确定性词表)
    related_skills    = [skill_name]

方案里那条纪律("模型只负责整理格式,不负责决定正确答案是什么")推到底,就是
**模型连格式也不必整理**。这同时让合成用例可复现、零成本、可在单测里完整验证——
一条门禁用例是要被当作裁判尺用的,它自己必须先是确定的。

---

**两个采样源,强弱不同,都要报出来。**

1. `SOURCE_TRACE`(强):`skill_traces` 里这个 skill **确实执行过**的轮次。
   归因是事实而非猜测(与 `failure_cases.py` 同一条口径)。
2. `SOURCE_KEYWORD`(弱):按 skill 自己 frontmatter 里声明的关键词去归档会话里捞。
   **新蒸馏的候选一条轨迹都没有**——它还没上过线。只有轨迹这一个源的话,死结对
   "全新 skill"这个最需要它的场景根本没解开。

   这一路是启发式的:命中关键词不等于这段对话真属于该 skill 的职责范围。所以它
   只决定**挑哪些真实会话**,不决定任何一条断言——断言仍然全部来自那段会话里
   真实发生过的事。每条用例的来源都记在 `provenance` 里,界面上如实标注。

---

**合成用例与人工用例分开存。** 落 `app/evaluation/cases_synth/<skill>.json`,
不进 `cases.json`。理由不是洁癖:`cases.json` 同时是**回归基线**的采样集
(`eval_baseline_path` 的来源),把未经人工审核的用例混进去,基线会随着自动合成
而漂移,从此失去"参照"这个唯一作用。

id 一律以 `synth-` 开头(`SYNTH_ID_PREFIX`),这样任何拿到 case_id 的下游——
日志、界面、报表——不查文件也知道这条没有人审过。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.agent.skills.canary import VARIANT_CANARY
from app.agent.skills.execution_trace import LOAD_SKILL_TOOL
from app.agent.skills.validator import is_safe_skill_name

#: 合成用例 id 前缀。**这是"未经人工审核"在系统里唯一的全局可见标记**——
#: case_id 会流到评测报告、门禁日志、界面,凡是拿得到 id 的地方都能判断。
SYNTH_ID_PREFIX = "synth-"

#: 采样源。强弱不同,必须分开记(见模块 docstring)。
SOURCE_TRACE = "trace"
SOURCE_KEYWORD = "keyword"

#: 一个 skill 最多合成多少条。`MIN_TRUSTWORTHY_CASES` 是 3,给到 5 留一点余量;
#: 再多不会让门禁更准,只会让每次门禁多跑两遍 LLM(两侧各跑一次)。
DEFAULT_MAX_CASES = 5

#: 买家原话短于这个长度的轮次不成用例("嗯""好的""在吗")——它复现不了任何东西。
MIN_TURN_CHARS = 6

#: frontmatter description 里声明关键词的写法,实测**三种都有**,而且第三种差点
#: 被漏掉:`关键词：查订单、我的订单`、`适用关键词：退货、退款`、
#: `关键词包括“优惠券”“有哪些券”`。第一版只认冒号,`query-coupons` 于是被判成
#: "未声明关键词",合成 0 条——它明明声明了,只是没写冒号。
#: 前瞻是必需的。把冒号和"包括"**都**写成可选,`关键词` 三个字后面跟任何东西都
#: 算声明——一句普通的散文「没有声明关键词的描述」会被解析出关键词「的描述」,
#: 然后拿它去捞会话。前瞻要求 `关键词` 紧跟着冒号或"包括/有/如"之一才算声明。
_KEYWORD_PREFIX_RE = re.compile(
    r"(?:适用)?关键词(?=[:：]|包括|有|如)(?:包括|有|如)?[:：]?\s*(.+)$")
_KEYWORD_SPLIT_RE = re.compile(r"[、,,;;/|]+")
#: 带引号的写法里,引号内才是词,引号外的"包括""等"是散文。
_QUOTED_RE = re.compile(r"[“\"「『]([^”\"」』]{2,12})[”\"」』]")

#: **测试脚手架**,不能进裁判尺。实测第一批合成结果里混进两条:
#: 「加载 track-order 技能查 ORD-20240115-001 快递」——那是我自己走查时打的话,
#: 不是买家会说的。更要命的是这种输入**把答案写在题面上**:它直接点名要加载哪个
#: skill,于是这条用例永远测不出"该不该选中这个 skill",而那正是门禁要测的东西。
_SCAFFOLD_RE = re.compile(r"加载\s*[A-Za-z0-9_-]*\s*技能|load_skill|调用\s*load_skill")


def is_synthetic_case_id(case_id: str) -> bool:
    """这条用例是自动合成的吗。全仓库唯一判据,勿在别处另写一份。"""
    return str(case_id or "").startswith(SYNTH_ID_PREFIX)


# --------------------------------------------------------------------------
# 归档会话 → 轮次
# --------------------------------------------------------------------------

@dataclass
class Turn:
    """归档会话里的一轮:买家说了什么、这一轮调了哪些工具、最后回了什么。

    `tools` 按调用顺序、去重后保留——`expected_tools` 是"整个会话应调用过的工具"
    (见 `EvalCase`),重复的名字不增加信息,只让期望看起来更严格。
    """

    user: str
    tools: list[str] = field(default_factory=list)
    reply: str = ""


def _tool_names(msg: dict) -> list[str]:
    """从一条 assistant 消息里取它发起的工具名。

    归档里的形状与 OpenAI 原始消息一致:
    `{"role":"assistant","tool_calls":[{"function":{"name":...,"arguments":...}}]}`。
    """
    out = []
    for call in msg.get("tool_calls") or []:
        name = ((call or {}).get("function") or {}).get("name")
        if name:
            out.append(str(name))
    return out


def split_turns(messages: list[dict] | str | None) -> list[Turn]:
    """把归档会话切成轮次。以 user 消息为界,后续 assistant/tool 归属于该轮。

    `messages` 允许是未反序列化的 JSON 串:`Database.get_archived_session` 返回
    `dict(row)` 而**不做 json.loads**,`list_recent_archives` 却做了。两个调用方
    给的形状不一样,在这里一次性容错(与 `trace_to_case.extract_human_reply` 同
    一处理,那边也是为了同一个原因)。
    """
    if isinstance(messages, str):
        try:
            messages = json.loads(messages) if messages else []
        except (ValueError, TypeError):
            messages = []
    if not isinstance(messages, list):
        return []

    turns: list[Turn] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            turns.append(Turn(user=str(msg.get("content") or "").strip()))
            continue
        if not turns:
            continue                      # 首条 user 之前的系统消息,不属于任何一轮
        if role == "assistant":
            for name in _tool_names(msg):
                if name not in turns[-1].tools:
                    turns[-1].tools.append(name)
            content = str(msg.get("content") or "").strip()
            if content:
                turns[-1].reply = content   # 取该轮**最后**一条有正文的回复
    return turns


# --------------------------------------------------------------------------
# 采样:哪些会话属于这个 skill
# --------------------------------------------------------------------------

def sessions_from_traces(skill_name: str, traces: list[dict]) -> list[str]:
    """按执行轨迹取该 skill 跑过的 session_id(强证据)。

    跳过 `variant=canary`:灰度候选跑出来的轨迹属于那一版候选,拿它给现行版本
    造门禁用例是张冠李戴(与 `failure_cases.collect_failures_by_skill` 同一条)。
    """
    out: list[str] = []
    for trace in traces or []:
        if trace.get("variant") == VARIANT_CANARY:
            continue
        if str(trace.get("skill_name") or "").strip() != skill_name:
            continue
        sid = trace.get("session_id")
        if sid and sid not in out:
            out.append(str(sid))
    return out


def skill_keywords(skill_md: str) -> list[str]:
    """从 SKILL.md 的 frontmatter description 里取声明的关键词。

    蒸馏出来的 skill 几乎都在 description 末尾写了「关键词：查订单、我的订单、…」
    ——那是**它自己声明的适用范围**,拿它去捞会话比另写一套猜测规则可靠。
    取不到就返回 [],调用方据此判定这个 skill 无法走关键词路(而不是去猜)。
    """
    from app.agent.skills.loader import _parse_frontmatter

    meta = _parse_frontmatter(skill_md or "")
    desc = str((meta or {}).get("description") or "")
    hit = _KEYWORD_PREFIX_RE.search(desc)
    if not hit:
        return []
    tail = hit.group(1)
    # 带引号时以引号为准:`关键词包括“优惠券”“有哪些券”等` 里,分隔符切法会切出
    # 「包括“优惠券”」这种带散文的碎片,而引号内的才是作者真正声明的词。
    quoted = _QUOTED_RE.findall(tail)
    words = quoted if quoted else [w.strip() for w in _KEYWORD_SPLIT_RE.split(tail)]
    # 一个字的"词"(如"退")会命中几乎所有会话,捞回来的样本与该 skill 无关
    return [w.strip("“”\"「」『』 ") for w in words
            if len(w.strip("“”\"「」『』 ")) >= 2]


def skill_actor(skill_md: str) -> str:
    """这份 skill 是买家侧还是卖家侧的(frontmatter 的 `actor`,缺省 buyer)。

    决定用哪一套工具全集去校验 `expected_tools`。`validator.known_tool_names()`
    是**买家侧**的(它刻意减掉了 `SELLER_ONLY_TOOLS`),拿它去过滤
    `daily-business-report` 这类卖家 skill,会把 `shop_overview`、
    `product_diagnostics` 全部当成"不存在的工具"剔掉,于是断言全空、用例全被丢,
    界面上显示 0 条——而真实原因不是"没素材",是**用错了工具全集**。
    """
    from app.agent.runtime_context import ACTOR_BUYER, ACTOR_SELLER
    from app.agent.skills.loader import _parse_frontmatter

    meta = _parse_frontmatter(skill_md or "")
    raw = str((meta or {}).get("actor") or ACTOR_BUYER).strip().lower()
    return raw if raw in (ACTOR_BUYER, ACTOR_SELLER) else ACTOR_BUYER


def tool_universe(actor: str) -> set[str]:
    """该 actor 实际能调到的工具名全集(用于剔除已下线/不属于本侧的工具名)。"""
    from app.agent.runtime_context import ACTOR_SELLER
    from app.agent.skills.validator import known_tool_names
    from app.agent.tools.registry import TOOL_DEFINITIONS

    if actor != ACTOR_SELLER:
        return known_tool_names()
    # 卖家侧:整张 registry(参谋/营销 Agent 能看到卖家专属工具,也能用通用工具)
    return {str((item.get("function") or {}).get("name"))
            for item in TOOL_DEFINITIONS
            if (item.get("function") or {}).get("name")}


def sessions_from_keywords(keywords: list[str], archives: list[dict]) -> list[str]:
    """按关键词在归档会话的**买家原话**里捞 session_id(弱证据,见模块 docstring)。

    只匹配 user 消息:助手回复里出现"退款"是因为它在回答,不说明买家问的是这件事。
    """
    if not keywords:
        return []
    out: list[str] = []
    for archive in archives or []:
        sid = archive.get("session_id")
        if not sid or sid in out:
            continue
        texts = [t.user for t in split_turns(archive.get("messages"))]
        if any(kw in text for kw in keywords for text in texts):
            out.append(str(sid))
    return out


# --------------------------------------------------------------------------
# 轮次 → 用例
# --------------------------------------------------------------------------

def _case_id(skill_name: str, user_text: str) -> str:
    """按买家原话取稳定 id —— 同一段对话反复合成会得到同一个 id。

    这条是"重跑不产生重复用例"的全部依据:落盘时按 id 去重,而 id 只由内容决定,
    不含时间戳、不含序号。换句话说合成是**幂等**的。
    """
    digest = hashlib.sha1(user_text.encode("utf-8")).hexdigest()[:8]
    return f"{SYNTH_ID_PREFIX}{skill_name}-{digest}"


def case_from_turn(skill_name: str, turn: Turn, *, known_tools: set[str],
                   human_reply: str = "") -> dict | None:
    """把一轮真实对话折成一条 EvalCase(dict 形状,可直接 json.dump)。

    断不出任何期望时返回 None —— 一条什么都不断言的用例**永远通过**,却每次门禁
    都要真跑一遍、花两次 token,还会把通过率往上抬(判据复用
    `trace_to_case.case_has_assertions`,那边是同一个理由)。
    """
    from app.evaluation.trace_to_case import case_has_assertions, keywords_from_reply

    user = (turn.user or "").strip()
    if len(user) < MIN_TURN_CHARS:
        return None
    if _SCAFFOLD_RE.search(user):
        return None      # 测试脚手架,见 `_SCAFFOLD_RE`

    # 只保留**当前 registry 里仍然存在**的工具名。合成源是真实轨迹,不存在 LLM 编
    # 工具名那种问题;要挡的是另一种:轨迹可能有几个月历史,期间工具被改名/下线,
    # 断言一个已经不存在的工具会让这条用例**永远无法通过**,而门禁 fail-closed,
    # 于是这个 skill 被一条过期用例永久钉死。
    tools = [t for t in turn.tools if t != LOAD_SKILL_TOOL and t in known_tools]

    case: dict = {
        "id": _case_id(skill_name, user),
        # 描述里就写明来历:这行会出现在评测报告与门禁日志里,是操作者最可能
        # 看到的地方。**不能只在文件级标注**——报告里一行行看过去时没人会回头
        # 去查这条用例来自哪个文件。
        "description": f"自动合成·未经人工审核 | {skill_name} | {user[:24]}",
        "turns": [user],
        "related_skills": [skill_name],
    }
    if tools:
        case["expected_tools"] = tools
    if human_reply:
        kws = keywords_from_reply(human_reply)
        if kws:
            case["expected_keywords"] = kws

    return case if case_has_assertions(case) else None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def synthesize_gate_cases(skill_name: str, *, traces: list[dict],
                          archives: list[dict], skill_md: str = "",
                          known_tools: set[str] | None = None,
                          max_cases: int = DEFAULT_MAX_CASES) -> tuple[list[dict], dict]:
    """为一个 skill 合成门禁用例。返回 `(cases, stats)`,不落盘、不碰 DB、不调 LLM。

    纯函数式(全部输入由调用方注入)是刻意的:门禁用例是裁判尺,它的生成过程
    必须能在单测里完整复现,不能依赖"跑的时候库里正好有什么"。

    `stats` 报出**丢了多少、为什么**——与 `collect_reflow_cases(with_stats=True)`
    同一条纪律:少给了东西必须说出来,否则"只合成到 1 条"读起来像"线上就这点素材"。
    """
    actor = skill_actor(skill_md)
    known = tool_universe(actor) if known_tools is None else known_tools
    by_session = {a.get("session_id"): a for a in (archives or []) if a.get("session_id")}

    trace_sids = [s for s in sessions_from_traces(skill_name, traces) if s in by_session]
    keywords = skill_keywords(skill_md)
    keyword_sids = [s for s in sessions_from_keywords(keywords, archives or [])
                    if s not in trace_sids]

    cases: list[dict] = []
    provenance: list[dict] = []
    seen_ids: set[str] = set()
    dropped_no_assertion = 0
    dropped_too_short = 0

    for source, sids in ((SOURCE_TRACE, trace_sids), (SOURCE_KEYWORD, keyword_sids)):
        for sid in sids:
            if len(cases) >= max_cases:
                break
            archive = by_session[sid]
            human_reply = _human_reply(archive)
            for turn in split_turns(archive.get("messages")):
                if len(cases) >= max_cases:
                    break
                if source == SOURCE_KEYWORD and keywords and not any(
                        kw in turn.user for kw in keywords):
                    continue      # 关键词路只取**命中的那一轮**,不把整段会话铺开
                if len((turn.user or "").strip()) < MIN_TURN_CHARS:
                    dropped_too_short += 1
                    continue
                case = case_from_turn(skill_name, turn, known_tools=known,
                                      human_reply=human_reply)
                if case is None:
                    dropped_no_assertion += 1
                    continue
                if case["id"] in seen_ids:
                    continue          # 同一句话在多个会话里重复出现,只留一条
                seen_ids.add(case["id"])
                cases.append(case)
                provenance.append({"case_id": case["id"], "session_id": sid,
                                   "source": source})

    stats = {
        "skill": skill_name,
        "actor": actor,
        "kept": len(cases),
        "sessions_scanned": len(by_session),
        "from_trace": sum(1 for p in provenance if p["source"] == SOURCE_TRACE),
        "from_keyword": sum(1 for p in provenance if p["source"] == SOURCE_KEYWORD),
        "keywords": keywords,
        "dropped_no_assertion": dropped_no_assertion,
        "dropped_too_short": dropped_too_short,
        "provenance": provenance,
        "note": ("断言全部来自事实:expected_tools 取自该轮真实调用过的工具,"
                 "expected_keywords 取自人工坐席回复里的已知词表命中,买家原话逐字照抄。"
                 "关键词来源的用例只说明「挑了哪些真实会话」,不参与任何断言。"),
    }
    return cases, stats


def _human_reply(archive: dict) -> str:
    """该会话最后一条人工坐席回复(没有则空串)。判据与 golden_corpus 同源。"""
    from app.evaluation.trace_to_case import extract_human_reply

    try:
        return extract_human_reply(archive)
    except Exception:  # noqa: BLE001 单条脏数据不该让整批合成失败
        return ""


# --------------------------------------------------------------------------
# 落盘 / 读取
# --------------------------------------------------------------------------

def synth_dir(root: str | Path | None = None) -> Path:
    from app.config.settings import settings

    return Path(root if root is not None else settings.eval_synth_cases_dir)


def synth_path(skill_name: str, root: str | Path | None = None) -> Path:
    """合成用例的落盘路径。skill 名非法时抛 ValueError。

    skill 名一路来自 LLM 生成的 frontmatter(素材是可被提示注入的顾客对话),
    这里要写文件,`..` 或路径分隔符会让写入逃出目录——与 `build_shadow_dir`
    同一条防线,复用同一个判定。
    """
    if not is_safe_skill_name(skill_name):
        raise ValueError(f"非法 skill 名(必须是单个安全路径段): {skill_name!r}")
    return synth_dir(root) / f"{skill_name}.json"


def save_synth_cases(skill_name: str, cases: list[dict], stats: dict | None = None,
                     root: str | Path | None = None) -> Path:
    """写 `cases_synth/<skill>.json`。**整份覆盖**,不与旧文件合并。

    合成是幂等的(id 只由买家原话决定),重跑得到的是同一批 id;而覆盖能让"轨迹
    变多了、素材变好了"如实反映到用例上。合并反而会把早期从坏素材里合成的用例
    永久留在裁判尺里。
    """
    path = synth_path(skill_name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "synthetic": True,
        "skill": skill_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "note": ("本文件由 app.agent.skills.case_synthesis 从真实会话自动合成,"
                 "**未经人工审核**;只用于 skill 转正门禁,不进回归基线 cases.json。"),
        "stats": stats or {},
        "cases": cases,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_synth_cases(skill_name: str, root: str | Path | None = None) -> list:
    """读一个 skill 的合成用例,返回 `list[EvalCase]`;文件不存在/坏掉返回 []。

    坏文件按"没有"处理而不是抛:合成用例是**增量**能力,它坏掉时应该退回到
    "只有人工用例"这个原状态,而不是让门禁连人工用例也跑不了。
    """
    from app.evaluation.dataset import EvalCase

    try:
        path = synth_path(skill_name, root)
    except ValueError:
        return []
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [EvalCase(**item) for item in data.get("cases") or []]
    except Exception:  # noqa: BLE001 坏文件 = 当作没有合成用例,不拖垮门禁
        return []


def load_all_synth_cases(root: str | Path | None = None) -> list:
    """读全部 skill 的合成用例(`default_eval_fn` 按 case_id 取用例时要用)。"""
    from app.evaluation.dataset import EvalCase

    directory = synth_dir(root)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.extend(EvalCase(**item) for item in data.get("cases") or [])
        except Exception:  # noqa: BLE001 单个坏文件不影响其余
            continue
    return out


def synthesize_and_save(skill_name: str, *, db=None, skills_dir: str | None = None,
                        candidates_dir: str | None = None,
                        max_cases: int = DEFAULT_MAX_CASES,
                        trace_limit: int = 500, archive_limit: int = 300,
                        root: str | Path | None = None) -> dict:
    """取数 → 合成 → 落盘的一条龙(CLI 与看门狗的入口)。

    正式目录里没有该 skill 时到 `_candidates/` 找 SKILL.md —— **新候选正是最需要
    合成用例的那一类**,而它此刻还没进正式目录。
    """
    from app.config.settings import settings

    if db is None:
        from app.db import get_db
        db = get_db()

    skills_dir = skills_dir or settings.skills_dir
    candidates_dir = candidates_dir or str(Path(skills_dir) / "_candidates")

    skill_md = ""
    for base in (skills_dir, candidates_dir):
        path = Path(base) / skill_name / "SKILL.md"
        if path.exists():
            skill_md = path.read_text(encoding="utf-8")
            break

    traces = db.list_skill_traces(skill_name=skill_name, limit=trace_limit)
    archives = db.list_recent_archives(limit=archive_limit)

    cases, stats = synthesize_gate_cases(
        skill_name, traces=traces, archives=archives, skill_md=skill_md,
        max_cases=max_cases)
    stats["skill_md_found"] = bool(skill_md)
    if not cases:
        # 一条都合不出来时**不写空文件**:留着上一次的结果比用一份空文件把它
        # 覆盖掉更好(素材可能只是这一次没取到)。同时如实报出来。
        stats["saved"] = None
        return stats
    stats["saved"] = str(save_synth_cases(skill_name, cases, stats, root))
    return stats
