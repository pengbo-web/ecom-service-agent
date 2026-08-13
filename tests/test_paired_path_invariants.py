"""成对路径的不变量:一条路做对了,它的孪生路必须也做。

这一轮走查抓到的缺陷有一个反复出现的形态——**同一件事有两条路,只有一条做了**:

    身份传过 MCP 了,确认门没传          (53)
    购物车「去下单」防了双击,「立即购买」没防   (㊿③)
    长期记忆过了归属过滤,会话摘要没过      (51②)
    Redis 后端往返保真,文件后端丢字段      (52)

本文件不是"把已修的列一遍"——那种测试对将来没有任何价值。这里只放**能从真源推导
期望**的不变量:新增一个字段、新暴露一个受管工具,测试会自动失败,而不必等下一次
走查再撞一遍。

**能盖住什么、盖不住什么**(说清楚比假装全覆盖重要):

- ✅ 会话状态字段:期望直接从 `_session_state()` 的返回值推导,加字段自动纳入;
- ✅ MCP 工具的确认门/身份透传:用 ast + inspect 机械判定,新工具自动纳入;
- ❌ 前端按钮的防重复点击、提示词注入点的归属过滤:这两类没有可靠的机械判据
  (什么算"会改数据的按钮"、什么算"注入点"都要靠读代码判断),仍然只能靠走查。
  它们各自有针对性的测试(`buy-double-click.test.tsx`、`test_memory_cross_user_leak.py`),
  但**不构成对未来新增路径的防护**。
"""

import ast
import inspect
import io
import json
import os
import tempfile
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------
# 不变量一:会话状态的每一个字段,两个后端都要能往返
#
# 实测缺陷(52):`_session_state()` 产出七个字段,`FileSessionStore.save` 只传三个,
# `status`/`step_seq`/`pending` 被静默丢在门口;而 Redis 后端存整块 JSON 一直保真。
# 同一份代码换个后端行为就变,且 Redis 连不上时会降级到文件后端。
# --------------------------------------------------------------------------

def _state_keys_from_source() -> set:
    """**从真源推导**要持久化哪些字段,而不是抄一份清单。

    抄清单的测试在"有人给 `_session_state()` 加了第八个字段"时不会响——而那正是
    这条缺陷会重演的方式。这里用假 self 调真方法,加字段自动被纳入。
    """
    from app.agent.chat import EcomAgent

    fake = SimpleNamespace(
        raw_messages=[], summary=None,
        memory_manager=SimpleNamespace(stm_to_dict=lambda: {}),
        _status="complete", _step_seq=0, _pending=None)
    return set(EcomAgent._session_state(fake).keys())


#: `version` 是格式版本号,由存储层自己写(SESSION_VERSION),不需要往返。
_NOT_PERSISTED = {"version"}


def _sample_state(keys: set) -> dict:
    """给每个字段造一个**可区分**的值:用 None 或空值填会让"丢了"和"存了空"分不开。"""
    samples = {
        "messages": [{"role": "user", "content": "订单在哪"}],
        "summary": "摘要文本",
        "short_term_memory": {"facts": ["x"]},
        "status": "in_flight",
        "step_seq": 7,
        "pending": {"action": "refund", "order_id": "ORD-1"},
        "version": 1,
    }
    missing = keys - set(samples)
    assert not missing, (
        f"`_session_state()` 新增了字段 {sorted(missing)},但本测试不知道该拿什么值试它。"
        "请在 _sample_state 里补一个可区分的样例——顺便确认两个后端都真的把它存下来了。")
    return {k: samples[k] for k in keys}


def test_file_store_round_trips_every_session_field():
    """**核心不变量。** 加一个字段而忘了往下传,这条会立刻失败。"""
    from app.session.store import FileSessionStore

    keys = _state_keys_from_source() - _NOT_PERSISTED
    state = _sample_state(_state_keys_from_source())
    path = os.path.join(tempfile.mkdtemp(), "s.json")

    fs = FileSessionStore()
    fs.save(path, state)
    back = fs.load(path)

    lost = [k for k in keys if back.get(k) != state[k]]
    assert not lost, f"文件后端丢了这些字段: {lost}(52 号缺陷的重演)"


def test_both_backends_round_trip_the_same_fields():
    """两个后端必须一致。不一致时症状只在其中一种部署下出现,极难查。"""
    fakeredis = pytest.importorskip("fakeredis")
    from app.session.store import FileSessionStore, RedisSessionStore

    keys = _state_keys_from_source() - _NOT_PERSISTED
    state = _sample_state(_state_keys_from_source())
    path = os.path.join(tempfile.mkdtemp(), "s.json")

    fs = FileSessionStore()
    fs.save(path, state)
    rs = RedisSessionStore(client=fakeredis.FakeStrictRedis(decode_responses=True),
                           fallback=fs)
    rs.save("sess-1", state)

    f_back, r_back = fs.load(path), rs.load("sess-1")
    diff = [k for k in keys if f_back.get(k) != r_back.get(k)]
    assert not diff, f"两个后端在这些字段上不一致: {diff}"


def test_fields_are_actually_written_to_disk():
    """回读对 ≠ 存下来了:换个读取实现就可能又丢。断言磁盘上真有这些键。"""
    from app.session.store import FileSessionStore

    keys = _state_keys_from_source() - _NOT_PERSISTED
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    FileSessionStore().save(path, _sample_state(_state_keys_from_source()))

    on_disk = set(json.load(open(path, encoding="utf-8")))
    assert keys <= on_disk, f"这些字段没落到磁盘上: {sorted(keys - on_disk)}"


# --------------------------------------------------------------------------
# 不变量二:MCP 暴露的工具,身份与确认门都要跨得过去
#
# 实测缺陷(53):`ctx_user_id` 传了,`ctx_consent` 没传。确认门是 ContextVar,
# 跨不了进程,于是 MCP 路径上买家确认了退款,工具仍回 need_confirm——退款永远完不成。
# --------------------------------------------------------------------------

def _mcp_tools():
    """枚举 `mcp_server/server.py` 里所有 `@mcp.tool()` 函数及其判定结果。

    用 ast 读源码而不是读 FastMCP 的注册表:要判断的是**代码里写没写**那两行透传,
    注册表看不到函数体。
    """
    import mcp_server.server as server_mod

    src = io.open(inspect.getsourcefile(server_mod), encoding="utf-8").read()
    tree = ast.parse(src)

    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
                   for d in node.decorator_list):
            continue

        body_src = ast.get_source_segment(src, node) or ""
        params = {a.arg for a in node.args.args}

        # 这个 MCP 包装函数调了哪些下层实现(命名约定:_query_order 之类的别名)
        gated = False
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            underlying = getattr(server_mod, call.func.id, None)
            if underlying is None or not callable(underlying):
                continue
            try:
                if "is_allowed(" in inspect.getsource(underlying):
                    gated = True
            except (OSError, TypeError):
                pass

        out.append(SimpleNamespace(name=node.name, params=params,
                                   consent_gated=gated, body=body_src))
    return out


def test_there_are_mcp_tools_to_check():
    """守住这套检查本身:枚举不到工具时上面的断言会**全部空过**。"""
    tools = _mcp_tools()
    assert len(tools) >= 4, f"只枚举到 {len(tools)} 个 MCP 工具,检查逻辑可能失效了"


def test_every_mcp_tool_plumbs_identity():
    """每个 MCP 工具都要接 `ctx_user_id` 并 `set_current_user`——否则 `owned_order`
    在 server 进程里拿不到身份。auth 开启时它 fail-closed(视同订单不存在),
    表现为"查不到自己的订单",而不是报错。"""
    bad = [t.name for t in _mcp_tools()
           if "ctx_user_id" not in t.params or "set_current_user" not in t.body]
    assert not bad, f"这些 MCP 工具没做身份透传: {bad}"


def test_consent_gated_mcp_tools_plumb_consent():
    """**核心不变量(53 号缺陷的防复发)。**

    只要下层实现里出现 `is_allowed(`,这个 MCP 包装就必须接 `ctx_consent` 并用
    `_consent_from` 落地。将来把 `cancel_order` / `change_address` / `deal_close`
    (都受确认门管)暴露到 MCP 上而忘了透传,这条会立刻失败——不必等买家反复确认
    却什么都没发生时才发现。
    """
    bad = [t.name for t in _mcp_tools()
           if t.consent_gated
           and ("ctx_consent" not in t.params or "_consent_from" not in t.body)]
    assert not bad, (
        f"这些 MCP 工具受确认门管却没透传 consent: {bad}。"
        "跨进程后 is_allowed 永远为假,该动作在 MCP 路径上永远完不成(53 号缺陷)")


def test_detection_actually_finds_the_gated_tool():
    """**反向守卫**:确认判定逻辑真的能识别出受管工具。

    没有这条,`consent_gated` 恒为 False 时上面那条会永远通过——一个永远绿的测试
    比没有测试更糟,因为它让人以为这里有防护。
    """
    gated = [t.name for t in _mcp_tools() if t.consent_gated]
    assert gated, "一个受确认门管的 MCP 工具都没识别出来,判定逻辑失效了"
    assert "apply_refund" in gated


# --------------------------------------------------------------------------
# 不变量三:客户端侧的保留参数不能被模型覆盖
# --------------------------------------------------------------------------

def test_reserved_context_params_override_model_arguments():
    """保留参数必须放在 `**arguments` **之后**。

    顺序写反了,模型就能自称任何用户、自授权任何动作——而这种写反在 code review 里
    极容易看漏(两行长得几乎一样)。
    """
    from app.agent.tools.manager import ToolManager

    calls = []

    class Recording:
        def call_tool(self, name, args):
            calls.append(args)
            return "{}"

    tm = ToolManager.__new__(ToolManager)
    tm._mcp_client = Recording()
    tm._tool_source = {"apply_refund": "mcp"}
    tm._shared_mcp = True
    tm.execute_tool("apply_refund", {"order_id": "O1", "reason": "r",
                                     "ctx_user_id": "victim", "ctx_consent": "refund"})

    assert calls[0]["ctx_user_id"] == "", "模型冒充了别的用户"
    assert calls[0]["ctx_consent"] == "", "模型自授权成功了"
