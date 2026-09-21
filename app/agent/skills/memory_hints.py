"""WS1 skill 记忆两层化:在线零决策标记通道(技术方案 §2)。

归因(`attribution.py`)的输入面是 outcome ∈ {tool_error, handoff} 的轨迹,但一类
"值得学"的信号发生在 **success 轮**:买家纠正了客服、KB 没命中却硬答了政策。
本模块把这些信号**标记**下来落 `skill_memory_hints` 表,供失败样本采样排序与
进化循环看板报数。

**只标记,不判定。** hint 不参与归因类别判定、不参与门禁、不参与看门狗——
判定权仍归 `attribution.py` 的规则表。这是 41 条归属拦截实证换来的纪律:
在线"自主判断什么值得学"会把不可修的信号(压测用户命中归属墙被按设计拒绝)
编码成知识。顺序保证:即使某轮同时带 hint 与归属拒绝特征,归因规则②仍先命中
capability_limit,hint 在该轮惰性化(写进表但采样不优先)——仓库级测试
`tests/test_memory_hints.py::test_hint_does_not_change_attribution` 钉死这条。

参考对齐:参考架构的"skill 记忆:Agent 自主判断什么值得记忆"在本项目的形态是
"在线规则标记 + 离线规则判定"两层,借其感知、不借其判定权。

fail 方向:生产者(`chat.py::_record_memory_hints`)整段 try/except 吞掉,
埋点失败绝不影响回话;本模块函数自身**不吞异常**(与 `record_skill_trace`
同姿态),离线复算与测试里丢标记要能被发现。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

# ---------- 类别与来源 ----------

KIND_USER_CORRECTION = "user_correction"      # 买家下一轮纠正了客服上一轮的回答
KIND_KB_MISS_POLICY = "kb_miss_policy"        # 门控说要检索、检索没命中、意图是政策类
KIND_PIGGYBACK_NOTE = "piggyback_note"        # 预留:会话末 LTM 抽取捎带(开关默认关,M1 不产)

SOURCE_RULE = "rule"
SOURCE_LLM_PIGGYBACK = "llm_piggyback"

# ---------- 用户纠正模式表 ----------
# 取材纪律与 understanding._EXTENDED_RULE_TABLE 同源:**宁漏勿错杀**,只收整句锚定
# 的短纠正句。不收无锚定子串——「这个尺寸不对吗」是正常咨询,「不对吧,我觉得挺好」
# 是肯定语气,子串匹配会把它们全标成纠正,而噪声标记会让采样排序失去意义。
_CORRECTION_MAX_CHARS = 20

_CORRECTION_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("denial", re.compile(r"^不对(啊|呀|哦|噢)?[。,，!！~～]?$")),
    ("denial_then_fix", re.compile(r"^不对[。,，!！](.{1,14})$")),
    ("you_said_wrong", re.compile(r"^你说错了[。,，!！]?$")),
    ("not_this_meaning", re.compile(r"^不是这个意思[。,，!！]?$")),
    ("i_asked", re.compile(r"^我问的是.{1,14}$")),
    ("you_mixed_up", re.compile(r"^你搞(错|混)了[。,，!！]?$")),
    ("irrelevant_answer", re.compile(r"^答非所问[。,，!！]?$")),
)

# 政策类 QU 意图:只有这类意图下"检索没命中却给了回答"才构成"无依据硬答政策"。
# 取 QU 的意图标签真源(chat.py::_QU_INTENT_MAP 的七类之一),不另抄语义近似的词表。
POLICY_INTENTS = frozenset({"政策咨询"})


def detect_user_correction(text: str) -> str | None:
    """整句锚定判定买家这句话是不是在纠正上一轮回答。命中返回模式名,否则 None。"""
    if not text:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > _CORRECTION_MAX_CHARS:
        return None
    for name, pattern in _CORRECTION_PATTERNS:
        if pattern.match(stripped):
            return name
    return None


def detect_kb_miss_policy(qu, recall) -> bool:
    """本轮是否"门控说要检索、走了统一召回、却零命中",且意图是政策类。

    `recall=None` 判 False:本轮没走统一召回(FAQ 缓存秒答等路径 `_turn_recall`
    保持 None),零命中无从谈起,标了就是噪声。`kb_hits` 非空判 False:检索到
    政策后如实转述是**正确行为**,不该被标(commitment_guard 同一条理由)。
    """
    if qu is None or not getattr(qu, "need_kb", False):
        return False
    if getattr(qu, "intent", "") not in POLICY_INTENTS:
        return False
    if recall is None:
        return False
    if getattr(recall, "kb_hits", None):
        return False
    return True


# ---------- 落库与报数 ----------

def _hint_id(session_id: str, skill_name: str, kind: str, detail: str) -> str:
    """同会话同技能同类同细节幂等:同一轮重跑不产生第二条标记。"""
    raw = f"{session_id}|{skill_name}|{kind}|{detail}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def record_hint(session_id: str, skill_name: str, kind: str,
                source: str = SOURCE_RULE, detail: str = "", db=None) -> str:
    """写一条标记,返回 hint_id。不吞异常——吞异常是生产者(chat 埋点)的职责。"""
    if db is None:
        from app.db import get_db
        db = get_db()
    from app.agent.runtime_context import get_traffic_source
    hint_id = _hint_id(session_id, skill_name, kind, detail)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO skill_memory_hints "
            "(hint_id, session_id, skill_name, kind, source, traffic, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (hint_id, session_id, skill_name or "", kind, source,
             get_traffic_source(), detail or "",
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return hint_id


def hint_stats(skill_name: str | None = None, db=None):
    """不带 skill:`{skill: {kind: n}}`(看板用);带 skill:`{kind: n}`(采样排序用)。

    两个形状共用这一个读取口,防口径分叉。
    """
    if db is None:
        from app.db import get_db
        db = get_db()
    sql = ("SELECT skill_name, kind, COUNT(*) AS n FROM skill_memory_hints"
           + (" WHERE skill_name = ?" if skill_name else "")
           + " GROUP BY skill_name, kind")
    params = (skill_name,) if skill_name else ()
    conn = db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        item = dict(row)
        out.setdefault(str(item["skill_name"]), {})[str(item["kind"])] = int(item["n"])
    if skill_name is not None:
        return out.get(skill_name, {})
    return out
