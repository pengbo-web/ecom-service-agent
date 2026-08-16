"""运行时上下文:把当前登录用户传到工具层(与 bargain.current_session 同模式)。

工具(如 query_coupons)据此按用户资格个性化,而不信任模型传参——防伪造身份。
EcomAgent.chat() 每轮刷新;取不到时工具应 fail-open。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
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


# 这一轮流量**是不是真实买家打出来的**。
#
# **为什么必须有。** 实测 `skill_traces` 里 976 轮的构成:
#
#     u1                                836 轮   测试固定用户
#     数字 id                            62 轮   hmdp 真实买家
#     ab*/ev*/trk*                       41 轮   压测 / 评测
#     measure_user/loadchat/framecheck…  35 轮   历次人工走查
#
# **真实买家只有 62 轮**,而在加这个字段之前,表里**没有任何东西能把它们分开**。
# 这不只是"数字不好看":`watchdog.evaluate_absolute` 拿这张表算成功率,
# `rate < 0.6 → 自动回滚` —— 也就是说**一轮压测就能把一份没问题的 skill 从线上
# 回滚掉**。`AB_SANITY_FLOOR = 0.3` 那道下限是为这个加的创可贴,它只挡得住低到
# 离谱的情况,挡不住 0.3~0.6 这一段,而 track-order 的 28% 正落在那附近。
#
# 更要紧的是:阶段三多轮用户模拟一旦开跑,会往同一张表灌上千条合成轨迹。
# **没有这个字段先落地,那 62 轮真实数据会被淹没到看不见,而看门狗还在拿它做
# 自动回滚判定。** 所以这个字段是多轮模拟的安全前提,不是它的准备工作。
#
# 与 actor 同模式:contextvar、每轮由入口刷新。默认 `live` —— 真实请求走的是
# 默认路径,而合成流量的每一个产生点都是我们自己写的代码,由它显式标注。
# 反过来把默认设成 unknown,会让真实流量因为某处忘了标注而被判定层整批丢掉。
SOURCE_LIVE = "live"            # 真实买家 / 店主
SOURCE_LOADTEST = "loadtest"    # 压测
SOURCE_EVAL = "eval"            # 离线评测 / 沙箱重跑
SOURCE_SIMULATED = "simulated"  # 多轮用户模拟(阶段三)
SOURCE_DEV = "dev"              # 人工走查 / 调试
SOURCE_UNKNOWN = "unknown"      # 历史行:字段加上之前就存在,判不出

#: 全部合法取值。落库前用它兜一道,防止拼错的字符串静默变成一个新"来源"
#: ——那会让"只认 live"的判定层看起来一切正常,实际把某一类流量漏算了。
TRAFFIC_SOURCES = frozenset({SOURCE_LIVE, SOURCE_LOADTEST, SOURCE_EVAL,
                             SOURCE_SIMULATED, SOURCE_DEV, SOURCE_UNKNOWN})

#: **判定类**(看门狗自动转正/回滚)只认这些。
#: `unknown` 不在内:在判不出来源的数据上做自动回滚,正是这个字段要防的事。
DECISION_SOURCES = frozenset({SOURCE_LIVE})

#: **语料采样类**(合成门禁用例、失败自改进、聚类蒸馏)认这些。
#:
#: 与 `DECISION_SOURCES` 的差别是刻意的,两类用途的错误代价不同:
#:
#: - 判定错 = 把一份没问题的 skill 从线上回滚掉,**立刻生效且不可白做**;
#: - 采样错 = 多学了一段不该学的对话,还要过校验、门禁、风险分级、人工审批
#:   四道关才可能上线。
#:
#: 所以采样容得下 `unknown`(历史行,不然那 87 条归档语料全部作废,阶段一等于
#: 白做),判定容不下。
#:
#: 但 `loadtest`/`eval`/`simulated` **两边都排除**:压测对话是模板化的重复句,
#: 评测对话是我们自己写的用例,拿它们当"真实买家语料"蒸馏进 SKILL.md 是在
#: 学自己的回声。`simulated` 尤其危险——阶段三的多轮模拟本身就是拿 skill 生成的,
#: 再喂回去合成门禁用例就成了闭环自证。
SAMPLING_SOURCES = frozenset({SOURCE_LIVE, SOURCE_UNKNOWN})

_current_source: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_traffic_source", default=None)


def set_traffic_source(source: Optional[str]) -> None:
    """标注这一轮流量的来源。非法值按 unknown 落,不静默接受拼错的字符串。"""
    if source is not None and source not in TRAFFIC_SOURCES:
        source = SOURCE_UNKNOWN
    _current_source.set(source)


def get_traffic_source() -> str:
    """当前流量来源;未设置时按 `live`(见上面注释里"为什么默认是 live")。"""
    return _current_source.get() or SOURCE_LIVE


@contextmanager
def traffic_source_scope(source: Optional[str]):
    """在作用域内标注来源,**退出时恢复原值**(与 `consent.consent_scope` 同模式)。

    裸 `set_traffic_source()` 只适合"这条请求从头到尾都是这个来源"的入口。
    进程内嵌套跑东西时必须用这个 —— 否则设完不还,后面所有在同一线程里发生的
    事都被打上那个标。

    **实测踩过**:`Sandbox.run` 裸设 `eval`,跑完不还。生产上没事(每个请求一个
    上下文),但全量测试是同一个线程串着跑的,于是评测沙箱之后的每一条轨迹都被
    记成 `eval` —— 看门狗按 live 过滤,一条都取不到,6 个 CLI 用例集体变红,
    而它们单独跑全过。同样的形态在生产里会出现在任何"进程内先跑一次评测再做
    别的"的地方。
    """
    token = _current_source.set(source)
    try:
        yield
    finally:
        _current_source.reset(token)
