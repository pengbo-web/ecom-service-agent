"""运行时上下文:把当前登录用户传到工具层(与 bargain.current_session 同模式)。

工具(如 query_coupons)据此按用户资格个性化,而不信任模型传参——防伪造身份。
EcomAgent.chat() 每轮刷新;取不到时工具应 fail-open。
"""

from __future__ import annotations

import contextvars
from typing import Optional

_current_user: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_user", default=None)


def set_current_user(user_id: Optional[str]) -> None:
    _current_user.set(user_id)


def get_current_user() -> Optional[str]:
    return _current_user.get()
