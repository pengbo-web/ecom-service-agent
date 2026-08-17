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
# 这四条是改造前的基线,不受 L3② 扩展开关影响,恒生效。
_RULE_TABLE = [
    ("闲聊寒暄", re.compile(r"^(嗯+|哦+|噢|好的?|好嘞|行吧?|可以|ok|okay|收到|明白了?|知道了)[\s!！~。.,，]*$", re.IGNORECASE), None),
    ("闲聊寒暄", re.compile(r"^(你好|您好|哈喽|嗨|在吗|再见|拜拜|谢谢|感谢)[\s!！~。.,，]*$", re.IGNORECASE), None),
    # 纯订单号≈查单/物流意图,定向 midsale——粘在 presale 会缺 query_order/query_logistics 工具
    ("订单事务", re.compile(r"^ORD-\d{8}-\d{3}$", re.IGNORECASE), "midsale"),
    ("转人工", re.compile(r"^(转人工|人工客服|找人工|叫真人|人工)[\s!！~。.]*$"), None),
]

# L3②:扩展规则(settings.qu_fast_path_extended_enabled 门控,关=只保留上面 4 条基线)。
# 取材:session_archive 里真实出现过的短句(≤20 字)+ 常见问题FAQ.md 的原句措辞。
# 每条都按"宁漏勿错杀"逐一核实过——不匹配任何可能被错误分流到别的领域画像/
# 因 need_kb=False 而漏检索的变体,只收"改写/加长后仍不会引出不同处理方式"的
# 精确锚定句式(^...$),模棱两可的一律不收(如"那运费呢"这类要靠上文指代消解
# 的追问、"我要退货,一步步教我怎么操作"这类既可能是问政策也可能是要发起
# 退货流程的双关句都没有收)。
#
# domain 取值的取舍:
#   - 订单查询("查我的订单"类):list_user_orders 三域画像都有,不存在"分错域
#     就少工具"的风险,因此不强制域,domain=None 交粘性路由,最大化安全边际;
#   - 议价("能便宜点吗,帮我砍砍价"):negotiate_price 只在 presale 画像里,
#     不强制域就可能真的少了这一步要用的工具——理由与既有"纯订单号→midsale"
#     那条完全同构,故此处沿用同一标准强制 domain="presale";
#   - 政策类直击原句("退货政策是什么"等):同一句话可能发生在售前(决定要不要
#     买之前先问清政策)或售后(正在退货过程中问),强制域有真实的"分错域丢
#     工具"风险,因此保留 domain=None——这类规则省的是 3.2s 的 QU LLM 调用,
#     不省域判定,domain 仍交给粘性路由(与关这条扩展开关前的兜底路径一致)。
_EXTENDED_RULE_TABLE = [
    # 订单查询类:"查(一下)?...订单(都)?(有哪些)?" / "我(的)?(都)?有哪些订单" 两种语序,
    # 均为纯粹的"列出我的订单"动作,靠 list_user_orders 工具即可,不涉政策检索。
    ("订单事务",
     re.compile(r"^(帮我)?查(一下)?(我的|我|你)?(名下的?)?订单(都)?(有哪些)?[？?！!。.]*$"),
     None, False),
    ("订单事务",
     re.compile(r"^我(的)?(名下)?(都)?(有哪些订单|订单有哪些)[？?！!。.]*$"),
     None, False),
    # 议价:negotiate_price 是 presale 专属工具,本轮就要用,强制域(理由见上)。
    ("商品咨询",
     re.compile(r"^(这个|这|该商品)?能便宜(一)?点(吗)?[,，]?(帮我)?砍(一)?砍价[？?！!。.]*$"),
     "presale", False),
    # 商品信息/价格/规格类询问:靠商品上下文/查询商品工具即可回答,不查知识库。
    ("商品咨询", re.compile(r"^这(个|是)?(是)?什么[？?！!~。.]*$"), None, False),
    ("商品咨询", re.compile(r"^多少钱[？?！!。.]*$"), None, False),
    ("商品咨询", re.compile(r"^(这个)?商品有(哪些|什么)规格(和属性)?[？?！!。.]*$"), None, False),
    ("商品咨询", re.compile(r"^你有(什么|哪些)商品[？?！!。.]*$"), None, False),
    # 政策类高频原句:自包含(无需消解上文指代),need_kb=True + kb_query=原句,
    # 省的是 QU 那次 LLM 调用,检索/域判定路径与老兜底一致不变。
    ("政策咨询", re.compile(r"^(退货|退换货)(政策|流程|完整流程)是(什么|啥)[？?！!。.]*$"), None, True),
    ("政策咨询", re.compile(r"^怎么退货[？?！!。.]*$"), None, True),
    ("政策咨询", re.compile(r"^价保多久[？?！!。.]*$"), None, True),
    ("政策咨询", re.compile(r"^退款(多久到账|怎么(还)?没到账)[？?！!。.]*$"), None, True),
    ("政策咨询", re.compile(r"^(大件家电)?退货运费怎么算[？?！!。.]*$"), None, True),
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

from prompts import get as _get_prompt

_QU_TEMPLATE = _get_prompt("query_understanding/system_prompt")


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


def _match_rules(text: str) -> QueryUnderstanding | None:
    """依次扫基线表(恒生效)与扩展表(受 settings.qu_fast_path_extended_enabled
    门控);两表内均按声明顺序取第一个命中,不命中返回 None 交给 LLM 层。"""
    for intent, pat, domain in _RULE_TABLE:
        if pat.match(text):
            return QueryUnderstanding(domain=domain, intent=intent,
                                      need_kb=False, source="rule")
    if settings.qu_fast_path_extended_enabled:
        for intent, pat, domain, need_kb in _EXTENDED_RULE_TABLE:
            if pat.match(text):
                return QueryUnderstanding(
                    domain=domain, intent=intent, need_kb=need_kb,
                    kb_query=(text if need_kb else None), source="rule")
    return None


def understand(user_input: str, history: list[dict], client, model: str,
               outreach_hint: str = "") -> QueryUnderstanding:
    """三层查询理解;任何失败兜底为"多检索、走默认"。

    `outreach_hint` 是一行确定性前情:店铺刚主动给这位顾客发过一条什么情境的
    消息(见 `outreach_context.router_hint`)。

    **为什么必须显式传进来**:下面的 `context` 只取最近 5 条 **user** 消息,
    而营销触达是 assistant 消息——它对这一步**完全不可见**。实测后果:一条催付款
    触达之后,买家回「好啊,帮我看看」,这一步判成 `intent=订单事务 → aftersale`,
    而 aftersale 手上没有 `query_coupons`、没有 `place_order`,既讲不清随触达发出
    的那张券,也帮不了买家把这单付掉。

    补一行前情比在编排层做"回落偏好"有效得多:回落只在这一步**判不出域**时才生效,
    而它几乎总能判出一个域——只是判错了。让它拿到那个缺失的事实,判定权仍在这里。

    **已知边界**:短句先走 `_match_rules`(确定性规则,`source=rule`),那条路不看
    前情。可接受——规则是逐字匹配的固定表,不是猜;真要收窄,该改的是规则表本身。
    """
    text = (user_input or "").strip()
    if len(text) <= _RULE_MAX_CHARS:
        matched = _match_rules(text)
        if matched is not None:
            return matched

    users = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    context = "\n".join(f"- {u}" for u in users[-5:] if u) or "(无)"
    if outreach_hint:
        # 拼在最前:它是这一轮的前提,而不是"最近对话"里的一条。
        context = f"{outreach_hint}\n{context}"
    req = dict(
        model=model, temperature=0.0, max_tokens=200,   # 多两个字段(emotion/emotion_level)
        messages=[{"role": "user",
                   "content": _QU_TEMPLATE.replace(
                       "{{_EMOTION_RUBRIC}}", _EMOTION_RUBRIC
                   ).format(context=context, user_input=text)}],
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
