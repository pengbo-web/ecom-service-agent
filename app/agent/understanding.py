"""统一查询理解节点(Query Understanding):意图识别+路由+检索门控+查询改写,一次调用。

对齐生产客服(阿里小蜜语义理解层/五步工作流①②):上游一个节点输出
domain/intent/need_kb/kb_query,下游各取所需——orchestrator 拿 domain 切画像,
召回层拿 need_kb/kb_query 决定检索。不再让"查不查知识库"依赖模型自觉,
也不再为路由和改写各花一次 LLM 调用(原两次上游调用合并为至多一次)。

三层结构:
  ① 规则快筛(0 成本,≤20 字):确认语气词/纯订单号/转人工 → 直接判定不调 LLM
     (纯问候/感谢在更上游的 API fast_path 已秒回,到不了这里;规则宁漏勿错杀)
  ② LLM 查询理解:一次调用输出严格 JSON
  ③ 兜底:任何失败 → need_kb=True + kb_query=原句 + domain=None(粘性路由接管)
     ——错误方向永远偏"多检索、走默认",绝不阻塞回复主流程。
"""

import json
import logging
import re
from dataclasses import dataclass

from app.config.settings import settings

logger = logging.getLogger(__name__)

VALID_DOMAINS = {"presale", "midsale", "aftersale"}
_RULE_MAX_CHARS = 20


@dataclass
class QueryUnderstanding:
    domain: str | None = None      # None=未判定,orchestrator 沿用上轮路由(粘性)
    intent: str = "其他"           # 政策咨询/商品咨询/订单事务/闲聊寒暄/投诉/转人工/其他
    need_kb: bool = True           # 检索门控:False=本轮跳过 KB 预召回
    kb_query: str | None = None    # need_kb 时的自包含检索查询(已消解指代/省略)
    source: str = "llm"            # rule/llm/fallback,供 route 事件与观测


# (意图, 判定正则, 规则可确定的 domain——None=交给粘性路由)
_RULE_TABLE = [
    ("闲聊寒暄", re.compile(r"^(嗯+|哦+|噢|好的?|好嘞|行吧?|可以|ok|okay|收到|明白了?|知道了)[\s!！~。.,，]*$", re.IGNORECASE), None),
    ("闲聊寒暄", re.compile(r"^(你好|您好|哈喽|嗨|在吗|再见|拜拜|谢谢|感谢)[\s!！~。.,，]*$", re.IGNORECASE), None),
    # 纯订单号≈查单/物流意图,定向 midsale——粘在 presale 会缺 query_order/query_logistics 工具
    ("订单事务", re.compile(r"^ORD-\d{8}-\d{3}$", re.IGNORECASE), "midsale"),
    ("转人工", re.compile(r"^(转人工|人工客服|找人工|叫真人|人工)[\s!！~。.]*$"), None),
]

_QU_PROMPT = """你是电商客服的查询理解模块。分析用户最新消息,输出严格 JSON(不要任何解释、不要代码块):
{{"domain": "presale|midsale|aftersale", "intent": "政策咨询|商品咨询|订单事务|闲聊寒暄|投诉|其他", "need_kb": true或false, "kb_query": "自包含检索查询或null"}}

domain(路由,选最主要的):
- presale: 下单前——商品推荐/商品信息/价格/库存/活动优惠/优惠券/议价
- midsale: 订单进行中——查订单/物流/催发货/改收货地址/取消订单
- aftersale: 收货后或交易后——退换货/退款/发票/质量投诉/赔偿;打招呼闲聊账户问题默认归此

need_kb(是否需要检索平台知识库):
- true: 涉及平台政策/规则/流程/时效/费用/权益/售后标准(如"运费谁出""价保多久""怎么退货""发票怎么开")
- false: 纯订单操作(查单号/物流)/纯商品参数/闲聊寒暄/情绪宣泄——这些靠工具或对话即可

kb_query(need_kb=true 时必填):结合最近对话把指代和省略补全成自包含查询,
如上文聊退货、用户问"那运费呢?"→"退货运费谁承担";need_kb=false 时为 null。

最近对话(用户侧):
{context}

用户最新消息:{user_input}"""


def understand(user_input: str, history: list[dict], client, model: str) -> QueryUnderstanding:
    """三层查询理解;任何失败兜底为"多检索、走默认"。"""
    text = (user_input or "").strip()
    if len(text) <= _RULE_MAX_CHARS:
        for intent, pat, domain in _RULE_TABLE:
            if pat.match(text):
                return QueryUnderstanding(domain=domain, intent=intent,
                                          need_kb=False, source="rule")

    users = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    context = "\n".join(f"- {u}" for u in users[-5:] if u) or "(无)"
    req = dict(
        model=model, temperature=0.0, max_tokens=150,
        messages=[{"role": "user",
                   "content": _QU_PROMPT.format(context=context, user_input=text)}],
    )
    try:
        try:
            resp = client.chat.completions.create(
                timeout=settings.recall_kb_timeout_s, **req)
        except TypeError:      # 代理不接受 per-request timeout
            resp = client.chat.completions.create(**req)
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        domain = data.get("domain")
        if domain not in VALID_DOMAINS:
            domain = None          # 非法 domain 不硬猜,交给粘性路由
        need_kb = bool(data.get("need_kb", True))
        kb_query = (str(data.get("kb_query") or "")).strip() or None
        if need_kb and not kb_query:
            kb_query = text        # 说要检索但没给查询 → 原句兜底
        return QueryUnderstanding(
            domain=domain, intent=str(data.get("intent") or "其他"),
            need_kb=need_kb, kb_query=kb_query if need_kb else None, source="llm")
    except Exception:
        logger.warning("query understanding failed, fallback to need_kb=True", exc_info=True)
        return QueryUnderstanding(source="fallback", kb_query=text or None)
