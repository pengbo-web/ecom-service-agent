"""风险动作的前置授权门（借鉴 nanobot agent/goal_permission.py）。

默认拒绝:退款、成交等"提交型/涉钱"动作在执行前必须获得本轮授权,
否则工具返回"需确认"而不执行。授权来源:用户在对话中显式确认(前端 confirm 标志)
或坐席放行。授权按轮生效(ContextVar),用后自动复位,绝不泄漏到下一轮。
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar

# 需要前置授权的风险动作(涉钱/不可逆/改重要信息)
RISK_ACTIONS = frozenset({"refund", "deal_close", "cancel_order", "change_address"})

_ALLOWED: ContextVar[frozenset] = ContextVar("consent_allowed", default=frozenset())


def is_allowed(action: str) -> bool:
    """本轮是否已授权该动作。"""
    return action in _ALLOWED.get()


def allowed_actions() -> frozenset:
    """本轮已授权的动作集合。

    存在的理由只有一个:**MCP 工具在独立进程里执行,ContextVar 传不过去。**
    `ToolManager.execute_tool` 需要把这个集合读出来、随保留参数 `ctx_consent`
    带过去,server 端再 `consent_scope(...)` 落地——与 `ctx_user_id` 同一套办法。
    没有这个读取口时,跨进程后 `is_allowed` 永远为假:实测 MCP 路径上买家确认了
    退款,工具仍然回一句"请确认是否办理退款",**退款永远完不成**。
    """
    return _ALLOWED.get()


@contextmanager
def consent_scope(actions):
    """在作用域内授权一组动作;退出即复位。"""
    token = _ALLOWED.set(frozenset(actions or ()))
    try:
        yield
    finally:
        _ALLOWED.reset(token)


def need_confirm_result(action: str, message: str) -> dict:
    """未授权时工具的统一返回体(不执行副作用)。"""
    return {"success": False, "need_confirm": True, "action": action, "message": message}


# 确认语识别:用户明确表示同意执行的短语
_CONFIRM_WORDS = [
    "确认", "确定", "同意", "可以", "好的", "没错", "就这样", "就这么办",
    "退吧", "退款吧", "下单", "成交", "同意退款", "同意下单", "是的",
]


def is_confirmation(text: str) -> bool:
    """判断用户这句是否为"确认执行"。过长的一般不是单纯确认。

    用作前置授权门的放行信号:用户这轮说了确认语,才在动作边界放行风险动作。
    这样匹配模型真实行为(模型总是先口头请用户确认、确认后才调风险工具)。
    """
    t = (text or "").strip()
    if not t or len(t) > 30:
        return False
    return any(w in t for w in _CONFIRM_WORDS)


def confirm_targets_pending(user_input: str, pending, explicit_confirm: bool) -> bool:
    """本轮确认是否真的指向这个挂起动作。绑定信号(任一成立即绑定):
    ① 前端显式 confirm 标志(点了确认按钮,正规路径);
    ② 用户消息提到了挂起动作的目标订单号(强绑定);
    ③ 用户消息含"确认/确定/同意"这类**强确认词**(非'可以/好的'泛化词)且未提到其它订单号。
    仅凭"可以/好的/是的"这类泛化附和,不足以重放不可逆动作。"""
    if pending is None:
        return False
    if explicit_confirm:
        return True
    text = (user_input or "").strip()
    if not text:
        return False
    # 大小写归一化:订单号匹配须无视大小写,否则小写他单号(如 ord-...)会绕过错目标防线
    text_up = text.upper()
    target_up = str((getattr(pending, "args", {}) or {}).get("order_id") or "").strip().upper()
    # 提到了"别的"订单号(ORD- 格式但不是挂起单)→ 明确指向别处/意图含糊,一律不重放
    # (错目标防线;放在最前,即使同时提到本单也按"含糊"从严处理,不误重放)
    others = [m for m in re.findall(r"ORD-\d{8}-\d{3}", text_up) if m != target_up]
    if others:
        return False
    # 精确挂起单号匹配:最强、最难伪造的绑定信号(16 位单号几乎不可能误含)。
    # 即便消息偏长也放行——单号本身占 16 字,叠加确认语极易超 30 字,长度门会误杀
    # 含单号的正常确认(如"确认 ORD-...-001 退款吧,原因是不想要了"),致退款静默不执行。
    if target_up and target_up in text_up:
        return True                       # 提到本挂起单号 → 强绑定
    # 无单号、仅凭确认词的弱路径:过长消息一般不是单纯确认,用长度门过滤附带闲聊
    if len(text) > 30:
        return False
    strong = ("确认", "确定", "同意", "就这么办", "退款吧", "成交")
    return any(w in text for w in strong)
