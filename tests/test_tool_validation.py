"""工具运行时硬校验:非法参数在进业务逻辑前挡回并回传纠错(借鉴 Customer-Agent goods_id<1000 护栏)。"""

import json

from app.agent.tools.validation import validate_tool_args
from app.agent.tools.registry import execute_tool


# ---- 纯校验函数 ----

def test_valid_order_id_passes():
    assert validate_tool_args("query_order", {"order_id": "ORD-20240110-003"}) is None


def test_fabricated_order_prefix_rejected():
    # 模型历史上编过 YD/PX 开头的假单号,硬校验直接挡
    err = validate_tool_args("query_order", {"order_id": "YD2024051234567890"})
    assert err is not None and "ORD-" in err and "格式" in err


def test_list_index_as_order_id_rejected():
    # 借鉴点:LLM 可能把列表序号当 ID 传
    err = validate_tool_args("apply_refund", {"order_id": "3", "reason": "不喜欢"})
    assert err is not None and "ORD-" in err


def test_empty_order_id_rejected():
    err = validate_tool_args("cancel_order", {"order_id": ""})
    assert err is not None


def test_all_order_tools_validated():
    for tool in ("query_order", "query_logistics", "apply_refund",
                 "cancel_order", "change_address",
                 "issue_invoice", "expedite_shipping"):
        args = {"order_id": "bad"}
        if tool == "apply_refund":
            args["reason"] = "x"
        if tool == "change_address":
            args["new_address"] = "x"
        assert validate_tool_args(tool, args) is not None, tool


def test_non_order_tool_not_touched():
    # query_product 收 keyword,不校验 order_id
    assert validate_tool_args("query_product", {"keyword": "运动鞋"}) is None


def test_missing_order_id_key_skipped():
    # 参数里根本没 order_id(理论不会发生)不误挡
    assert validate_tool_args("query_order", {}) is None


# ---- execute_tool 接线:非法参数不进业务逻辑 ----

def test_execute_tool_blocks_before_business_logic(monkeypatch):
    called = []
    import app.agent.tools.registry as reg
    monkeypatch.setitem(reg._TOOL_MAP, "query_order",
                        lambda order_id: called.append(order_id) or {"success": True})
    out = execute_tool("query_order", {"order_id": "PX编造的单号"})
    data = json.loads(out)
    assert data.get("success") is False and "ORD-" in data.get("error", "")
    assert called == []          # 业务函数根本没被调用


def test_execute_tool_valid_passes_through(monkeypatch):
    import app.agent.tools.registry as reg
    monkeypatch.setitem(reg._TOOL_MAP, "query_order",
                        lambda order_id: {"success": True, "order_id": order_id})
    out = execute_tool("query_order", {"order_id": "ORD-20240110-003"})
    assert json.loads(out)["success"] is True
