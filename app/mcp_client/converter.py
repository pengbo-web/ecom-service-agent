"""MCP 工具 schema → OpenAI function calling 格式转换。"""


def mcp_tools_to_openai(mcp_tools: list) -> list[dict]:
    """将 MCP Tool 列表转换为 OpenAI function calling 格式。

    剔除保留参数 ctx_user_id / ctx_token(身份/凭据透传用，不该暴露给模型——否则模型会看到并伪造)。
    """
    _RESERVED = ("ctx_user_id", "ctx_token")
    result = []
    for tool in mcp_tools:
        schema = dict(tool.inputSchema or {})
        props = dict(schema.get("properties") or {})
        for k in _RESERVED:
            props.pop(k, None)
        schema["properties"] = props
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in _RESERVED]
        result.append({
            "type": "function",
            "function": {"name": tool.name, "description": tool.description or "",
                         "parameters": schema},
        })
    return result
