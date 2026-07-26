from app.agent.consent import is_allowed, consent_scope, RISK_ACTIONS, need_confirm_result


def test_default_deny():
    assert is_allowed("refund") is False
    assert is_allowed("deal_close") is False


def test_scope_grants_then_resets():
    with consent_scope({"refund"}):
        assert is_allowed("refund") is True
        assert is_allowed("deal_close") is False   # 只授权了 refund
    assert is_allowed("refund") is False            # 退出即复位


def test_scope_all_risk_actions():
    with consent_scope(RISK_ACTIONS):
        assert is_allowed("refund") and is_allowed("deal_close")


def test_nested_scopes_restore():
    with consent_scope({"refund"}):
        with consent_scope({"deal_close"}):
            assert is_allowed("deal_close") and not is_allowed("refund")
        assert is_allowed("refund") and not is_allowed("deal_close")


def test_need_confirm_result_shape():
    r = need_confirm_result("refund", "确认?")
    assert r["success"] is False and r["need_confirm"] is True and r["action"] == "refund"


def test_is_confirmation():
    from app.agent.consent import is_confirmation
    assert is_confirmation("确认") is True
    assert is_confirmation("好的,退款吧") is True
    assert is_confirmation("同意下单") is True
    assert is_confirmation("我要退款订单 ORD-1") is False        # 首次请求不是确认
    assert is_confirmation("我先问一下这个商品有货吗") is False   # 太长/非确认
    assert is_confirmation("") is False


def test_confirm_binding_allows_long_message_with_target_order():
    """回归:含挂起单号的确认即便超 30 字也应绑定(原长度门会误杀,致退款静默不执行)。"""
    from app.agent.consent import confirm_targets_pending
    from app.agent.pending import PendingAction
    order = "ORD-20240115-001"
    pa = PendingAction(action="refund", tool_name="apply_refund",
                       args={"order_id": order, "reason": "x"}, message="c")
    long_with_target = f"确认 {order} 退款吧，原因是不想要了这个商品谢谢"
    assert len(long_with_target) > 30
    assert confirm_targets_pending(long_with_target, pa, False) is True
    # 提到"别的"单号仍不绑定(错目标防线不回退)
    assert confirm_targets_pending(f"确认退 ORD-20240115-002 谢谢", pa, False) is False
    # 无单号、仅确认词、超长 → 仍按弱路径的长度门过滤
    assert confirm_targets_pending("确认" + "啦" * 40, pa, False) is False
