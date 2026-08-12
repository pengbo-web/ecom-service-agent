"""出话泄漏检测漏了工具名——而实际漏出去的正是工具名。

**实测缺陷**(走查记忆页时抓到)。以真实买家身份问一个查不到的订单号,回复里有这么一句:

    我将立即调用 list_user_orders 查看您全部在途订单,稍后为您确认匹配项。

`detect_internal_leak` 一个词都没命中:`INTERNAL_JARGON_TERMS` 里有手写的 `"调用工具"`,
而这里说的是"调用 list_user_orders";**工具真名一个都不在词表里**。

而 `app/agent/jargon_guard.py` 的模块 docstring 自己写着那条纪律:

> 词表来源单一:skill 名字必须从调用方传入的 `skill_names` 取…本模块不自己维护、
> 也不重新发现一份 skill 清单——这个仓库已经因为"手抄表跟真源 drift"出过好几次问题,
> 不要再添一份。

这条纪律用在了 skill 名上,**没用在工具名上**。所以修法不是往
`INTERNAL_JARGON_TERMS` 里手抄一批工具名(那正是它警告过的做法),而是从
`ToolManager.tool_names` 这个真源取——与 `SkillManager.skill_names` 完全同构。

取**过滤后**的 `_tool_defs` 而不是全量注册表:没装上的工具模型不可能说出来,
放进词表只会增加误报面。
"""

import pytest

from app.agent.jargon_guard import detect_internal_leak


# --------------------------------------------------------------------------
# 核心:那句真实回复
# --------------------------------------------------------------------------

REAL_REPLY = ("抱歉暂时未查到订单号 ORD-20240115-001。"
              "为帮您准确定位,我将立即调用 list_user_orders 查看您全部在途订单,"
              "稍后为您确认匹配项。")


def test_real_leaked_reply_is_caught():
    """**就是买家实际收到的那句话。** 修复前返回 []。"""
    hits = detect_internal_leak(REAL_REPLY, [], ["list_user_orders", "query_order"])
    assert "list_user_orders" in hits, f"工具名仍然漏检: {hits}"


def test_without_tool_names_it_still_misses():
    """反过来锁住"这确实是原来漏的那一条":不传工具名时,这句话一个词都不命中。

    这条断言的作用是证明修复前的行为,以免以后有人把 tool_names 参数删掉却
    以为别的词表兜住了。
    """
    assert detect_internal_leak(REAL_REPLY, []) == []


# --------------------------------------------------------------------------
# 既有能力不能因为加了一个参数而回退
# --------------------------------------------------------------------------

def test_skill_names_still_caught():
    assert "process-return" in detect_internal_leak(
        "我已加载 process-return 的流程为您处理", ["process-return"])


def test_jargon_still_caught():
    hits = detect_internal_leak("我也可以先调用工具帮您查", [])
    assert "调用工具" in hits


def test_tool_names_optional():
    """`tool_names` 必须可选:这个函数有别的调用方(测试、离线复算),
    改成必填会把它们全部打断。"""
    assert detect_internal_leak("你好", ["x"]) == []
    assert detect_internal_leak("你好", ["x"], None) == []


def test_clean_reply_not_flagged():
    """正常回复不该命中——误报会让这道检测很快被忽略。"""
    reply = "您好,已为您查询到 3 笔待发货订单,预计 1-2 个工作日内出库。"
    assert detect_internal_leak(reply, ["track-order"], ["list_user_orders"]) == []


# --------------------------------------------------------------------------
# 词表真源:ToolManager.tool_names
# --------------------------------------------------------------------------

def test_tool_manager_exposes_names_from_real_source():
    """`tool_names` 必须来自 `tool_definitions`(装上的那批),不是另抄一份。"""
    from app.agent.tools.manager import ToolManager

    tm = ToolManager()
    names = tm.tool_names
    assert names, "真源为空,检测器会退化成只查黑话"
    assert names == [d["function"]["name"] for d in tm.tool_definitions], \
        "tool_names 与 tool_definitions 不同源——正是 drift 的起点"


def test_tool_names_respects_allowlist():
    """子 Agent 限了工具集时,词表要跟着缩——没装上的工具模型不可能说出来,
    留在词表里只会增加误报面。"""
    from app.agent.tools.manager import ToolManager

    tm = ToolManager()
    keep = set(sorted(tm.tool_names)[:2])
    tm._filter_tools(keep)                    # 子 Agent 工具隔离走的就是这个入口
    assert set(tm.tool_names) == keep


# --------------------------------------------------------------------------
# 新依赖不能把整道检测拖垮
# --------------------------------------------------------------------------

def test_missing_tool_manager_does_not_silence_the_whole_check():
    """`_check_internal_leak` 整个包在 `except: pass` 里,所以新加的依赖必须用
    getattr 取:少一个属性只应让"工具名这一项"不查,不能把 skill 名与黑话一起带走。

    这条是加 tool_names 时当场撞到的——`test_jargon_guard.py` 里那个不带
    tool_manager 的 fake_self 直接让事件数从 1 掉到 0,**检测静默失效**。
    """
    from types import SimpleNamespace

    from app.agent.chat import EcomAgent

    events = []
    fake_self = SimpleNamespace(                      # 故意不给 tool_manager
        skill_manager=SimpleNamespace(skill_names=["process-return"]),
        _emit=lambda e: events.append(e),
    )
    EcomAgent._check_internal_leak(
        fake_self, "已加载process-return技能流程，我先调用工具查一下")

    assert len(events) == 1, "少了 tool_manager,整道检测就哑了"
    assert "process-return" in events[0]["hits"]
