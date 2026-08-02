"""运行时上下文:把当前登录用户传到工具层(与 bargain.current_session 同模式)。

工具(如 query_coupons)据此按用户资格个性化,而不信任模型传参——防伪造身份。
EcomAgent.chat() 每轮刷新;取不到时工具应 fail-open。
"""

from __future__ import annotations

import contextvars
from typing import Optional

_current_user: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_user", default=None)

# 当前用户的 hmdp 登录 token(接 hmdp 真实数据源时,MCP 侧调 hmdp 登录保护接口需要它)。
# 与 current_user 同模式:每轮刷新;跨进程经 MCP 的 ctx_token 保留参数透传。
_current_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_token", default=None)


def set_current_user(user_id: Optional[str]) -> None:
    _current_user.set(user_id)


def get_current_user() -> Optional[str]:
    return _current_user.get()


def set_current_token(token: Optional[str]) -> None:
    _current_token.set(token)


def get_current_token() -> Optional[str]:
    return _current_token.get()


# 当前咨询商品 id(顾客正在看的商品):用于"这/它/这款"的指代消解与商品介绍接地。
# 与 current_user/current_token 同模式:每轮由 streaming worker 刷新;不信任模型传参。
_current_item: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_item", default=None)


def set_current_item(item_id: Optional[str]) -> None:
    _current_item.set(item_id)


def get_current_item() -> Optional[str]:
    return _current_item.get()
