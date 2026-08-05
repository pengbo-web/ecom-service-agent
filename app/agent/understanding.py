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
VALID_EMOTIONS = {"neutral", "unhappy", "angry"}
_RULE_MAX_CHARS = 20
_EMOTION_LEVEL_MAX = 3
# Finding-3:level→标签的阈值只在这三个常量定义一次——prompt 里的判据描述
# (下方 _EMOTION_RUBRIC)和 _parse_emotion 的一致性校验都从这里派生,不再
# 各自各处硬编码"1-2"/"3"这组数字,两处不可能再各自漂移。
# 前端(MetadataChips.tsx)不复用这套数字:它不比较 emotion_level,只消费
# 本模块已经判定并校验过一致性的 emotion 标签本身——权威在后端,前端只展示。
_EMOTION_UNHAPPY_MIN = 1   # unhappy 下界(含)
_EMOTION_ANGRY_MIN = 3     # angry(激烈)下界(含),当前等于上限——仅 level==3 算激烈


@dataclass
class QueryUnderstanding:
    domain: str | None = None      # None=未判定,orchestrator 沿用上轮路由(粘性)
    intent: str = "其他"           # 政策咨询/商品咨询/订单事务/闲聊寒暄/投诉/转人工/其他
    need_kb: bool = True           # 检索门控:False=本轮跳过 KB 预召回
    kb_query: str | None = None    # need_kb 时的自包含检索查询(已消解指代/省略)
    source: str = "llm"            # rule/llm/fallback,供 route 事件与观测
    emotion: str = "neutral"       # neutral/unhappy/angry;非法/缺失一律 neutral,不硬猜
    emotion_level: int = 0         # 0..3,0=中性,3=激烈;与 emotion 同步回落


# (意图, 判定正则, 规则可确定的 domain——None=交给粘性路由)
_RULE_TABLE = [
    ("闲聊寒暄", re.compile(r"^(嗯+|哦+|噢|好的?|好嘞|行吧?|可以|ok|okay|收到|明白了?|知道了)[\s!！~。.,，]*$", re.IGNORECASE), None),
    ("闲聊寒暄", re.compile(r"^(你好|您好|哈喽|嗨|在吗|再见|拜拜|谢谢|感谢)[\s!！~。.,，]*$", re.IGNORECASE), None),
    # 纯订单号≈查单/物流意图,定向 midsale——粘在 presale 会缺 query_order/query_logistics 工具
    ("订单事务", re.compile(r"^ORD-\d{8}-\d{3}$", re.IGNORECASE), "midsale"),
    ("转人工", re.compile(r"^(转人工|人工客服|找人工|叫真人|人工)[\s!！~。.]*$"), None),
]

# Finding-3:情绪判据的文字描述(下面这一段)由 _EMOTION_UNHAPPY_MIN/
# _EMOTION_ANGRY_MIN 拼出,不再在 prompt 里单独硬编码"1-2"/"3"——与
# _parse_emotion 的一致性校验共用同一份数字,改阈值只改一处常量。
_EMOTION_RUBRIC = (
    "emotion/emotion_level(判定用户情绪,不是意图):\n"
    "- neutral 平静(level 0):正常咨询/闲聊,没有不满情绪\n"
    f"- unhappy 不满(level {_EMOTION_UNHAPPY_MIN}-{_EMOTION_ANGRY_MIN - 1}):"
    "抱怨、催促、语气不耐烦,但未失控\n"
    f"- angry 激烈(level {_EMOTION_ANGRY_MIN}):咒骂、威胁投诉/曝光、情绪失控\n"
    "只描述用户当下语气,不要因为话题是投诉就自动判高;语气平和的投诉仍是 neutral/低 level。"
)

_QU_PROMPT = """你是电商客服的查询理解模块。分析用户最新消息,输出严格 JSON(不要任何解释、不要代码块):
{{"domain": "presale|midsale|aftersale", "intent": "政策咨询|商品咨询|订单事务|闲聊寒暄|投诉|其他", "need_kb": true或false, "kb_query": "自包含检索查询或null", "emotion": "neutral|unhappy|angry", "emotion_level": 0到3的整数}}

domain(路由,选最主要的):
- presale: 下单前——商品推荐/商品信息/价格/库存/活动优惠/优惠券/议价
- midsale: 订单进行中——查订单/物流/催发货/改收货地址/取消订单
- aftersale: 收货后或交易后——退换货/退款/发票/质量投诉/赔偿;打招呼闲聊账户问题默认归此

need_kb(是否需要检索平台知识库):
- true: 涉及平台政策/规则/流程/时效/费用/权益/售后标准(如"运费谁出""价保多久""怎么退货""发票怎么开")
- false: 纯订单操作(查单号/物流)/纯商品参数/闲聊寒暄/情绪宣泄——这些靠工具或对话即可

kb_query(need_kb=true 时必填):结合最近对话把指代和省略补全成自包含查询,
如上文聊退货、用户问"那运费呢?"→"退货运费谁承担";need_kb=false 时为 null。

""" + _EMOTION_RUBRIC + """

最近对话(用户侧):
{context}

用户最新消息:{user_input}"""


def _label_for_level(level: int) -> str:
    """N2 Finding-3:level→标签的唯一权威映射,_parse_emotion 的一致性校验与
    上面 _EMOTION_RUBRIC 的 prompt 文案共用这两个阈值常量,不再各自硬编码。"""
    if level >= _EMOTION_ANGRY_MIN:
        return "angry"
    if level >= _EMOTION_UNHAPPY_MIN:
        return "unhappy"
    return "neutral"


def _parse_emotion(data: dict) -> tuple[str, int]:
    """从 LLM JSON 里取 emotion/emotion_level,白名单校验;任一非法则整体回落
    neutral/0——不硬猜,与既有 domain 非法即 None 同口径(不去猜"多半是不满")。

    Finding-2:emotion 与 emotion_level 分开看各自合法,不代表两者构成的
    "对"合法——LLM 仍可能吐出 ("neutral", 3) 或 ("angry", 0) 这类自相矛盾的
    组合(标签与强度不一致)。若在这里放过,下游(turn_signals 落库、前端
    MetadataChips 直接消费 emotion 标签渲染)就会显示一个连后端自己都不
    自洽的情绪。选择在解析边界就用 _label_for_level 校验两者是否一致、
    不一致则整体回落 neutral/0——与既有"任一字段非法即回落"同一个口径
    (数据自相矛盾本质上也是一种非法,不做"信 level 还是信 emotion"式的
    偏袒式硬猜),这样任何下游消费者拿到的 (emotion, emotion_level) 必定
    自洽,不必再各自重复校验一遍。"""
    emotion = data.get("emotion")
    if emotion not in VALID_EMOTIONS:
        return "neutral", 0
    try:
        level = int(data.get("emotion_level", 0))
    except (TypeError, ValueError):
        return "neutral", 0
    if not (0 <= level <= _EMOTION_LEVEL_MAX):
        return "neutral", 0
    if emotion != _label_for_level(level):
        return "neutral", 0
    return emotion, level


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
        model=model, temperature=0.0, max_tokens=200,   # 多两个字段(emotion/emotion_level)
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
        emotion, emotion_level = _parse_emotion(data)
        return QueryUnderstanding(
            domain=domain, intent=str(data.get("intent") or "其他"),
            need_kb=need_kb, kb_query=kb_query if need_kb else None, source="llm",
            emotion=emotion, emotion_level=emotion_level)
    except Exception:
        logger.warning("query understanding failed, fallback to need_kb=True", exc_info=True)
        return QueryUnderstanding(source="fallback", kb_query=text or None)
