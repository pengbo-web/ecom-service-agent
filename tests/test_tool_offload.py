import json

from app.agent.tools.manager import ToolManager
from app.agent.tools.result_store import ToolResultStore
from app.agent.tools import result_store as rs
from app.config.settings import settings


def _tm():
    return ToolManager(use_mcp=False, mcp_server_url="")


def test_small_result_passthrough(tmp_path):
    rs.set_result_store(ToolResultStore(str(tmp_path / "tr")))
    tm = _tm()
    assert tm._maybe_offload("query_order", '{"success": true}') == '{"success": true}'
    rs.set_result_store(None)


def test_large_result_offloaded_and_readable(tmp_path):
    rs.set_result_store(ToolResultStore(str(tmp_path / "tr")))
    tm = _tm()
    big = "P" * (settings.tool_result_max_chars + 500)
    out = json.loads(tm._maybe_offload("query_product", big))
    assert out["truncated"] is True
    assert out["result_ref"].startswith("tr_")
    assert out["total_chars"] == len(big)
    assert len(out["preview"]) == settings.tool_result_preview_chars
    # 用 ref 能读回完整内容(零丢失)
    got = rs.get_result_store().load(out["result_ref"], offset=0, length=len(big))
    assert got["content"] == big
    rs.set_result_store(None)


def test_read_tool_result_is_exempt(tmp_path):
    rs.set_result_store(ToolResultStore(str(tmp_path / "tr")))
    tm = _tm()
    big = "Q" * (settings.tool_result_max_chars + 500)
    # 读取工具的返回即使很大也不被再次落盘(防死循环)
    assert tm._maybe_offload("read_tool_result", big) == big
    rs.set_result_store(None)
