"""风险动作的前置授权门（借鉴 nanobot agent/goal_permission.py）。

默认拒绝:退款、成交等"提交型/涉钱"动作在执行前必须获得本轮授权,
否则工具返回"需确认"而不执行。授权来源:用户在对话中显式确认(前端 confirm 标志)
或坐席放行。授权按轮生效(ContextVar),用后自动复位,绝不泄漏到下一轮。
"""

from contextlib import contextmanager
from contextvars import ContextVar

# 需要前置授权的风险动作
RISK_ACTIONS = frozenset({"refund", "deal_close"})

_ALLOWED: ContextVar[frozenset] = ContextVar("consent_allowed", default=frozenset())


def is_allowed(action: str) -> bool:
    """本轮是否已授权该动作。"""
    return action in _ALLOWED.get()


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
