from app.agent.tools.result_store import ToolResultStore
from app.agent.tools.read_result import read_tool_result
from app.agent.tools import result_store as rs


def _store(tmp_path):
    import itertools
    ids = itertools.count(1)
    return ToolResultStore(str(tmp_path / "tr"), id_factory=lambda: f"tr_{next(ids)}")


def test_save_and_load_roundtrip(tmp_path):
    s = _store(tmp_path)
    ref = s.save("x" * 100)
    got = s.load(ref, offset=0, length=1000)
    assert got["content"] == "x" * 100
    assert got["total_chars"] == 100
    assert got["has_more"] is False


def test_chunked_read(tmp_path):
    s = _store(tmp_path)
    ref = s.save("abcdefghij")           # 10 字符
    a = s.load(ref, offset=0, length=4)
    assert a["content"] == "abcd" and a["has_more"] is True
    b = s.load(ref, offset=4, length=4)
    assert b["content"] == "efgh" and b["has_more"] is True
    c = s.load(ref, offset=8, length=4)
    assert c["content"] == "ij" and c["has_more"] is False


def test_missing_ref_returns_error(tmp_path):
    s = _store(tmp_path)
    assert "error" in s.load("tr_nope")


def test_illegal_ref_rejected(tmp_path):
    s = _store(tmp_path)
    assert "error" in s.load("../etc/passwd")     # 防路径穿越


def test_clear(tmp_path):
    s = _store(tmp_path)
    ref = s.save("data")
    s.clear()
    assert "error" in s.load(ref)


def test_read_tool_result_uses_default_store(tmp_path):
    s = _store(tmp_path)
    rs.set_result_store(s)
    ref = s.save("hello world")
    out = read_tool_result(ref)
    assert out["content"] == "hello world"
    rs.set_result_store(None)            # 复位,避免污染其它用例
