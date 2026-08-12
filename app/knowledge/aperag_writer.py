"""ApeRAG 写入客户端:把经验文档写进向量库(V1)。

项目此前只接了 ApeRAG 的**读**面(`app/agent/recall/external_kb.py` 的
`aperag_search`),写面完全没接——也就是说 ApeRAG 在这里一直是"离线灌好、
运行时只读"的知识库,装的是政策文档,而不是系统自己跑出来的经验。本模块把
写入这一半打通,是「共享向量记忆池」的地基。

---

**三个来自真实 API 契约的约束(读 ApeRAG 的 openapi.yaml 得出,不是推测)**

1. **写入是文件式,不是"插一条记录"。** `POST .../documents/upload` 收
   multipart 文件。所以沉淀的粒度是**一份经验文档**,不是一条对话——这直接
   决定了上层(V2 提炼 worker)必须先把若干通对话归纳成一份文档再上传。
2. **索引是异步的。** `UPLOADED --confirm--> PENDING --> CREATING --> ACTIVE`。
   写完**不能立即检索**。所以本模块只能被离线 worker 调用,绝不能挂在买家会话
   的热路径上等它生效。

   **终态是 `ACTIVE`,不是 `COMPLETE`。** 这里原先写的是 COMPLETE,而 ApeRAG 的
   状态枚举(`aperag/schema/view_models.py`)只有
   `PENDING / CREATING / ACTIVE / DELETING / FAILED`——没有 COMPLETE。等一个不
   存在的状态会永远等不到:冒烟脚本因此恒定报"索引未在 N 秒内完成",而索引其实
   早就建好了。实测真服务上 `PENDING → CREATING → ACTIVE` 用了 15 秒。
   这正是模块顶部说"契约只能对着真服务验证"的那一类错误。
3. **检索请求没有元数据过滤**(`searchRequest` 只有 query + 各模态参数 +
   rerank)。所以"正样本 / 负样本 / 洞察"必须落在**不同 collection**,靠隔离
   而不是靠查询时过滤——本模块的每个函数因此都显式接 `collection_id`,不复用
   `settings.aperag_collection_id`(那是政策知识库的,别混)。

---

**4. 内网调用必须绕过系统代理。** 本模块的目标(默认 `127.0.0.1:8100`)是内网
地址,而裸 `httpx.get/post` 默认 `trust_env=True` 会去读 `HTTP_PROXY`。开发/部署
机上挂着代理而 `NO_PROXY` 没带回环时,每一次调用都被塞进代理隧道 → 502/超时,
而本模块的 fail-soft 姿态会把它变成"知识库是空的"。实测:同一份代码在有
`NO_PROXY` 的终端里全通、在 uvicorn 进程里全挂。故统一走
`app.net.internal_http.internal_client`(按目标地址判定,公网部署的 ApeRAG 仍会
正常走代理)。

**fail-soft 姿态**:与 `aperag_search` 一致——任何失败返回 None/False 并记
warning,不抛。调用方是离线 worker,一次写入失败下一轮会重来;让异常穿透只会
把整个提炼流程打断,而前面那些已经花掉的 LLM 成本就白费了。

**但有一条例外必须由上层保证**:内容脱敏是 **fail-closed** 的——脱敏没做或
做失败,就不该走到这里。一条没脱敏的对话进了向量库,之后每次检索都可能把它
捞给别人看,而删除是补救不了"已经被检索过"的。本模块不做脱敏(它不知道内容
语义),但把这条写在这里,免得上层以为写入侧会兜。
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config.settings import settings
from app.net.internal_http import internal_client

logger = logging.getLogger(__name__)


class KnowledgeBaseTimeout(RuntimeError):
    """ApeRAG 响应超时——**服务是活着的,只是这一刻太慢**。

    与"连不上"必须分开。实测:刚上传完一篇文档之后的一段窗口里,文档列表接口连续
    30 秒超时,而 ApeRAG 本身直连 0.5 秒就返回 200——它只是在忙着建索引。两者共用
    一个 `None` 返回值时,界面提示是"知识库服务连不上,请确认 ApeRAG 已启动",
    **把人指去重启一个健康的服务**,而正确的动作是等几十秒再刷新。

    做成异常而不是多一种返回值(比如返回 False):既有调用方全都写着
    `if docs is None`,新增一种假值会让它们悄悄错判。
    """


#: 判定为"超时"的异常类型。httpx 的超时分好几种(连接/读/写/连接池),这里全算
#: 超时——它们的共同点是"请求发出去了或建连中,但没在时限内拿到结果",与
#: ConnectError(压根连不上)在运维动作上完全不同。
_TIMEOUT_EXCS = (httpx.TimeoutException,)


def _base() -> str:
    return settings.aperag_base_url.rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.aperag_api_key}"}


def _ready() -> bool:
    if not settings.aperag_api_key:
        logger.warning("ApeRAG 写入未配置 api_key,跳过")
        return False
    return True


#: 扩展名 → multipart 的 content-type。**PDF/DOCX 不能声明成 text/markdown**:
#: 上游要靠它选解析器,声明错了要么解析失败、要么把二进制当文本切出乱码分块,
#: 而那些乱码会被向量化、之后一直混在召回结果里(删掉原文档也补救不了已经
#: 被召回过的那些轮次)。未知扩展名给 application/octet-stream,让上游自己嗅探,
#: 而不是替它猜一个可能是错的。
_CONTENT_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".txt": "text/plain", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _content_type_for(filename: str) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def upload_document_bytes(collection_id: str, filename: str,
                          payload: bytes) -> Optional[str]:
    """上传**原始字节**,返回 document_id;失败返回 None。

    与 `upload_document` 的分工:那个收 `str`(给 V2 提炼 worker 生成的 markdown
    用),这个收 `bytes`(给管理端上传的真实文件用)。**不能让二进制走 str 那条路**
    ——`content.encode("utf-8")` 对 PDF/DOCX 直接抛 UnicodeDecodeError 或产出坏
    字节,而这类文件恰恰是店主最常上传的。

    **上传完还不会被检索到**,必须再调 `confirm_documents`(见模块顶部约束 2)。
    filename 决定检索结果里的 `source` 字段(`aperag_search` 渲染成"来源"给模型
    看),所以名字要有意义,别用随机串。
    """
    if not _ready():
        return None
    url = f"{_base()}/api/v1/collections/{collection_id}/documents/upload"
    try:
        with internal_client(url, timeout=settings.aperag_write_timeout_s) as c:
            resp = c.post(url,
                          files={"file": (filename, payload,
                                          _content_type_for(filename))},
                          headers=_headers())
        if resp.status_code != 200:
            logger.warning("aperag upload http %s: %s", resp.status_code,
                           resp.text[:200])
            return None
        doc_id = resp.json().get("document_id")
        if not doc_id:
            logger.warning("aperag upload 返回缺少 document_id: %s", resp.text[:200])
            return None
        return str(doc_id)
    except Exception as exc:  # noqa: BLE001 离线写入,失败下一轮重来
        logger.warning("aperag upload 失败 collection=%s file=%s: %s",
                       collection_id, filename, exc)
        return None


def upload_document(collection_id: str, filename: str,
                    content: str) -> Optional[str]:
    """上传一份**文本**文档(UTF-8 编码后走 `upload_document_bytes`)。

    保留这个签名是因为 V2 提炼 worker 产出的是内存里的 markdown 字符串,
    让它先自己 encode 一遍只是把同一件事挪个地方。
    """
    return upload_document_bytes(collection_id, filename, content.encode("utf-8"))


def confirm_documents(collection_id: str, document_ids: list[str]) -> int:
    """把上传的文档转入建索引流程,返回**确认成功的条数**(失败返回 0)。

    返回条数而不是布尔:批量确认时可能部分成功,调用方需要知道到底进去了几条
    才能如实记账——ApeRAG 的响应本身就区分 confirmed_count / failed_count。
    """
    if not _ready() or not document_ids:
        return 0
    url = f"{_base()}/api/v1/collections/{collection_id}/documents/confirm"
    try:
        with internal_client(url, timeout=settings.aperag_write_timeout_s) as c:
            resp = c.post(url, json={"document_ids": list(document_ids)},
                          headers=_headers())
        if resp.status_code != 200:
            logger.warning("aperag confirm http %s: %s", resp.status_code,
                           resp.text[:200])
            return 0
        body = resp.json() or {}
        failed = int(body.get("failed_count") or 0)
        if failed:
            logger.warning("aperag confirm 有 %s 条失败 collection=%s", failed,
                           collection_id)
        return int(body.get("confirmed_count") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("aperag confirm 失败 collection=%s: %s", collection_id, exc)
        return 0


def list_documents(collection_id: str) -> Optional[list[dict]]:
    """列出 collection 下的文档(含各索引状态)。失败返回 None。

    两个用途:①幂等覆盖时按 filename 找旧文档(见 `put_document`);
    ②冒烟脚本轮询"索引建完了没"。**生产写入路径不该轮询索引状态**——那是
    异步的,等它就等于把离线 worker 变成同步阻塞。
    """
    if not _ready():
        return None
    url = f"{_base()}/api/v1/collections/{collection_id}/documents"
    try:
        with internal_client(url, timeout=settings.aperag_write_timeout_s) as c:
            resp = c.get(url, headers=_headers())
        if resp.status_code != 200:
            logger.warning("aperag list documents http %s: %s", resp.status_code,
                           resp.text[:200])
            return None
        body = resp.json() or {}
        return list(body.get("items") or [])
    except _TIMEOUT_EXCS as exc:
        # **超时不等于连不上。** 实测:刚上传完一篇文档之后的一段窗口里,这个列表
        # 接口连续 30 秒超时,而 ApeRAG 本身是活着的(直连 0.5 秒返回 200)——它只是
        # 在忙着建索引。此前这里与连接失败共用一个 except,调用方拿到的都是 None,
        # 于是界面提示"知识库服务连不上,请确认 ApeRAG 已启动",**把人指去重启一个
        # 健康的服务**,而正确的动作是等几十秒再刷新。
        #
        # 抛一个具名异常而不是继续返回 None:调用方需要能区分,而"多一种返回值"
        # (比如返回 False)会让既有的 `docs is None` 判断悄悄错判。
        logger.warning("aperag list documents 超时(服务可能正忙于建索引) "
                       "collection=%s timeout=%ss: %s",
                       collection_id, settings.aperag_write_timeout_s, exc)
        raise KnowledgeBaseTimeout(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("aperag list documents 失败 collection=%s: %s",
                       collection_id, exc)
        return None


def delete_document(collection_id: str, document_id: str) -> bool:
    """删除一份文档。失败返回 False。"""
    if not _ready():
        return False
    url = (f"{_base()}/api/v1/collections/{collection_id}"
           f"/documents/{document_id}")
    try:
        with internal_client(url, timeout=settings.aperag_write_timeout_s) as c:
            resp = c.delete(url, headers=_headers())
        if resp.status_code not in (200, 204):
            logger.warning("aperag delete http %s: %s", resp.status_code,
                           resp.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("aperag delete 失败 collection=%s doc=%s: %s",
                       collection_id, document_id, exc)
        return False


def put_document(collection_id: str, filename: str,
                 content: str) -> Optional[str]:
    """**幂等写入**:同名文档先删后写,返回新的 document_id。

    这是上层(经验提炼 worker)真正该调的入口,而不是直接 upload。

    为什么必须幂等:提炼是**周期性重跑**的——同一簇对话下个月还会被重新提炼
    一次(语料变多、提炼质量改进)。若每次都新增一份,向量库里会堆满同一份
    经验的十几个版本,检索时互相挤占 top-k,而且旧版本的过时结论会和新版本
    一起被召回。用稳定的 filename(如 `experience-<簇指纹>.md`)+ 先删后写,
    库里永远只有最新那一份。

    删除失败**不阻断写入**:宁可短暂出现两份(下一轮会再清一次),也不能因为
    删不掉就让新经验进不去——旧的那份至少还是有效知识,而写入失败等于这一轮
    提炼全白做。这个取舍与"删除是补救不了已被检索过的"那条不冲突:那条说的是
    **脱敏**,这里说的是版本。
    """
    existing = list_documents(collection_id)
    if existing is None:
        logger.warning("列不出已有文档,本次跳过去重直接写入 collection=%s",
                       collection_id)
    else:
        for doc in existing:
            if str(doc.get("name") or doc.get("filename") or "") == filename:
                doc_id = doc.get("id") or doc.get("document_id")
                if doc_id and not delete_document(collection_id, str(doc_id)):
                    logger.warning("旧文档删除失败,仍继续写入新版本 file=%s", filename)

    doc_id = upload_document(collection_id, filename, content)
    if doc_id is None:
        return None
    if confirm_documents(collection_id, [doc_id]) < 1:
        # 上传成功但确认失败:文档停在 UPLOADED,**永远不会被检索到**,而且
        # 会占着临时区。如实返回 None(调用方按写入失败处理),并留下线索。
        logger.warning("aperag 上传成功但确认失败,文档停在 UPLOADED file=%s doc=%s",
                       filename, doc_id)
        return None
    return doc_id
