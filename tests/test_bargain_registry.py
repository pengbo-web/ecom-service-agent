from app.agent.tools import registry


def test_negotiate_price_registered_when_enabled():
    # 默认 bargain_enabled=True
    assert "negotiate_price" in registry._TOOL_MAP
    names = [d["function"]["name"] for d in registry.TOOL_DEFINITIONS]
    assert "negotiate_price" in names


def test_negotiate_price_schema_shape():
    spec = next(d for d in registry.TOOL_DEFINITIONS
                if d["function"]["name"] == "negotiate_price")
    props = spec["function"]["parameters"]["properties"]
    assert "product_id" in props
    assert "buyer_offer" in props
    assert spec["function"]["parameters"]["required"] == ["product_id"]
