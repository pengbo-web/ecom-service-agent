"""
测试客服 Agent 的结构化输出能力(第 1 期遗留,**真调 LLM**,显式 opt-in)

测试策略：真实调用 API，验证：
1. 返回类型是否正确（CustomerServiceResponse）
2. 意图识别是否准确
3. 多轮对话上下文是否保持
4. reset 功能是否正常

默认不跑——本文件每个用例都会真的打模型端点(花钱、慢、结果不确定),与
tests/ 下其余 190+ 个 hermetic 用例不是一回事。跑法与 tests/test_mcp.py
同一惯例(显式 env opt-in,不做端口/key 探测):

    RUN_LIVE_LLM=1 pytest tests/test_agent.py -v
"""

import os
import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_LLM"),
    reason="真调 LLM 的遗留端到端用例,需显式开启:RUN_LIVE_LLM=1(并已配置 OPENAI_API_KEY)",
)

from app.agent.chat import EcomAgent  # noqa: E402
from app.schemas.response import CustomerServiceResponse, IntentType  # noqa: E402

# 测试用例：(用户输入, 期望的意图类型列表)
TEST_CASES = [
    ("你好呀", [IntentType.GREETING]),
    ("我的订单 2024010112345 什么时候发货？", [IntentType.ORDER_QUERY]),
    ("这件衣服买了三天了想退货", [IntentType.RETURN_REQUEST]),
    ("你们这个商品质量也太差了吧！", [IntentType.COMPLAINT]),
    ("有没有什么优惠活动？", [IntentType.PROMOTION]),
    ("帮我推荐一款适合送女朋友的包包", [IntentType.PRODUCT_CONSULT]),
]


def test_structured_output():
    """测试单轮对话的结构化输出"""
    agent = EcomAgent()

    print("=" * 60)
    print("  测试 1：结构化输出 & 意图识别")
    print("=" * 60)

    passed = 0
    failed = 0

    for user_input, expected_intents in TEST_CASES:
        agent.reset()

        response = agent.chat(user_input)

        # 验证返回类型
        assert isinstance(response, CustomerServiceResponse), (
            f"返回类型错误: {type(response)}"
        )

        # 验证字段完整性
        assert response.reply, "reply 不能为空"
        assert 0.0 <= response.confidence <= 1.0, (
            f"confidence 越界: {response.confidence}"
        )
        assert isinstance(response.requires_human, bool)

        # 验证意图
        intent_ok = response.intent in expected_intents
        status = "PASS" if intent_ok else "FAIL"

        if intent_ok:
            passed += 1
        else:
            failed += 1

        print(f"\n[{status}] 输入: {user_input}")
        print(f"  意图: {response.intent.value} (期望: {[i.value for i in expected_intents]})")
        print(f"  置信度: {response.confidence:.0%}")
        print(f"  回复: {response.reply[:80]}...")

    print(f"\n结果: {passed} 通过, {failed} 失败 / 共 {len(TEST_CASES)} 条")
    assert failed == 0, f"{failed} 条意图识别不符合预期"


def test_multi_turn():
    """测试多轮对话上下文保持"""
    print("\n" + "=" * 60)
    print("  测试 2：多轮对话上下文")
    print("=" * 60)

    agent = EcomAgent()

    # 第一轮：提出问题
    r1 = agent.chat("我想退掉上周买的那双运动鞋")
    print(f"\n[轮次1] 输入: 我想退掉上周买的那双运动鞋")
    print(f"  回复: {r1.reply[:80]}...")

    # 第二轮：基于上下文追问（不再提"运动鞋"，看模型是否记住）
    r2 = agent.chat("穿了一次，鞋底就开胶了")
    print(f"\n[轮次2] 输入: 穿了一次，鞋底就开胶了")
    print(f"  回复: {r2.reply[:80]}...")

    # 验证对话历史。注意两点与第 1 期不同(引擎重构后的真实语义):
    #   ① 历史存在 `raw_messages`,且**不含 system**——system prompt 由
    #      `_build_messages()` 每轮现场拼(画像可切换),不落历史。
    #   ② 条数不是固定的 4:ReAct 循环命中工具时会追加 assistant(tool_calls)
    #      与 role="tool" 的观察结果,条数随模型这一轮调了几个工具而变。
    #      所以断言"两条 user 输入都在、顺序正确",而不是断言一个会随模型
    #      行为漂移的总数——后者测的是模型心情,不是上下文有没有保持。
    user_msgs = [m["content"] for m in agent.raw_messages if m.get("role") == "user"]
    assert user_msgs == ["我想退掉上周买的那双运动鞋", "穿了一次，鞋底就开胶了"], (
        f"用户消息未按序进入对话历史: {user_msgs}"
    )
    assert len(agent.raw_messages) >= 4, (
        f"对话历史过短(至少 2 轮 user/assistant): {len(agent.raw_messages)}"
    )
    print(f"\n[PASS] 对话历史保持正确: {len(agent.raw_messages)} 条消息(含工具轨迹)")


def test_reset():
    """测试 reset 功能"""
    print("\n" + "=" * 60)
    print("  测试 3：对话重置")
    print("=" * 60)

    agent = EcomAgent()
    agent.chat("你好")

    assert agent.raw_messages, "对话后历史不应为空"

    agent.reset()
    # reset() 把 raw_messages 清成 **空列表**(见 EcomAgent.reset):system prompt
    # 不在历史里,所以这里是 0 而不是第 1 期的"只剩 system"那 1 条。
    assert agent.raw_messages == [], f"reset 后历史未清空: {agent.raw_messages}"

    print("\n[PASS] reset 后对话历史已清空")


def _run(name: str, fn) -> tuple[str, bool]:
    """直接 `python tests/test_agent.py` 时用:测试函数现在只 assert 不返回
    (pytest 的 PytestReturnNotNoneWarning 要求),所以这里把"有没有抛
    AssertionError"翻译成汇总表要的布尔值。"""
    try:
        fn()
        return name, True
    except AssertionError as exc:
        print(f"  ❌ {exc}")
        return name, False


if __name__ == "__main__":
    results = [
        _run("结构化输出 & 意图识别", test_structured_output),
        _run("多轮对话上下文", test_multi_turn),
        _run("对话重置", test_reset),
    ]

    print("\n" + "=" * 60)
    print("  测试汇总")
    print("=" * 60)
    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("全部测试通过！")
    else:
        print("存在失败的测试，请检查。")
        sys.exit(1)
