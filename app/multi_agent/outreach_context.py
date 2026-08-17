"""把「刚刚发出的营销触达」渲染成买家侧可安全使用的前情提示。

要解决的是一个实测缺陷:**营销把一条消息投进买家会话,然后退场。**

投递走 `_append_agent_reply(..., intent="growth_outreach")`,买家看到的就是一条普通
客服消息(信封与模型正常输出同构,界面上分不出来)。而那条消息结尾常常是个问句——
实测 draft 41:「需要帮您看看是卡在哪儿了吗?」

买家回一句"好啊",接手的是买家侧的某个画像,它能看到那句话(在历史里),但**不知道**:

    这是店铺主动发的,不是顾客先开口   → 会让顾客重复说明来意
    随触达发了券 SHOE30                → 顾客问"券怎么用"时只能靠编
    关联的是哪一单                     → 又要问一遍订单号
    商机类型是"下单未支付"             → 路由器只能靠那七个字猜

所以这不是"缺一个营销画像"——presale 手上早就有 query_coupons / query_product /
place_order / add_to_cart,回答营销问题和推动付款本来就是它的活。缺的只是**把上下文
带过去**。

---

**为什么走确定性渲染,和 `buyer_hints` 同一套纪律**

草稿里有两个字段**绝不能进买家侧 prompt**:

- `reason`(审批依据):它是给店主看的经营判断,形如「该款跑鞋存在普遍性的尺码偏大
  问题,导致 6 笔退款…建议在详情页添加尺码提醒」。进了买家上下文,客服就可能说出
  "我们这款鞋退款率确实偏高"——把内部经营数据以"客服亲口承认"的形式泄露出去。
- `needs_review_reason` / `correlation_id` 等内部字段:同理,顾客不该知道。

而 `content` **不需要**注入:那条消息已经在会话历史里,画像本来就看得见。重复注一遍
只会让同一句话在上下文里出现两次。

所以这里注入的只有三样,全部经过固定映射:商机类型(查 `OPPORTUNITY_KINDS` 的中文
标签)、订单号、券码。**没有任何数字指标、没有任何 LLM 生成内容、没有诊断结论原文。**
泄露风险从"靠模型自觉"变成"结构上不可能"——能拿到的东西里根本没有可泄露的。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: 商机类型 → 这一轮**更可能**该由哪个买家侧画像接。
#:
#: 这是给路由器的**确定性提示**,不是硬覆盖(见 `preferred_profile` 的说明)。
#: 依据是"买家对这类触达的回应最可能想干什么":
#:   催付款 / 弃单 / 议价 / 咨询未下单 → 想付款或还在挑 → presale(它有
#:       query_coupons / place_order / add_to_cart / negotiate_price)
#:   已付款待发货 / 已发货待关怀       → 想知道货到哪了 → midsale(query_order /
#:       query_logistics / expedite_shipping)
#:   已签收未评价                      → 多半是对到手的商品有话说 → aftersale
_PREFERRED_PROFILE: dict[str, str] = {
    "unpaid_order": "presale",
    "abandoned_cart": "presale",
    "stalled_bargain": "presale",
    "consulted_no_order": "presale",
    "stale_pending_order": "midsale",
    "shipped_no_care": "midsale",
    "delivered_no_review": "aftersale",
}


def outreach_window_hours() -> float:
    from app.config.settings import settings
    return float(getattr(settings, "outreach_context_window_hours", 24.0) or 0)


def recent_outreach(user_id: str, db=None) -> Optional[dict]:
    """该买家是否刚收到过营销触达(承接窗口内)。**整段 fail-soft。**

    这条读在买家会话热路径上:库读不出来、窗口配错、表结构对不上,一律返回 None
    按"没有触达"处理。少一段前情提示的代价是客服要多问一句;让买家这一轮失败的
    代价大得多。
    """
    try:
        if db is None:
            from app.db import get_db
            db = get_db()
        return db.recent_sent_outreach(user_id, outreach_window_hours())
    except Exception:  # noqa: BLE001 热路径,拿不到就当没有
        logger.warning("反查最近触达失败(本轮按无触达处理) user=%s", user_id,
                       exc_info=True)
        return None


def awaiting_reply(raw_messages) -> bool:
    """最后一条 assistant 消息是不是营销触达——即"买家这一轮是在回应它"。

    **为什么光有"窗口内发过触达"不够**:承接窗口是 24 小时,而买家可能在这期间
    已经和客服聊了三轮别的事。那时触达偏好不该再压过粘性 `_last_key`——话题早就
    走开了,硬把路由拽回催付款是错的。

    判据取信封里的 `intent == "growth_outreach"`(投递时由
    `_append_agent_reply` 写入),不是"最后一条是不是 assistant":坐席人工回复、
    正常客服回答都是 assistant。

    整段 fail-soft 返回 False:判不出来就当"不是在回应触达",退回既有路由行为。
    """
    import json

    for m in reversed(list(raw_messages or [])):
        role = m.get("role") if isinstance(m, dict) else None
        if role == "user":
            return False          # 买家已经在触达之后说过话了
        if role != "assistant":
            continue              # tool 结果等中间消息跳过
        content = m.get("content")
        if not isinstance(content, str) or not content.strip().startswith("{"):
            return False          # 纯文本 assistant 消息:不是结构化信封
        try:
            return json.loads(content).get("intent") == "growth_outreach"
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False
    return False


def preferred_profile(outreach: Optional[dict]) -> Optional[str]:
    """这条触达更可能该由哪个画像接;未登记/无触达返回 None。

    **这是回落目标,不是覆盖。** 用法见 `Router.route`:LLM 判出了明确意图就听
    LLM 的——买家完全可以对一条催付款触达回"我要退款",那时必须去售后。这个映射
    只替换掉"LLM 判不出来时盲选 `DEFAULT_AGENT`"那一支:同样是猜,拿商机类型猜比
    拿七个字猜准。

    **绝不能让营销相关的偏好把 `aftersale` 这个兜底改掉**:未登记的商机类型返回
    None,由调用方回落到既有 `DEFAULT_AGENT`。履约诉求被误路由到一个没有
    `apply_refund` 的画像是真实伤害,反过来只是答得笼统一点——两者不对称。
    """
    if not outreach:
        return None
    return _PREFERRED_PROFILE.get(str(outreach.get("opportunity_type") or ""))


def router_hint(outreach: Optional[dict]) -> str:
    """给路由器的一行前情。空 = 没有触达。

    只说"店铺刚主动发过一条什么情境的消息",不含券码/订单号——路由器只需要判断
    意图归属,给它更多信息只会让那个 10-token 的分类输出更容易跑偏。
    """
    if not outreach:
        return ""
    label = _kind_label(outreach.get("opportunity_type"))
    if not label:
        return ""
    return f"注意:店铺刚刚主动给这位顾客发过一条消息,情境是「{label}」。"


def _kind_label(kind) -> str:
    """商机类型 → 中文标签。**复用 growth 的那张表,不在这里另抄一份。**

    抄一份的后果是它会漂移:那边加了一类商机、改了措辞,这里还是旧的,而症状是
    "买家侧提示里的情境说得不对",没有任何报错。
    """
    try:
        from app.agent.tools.growth import OPPORTUNITY_KINDS
        return str(OPPORTUNITY_KINDS.get(str(kind or ""), "")).strip()
    except Exception:  # noqa: BLE001
        return ""


def render_outreach_context(outreach: Optional[dict]) -> str:
    """渲染成买家侧 system prompt 片段。没有触达/认不出情境返回空串。

    认不出商机类型时返回空,而不是给一段泛化的"顾客刚收到过一条消息"——一条说不清
    情境的前情会让客服的开场莫名其妙地变成在追问一件它自己也不知道是什么的事。
    """
    if not outreach:
        return ""
    label = _kind_label(outreach.get("opportunity_type"))
    if not label:
        return ""

    lines = [f"- 顾客的上一条消息是**店铺主动发出**的,情境是「{label}」;"
             "顾客这一轮很可能是在回应它,**不要让顾客重复说明来意**。"]

    order_id = str(outreach.get("order_id") or "").strip()
    if order_id:
        lines.append(f"- 这条消息关联订单 `{order_id}`。顾客说「这单」「那个订单」时指的"
                     "就是它,不必再问一遍订单号;要用具体状态请照常调工具查,"
                     "不要凭这段提示作答。")

    coupon = str((outreach.get("offer") or {}).get("coupon_code") or "").strip()
    if coupon:
        # 券码本身是**已经发给买家**的(投递前 issue_for_draft 已发放),告诉画像
        # 不构成任何泄露。但**规则必须查**:面额、门槛、有效期都在库里,凭印象
        # 说明的后果是顾客照着一个编出来的门槛去下单。
        lines.append(f"- 随这条消息发放了优惠券 `{coupon}`。顾客问它怎么用/能不能叠加时,"
                     "**必须用 query_coupons 查实际规则再回答**,不得凭印象说明面额、"
                     "门槛或有效期。")

    return ("\n\n## 本次对话的前情(内部提示,**不要向顾客复述这段话本身**)\n"
            + "\n".join(lines))
