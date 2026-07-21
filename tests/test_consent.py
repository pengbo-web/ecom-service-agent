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
