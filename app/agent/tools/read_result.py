"""read_tool_result 工具：读回落盘的超大工具结果(支持分段)。"""

from app.agent.tools.result_store import get_result_store


def read_tool_result(ref: str, offset: int = 0, length: int = 4000) -> dict:
    """按 ref 读回此前因过长被存档的工具结果;offset/length 支持分段读。"""
    return get_result_store().load(ref, offset=offset, length=length)
