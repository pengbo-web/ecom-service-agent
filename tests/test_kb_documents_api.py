"""知识库文档管理端点。

把"写"这一半搬进本项目的管理端,免得店主为了改一份政策文档去开另一个系统。
检索侧不变(仍是 `aperag_search` 读同一个 collection)。

**契约已在真服务上验证过**(`scripts/aperag_write_smoke.py`,退出码 0):
API 写入的文档与 ApeRAG UI 上传的文档落在同一 collection、走同一条索引流水线、
被同一次检索并排召回。
"""

from __future__ import annotations

import inspect
import textwrap

import pytest

from app.api import app as app_module


def _src(fn_name: str, until: str) -> str:
    src = inspect.getsource(app_module)
    start = src.index(f"def {fn_name}")
    return src[start:src.index(until, start)]


# ---------- 上传:一步到位 + 后端把关 ----------

def test_upload_confirms_in_the_same_call():
    """上传即确认。

    实测未 confirm 的文档**不出现在 list_documents 里**——拆两步交给前端串,
    一旦 confirm 失败或用户中途关页面,就留下一份查不到、也没法在界面上删掉的
    孤儿,还占着临时区。
    """
    body = _src("kb_upload_document", "@app.delete")
    assert "upload_document_bytes" in body
    assert "confirm_documents" in body
    up = body.index("upload_document_bytes")
    cf = body.index("confirm_documents")
    assert up < cf, "必须先上传再确认"


def test_upload_reports_the_unconfirmed_orphan():
    """确认失败要如实报错并带上 document_id,不能假装成功。"""
    body = _src("kb_upload_document", "@app.delete")
    assert "永远不会被检索到" in body
    assert '"document_id": doc_id' in body


def test_upload_enforces_type_and_size_on_the_server():
    """类型与体积必须在后端卡。

    这个页面能改客服的政策依据——传错一份文档,全店客服的回答口径当场就变了。
    只靠前端 accept 属性等于没有把关。
    """
    body = _src("kb_upload_document", "@app.delete")
    assert "_KB_ALLOWED_EXT" in body
    assert "_KB_MAX_BYTES" in body
    assert "415" in body and "413" in body


def test_upload_reads_in_chunks():
    """分块读并随读随判:一次性 read() 会先把整个请求体读进内存,
    那样体积上限根本约束不到内存占用(与技能包上传同一手法)。"""
    body = _src("kb_upload_document", "@app.delete")
    assert "_UPLOAD_CHUNK_BYTES" in body
    assert "await file.read(" in body


def test_upload_does_not_wait_for_indexing():
    """索引异步(实测约 15 秒),端点不能等它完成。

    等着就把一次上传变成 15 秒的同步阻塞。

    用 AST 而不是文本搜:分块读上传体本身就是个 `while True`,而注释里也写着
    "不该轮询"——按文本搜会把合法的读循环和解释文字一起判成违规
    (本项目已两次踩过这个坑)。这里只看**真实的调用节点**。
    """
    import ast

    body = _src("kb_upload_document", "@app.delete")
    tree = ast.parse(textwrap.dedent(body))
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for polling in ("sleep", "list_documents"):
        assert polling not in called, f"上传端点不该调用 {polling} 等索引"


def test_blocking_work_goes_to_threadpool():
    """阻塞段丢线程池:本服务主业是 SSE 流式对话,把 HTTP 往返放在事件循环上跑,
    一次上传就会冻住所有在途的流。"""
    for fn, until in (("kb_upload_document", "@app.delete"),
                      ("kb_list_documents", "@app.post"),
                      ("kb_delete_document", "@app.get")):
        assert "run_in_threadpool" in _src(fn, until), fn


# ---------- 列表:状态不翻译 ----------

def test_index_status_is_passed_through_unchanged():
    """索引状态下发上游原值,不翻译成自己一套词。

    终态是 **ACTIVE** 而不是 COMPLETE——项目里此前两处写成 COMPLETE,导致轮询
    永远等不到终态、恒定报"索引未完成",而索引其实早就建好了。多翻译一层就多
    一处会和上游枚举漂移的地方。

    只检查**字符串字面量**,不搜整段源码:上面这段注释里就写着 COMPLETE。
    """
    import ast

    body = _src("kb_list_documents", "@app.post")
    tree = ast.parse(textwrap.dedent(body))
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "vector_index_status" in literals
    assert "COMPLETE" not in literals, "不该出现 COMPLETE 这个不存在的状态"


def test_list_distinguishes_unreachable_from_empty():
    """读不到与"知识库是空的"是两件事,不能都返回空列表。"""
    body = _src("kb_list_documents", "@app.post")
    assert "502" in body


# ---------- collection 归属 ----------

def test_kb_uses_the_policy_collection_explicitly():
    """本端点管理的就是政策库,显式取 `aperag_collection_id` 是对的。

    与 `aperag_writer` 的"绝不回落该 id"不冲突:那条针对的是经验/教训/洞察这类
    **新内容**——ApeRAG 的检索请求没有元数据过滤,混进政策库会让买家问退货政策
    时召回一段"历史话术"。
    """
    src = inspect.getsource(app_module)
    body = src[src.index("def _kb_collection"):src.index("def kb_list_documents")]
    assert "aperag_collection_id" in body
    assert "503" in body, "未配置时要明确不可用,而不是拿空 id 去请求"


def test_all_kb_endpoints_require_admin():
    """知识库能改客服的政策依据,影响面比"批准一条触达"更大。"""
    src = inspect.getsource(app_module)
    for path in ('"/api/admin/kb/documents"',
                 '"/api/admin/kb/documents/upload"',
                 '"/api/admin/kb/documents/{doc_id}"'):
        i = src.index(path)
        assert "admin_auth" in src[i:i + 200], path


# ---------- 二进制安全 ----------

def test_binary_upload_does_not_go_through_the_str_path():
    """PDF/DOCX 必须走 bytes 那条路。

    `upload_document` 会对内容做 `encode("utf-8")`,对二进制文件直接抛异常或
    产出坏字节——而这类文件恰恰是店主最常上传的。

    用 AST 比对**被调用的函数名**,而不是搜子串:`upload_document_bytes` 里就
    含着 `upload_document`。
    """
    import ast
    import inspect as _i

    from app.knowledge import aperag_writer as w

    assert list(_i.signature(w.upload_document_bytes).parameters) ==         ["collection_id", "filename", "payload"]

    body = _src("kb_upload_document", "@app.delete")
    tree = ast.parse(textwrap.dedent(body))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "upload_document_bytes" in called
    assert "upload_document" not in called, "管理端上传不该走 str 版"


@pytest.mark.parametrize("name,expect", [
    ("a.md", "text/markdown"),
    ("a.txt", "text/plain"),
    ("a.pdf", "application/pdf"),
    ("a.bin", "application/octet-stream"),
])
def test_content_type_matches_extension(name, expect):
    """PDF/DOCX 不能声明成 text/markdown。

    上游要靠 content-type 选解析器,声明错了会把二进制当文本切出乱码分块,
    而那些乱码会被向量化、之后一直混在召回结果里——删掉原文档也补救不了
    已经被召回过的那些轮次。
    """
    from app.knowledge.aperag_writer import _content_type_for

    assert _content_type_for(name) == expect
