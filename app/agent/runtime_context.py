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


# 当前这一轮服务的是谁:买家("buyer")还是店主("seller")。
#
# 为什么需要它:买卖两侧的**工具**早就隔离了(各画像独立 ToolManager,有测试从
# 注册表派生校验),但 **skill 没有**——`SkillManager` 是同一个引擎实例上的同一
# 个对象,买家画像和卖家画像共用,`build_catalog_prompt()` 无条件列出全部 skill,
# 而两侧画像的工具集里都有 `load_skill`。也就是说在加 actor 归属之前,一份写给
# 店主的营销 skill 会出现在买家会话的技能目录里、能被买家侧加载出来——工具确实
# 调不动(买家 ToolManager 里没有 find_opportunities),但**运营指令文本会原样进
# 买家上下文**,商机口径、催付款话术、优惠策略全都在里面。
#
# 与 current_user/current_item 同模式:contextvar、每轮由编排器刷新、取不到时
# 按最保守的一侧处理(见 skills/loader.py 的 `_visible_skills`:未知 actor 只看
# 得到买家 skill,而不是看得到全部)。
ACTOR_BUYER = "buyer"
ACTOR_SELLER = "seller"

_current_actor: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_actor", default=None)


def set_current_actor(actor: Optional[str]) -> None:
    _current_actor.set(actor)


def get_current_actor() -> str:
    """当前 actor;未设置时按 buyer 处理(保守默认:少看见,不多看见)。"""
    return _current_actor.get() or ACTOR_BUYER
