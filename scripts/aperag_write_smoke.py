"""ApeRAG **写入**冒烟:验证 upload → confirm → 建索引 → 能被检索到 这条链是通的。

用法:
    .venv/Scripts/python.exe scripts/aperag_write_smoke.py [collection_id]

不传 collection_id 时用 .env 的 APERAG_COLLECTION_ID。
**注意:它会往该 collection 真写一份文档**,默认结束时删掉;想留下来看就加
`--keep`。

为什么必须有这个脚本(而不是只写单测):写入面的正确性**单测证明不了**。
mock 掉 httpx 之后,你验证的是"我按自己以为的形状发了请求",而真正会出错的
恰恰是"我以为的形状"和服务端实际契约不一致——字段名、multipart 的 part 名、
确认接口收数组还是对象、索引要多久才建完。这些只有对着真服务跑才知道。

这也是 V1 的验收标准本身:方案里写明「必须在真服务上验证,不能只写单测」。
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许以脚本方式直跑

# Windows 控制台默认 GBK,脚本里的 ✅/❌ 会让 print 抛 UnicodeEncodeError——
# 而它抛在"上传已经成功、正准备打印 document_id"的那一行,于是**一次成功的
# 验收看起来像失败**,还留下一份没被清理的测试文档。验收脚本自己不能有这种
# 假失败:重配 stdout 为 UTF-8(errors="replace" 兜底,终端不支持某字符也不该崩)。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.agent.recall.external_kb import aperag_search  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.knowledge import aperag_writer as w  # noqa: E402

#: 冒烟文档里放一句**足够独特**的话:检索验证要能确认"召回的确实是我刚写的
#: 这份",而不是碰巧命中了知识库里别的政策文档。
MARKER = "紫水晶护腕的售后凭证编号是 ZSJ-7788"
FILENAME = "_write_smoke.md"
CONTENT = f"""# 写入冒烟测试文档

这份文档由 scripts/aperag_write_smoke.py 生成,用于验证写入链路。

{MARKER}
"""


def _indexed(collection_id: str, doc_id: str) -> str:
    """取该文档的向量索引状态;取不到返回 "?"。"""
    docs = w.list_documents(collection_id) or []
    for d in docs:
        if str(d.get("id") or d.get("document_id")) == doc_id:
            return str(d.get("vector_index_status") or "?")
    return "?"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("collection_id", nargs="?", default=None)
    parser.add_argument("--keep", action="store_true", help="结束后不删除测试文档")
    parser.add_argument("--wait", type=int, default=120, help="等索引建完的秒数上限")
    args = parser.parse_args()

    cid = args.collection_id or settings.aperag_collection_id
    if not settings.aperag_api_key or not cid:
        print("缺少 APERAG_API_KEY / collection_id,先按 docs/ApeRAG接入-操作手册.md 初始化")
        return 1

    print(f"目标: {settings.aperag_base_url}  collection={cid}")

    print("\n[1/4] 上传文档…")
    doc_id = w.upload_document(cid, FILENAME, CONTENT)
    if not doc_id:
        print("  ❌ 上传失败(看 warning 日志里的 http 状态与响应体)")
        return 2
    print(f"  ✅ document_id={doc_id}")

    print("[2/4] 确认入库(触发建索引)…")
    n = w.confirm_documents(cid, [doc_id])
    if n < 1:
        print("  ❌ 确认失败——文档会停在 UPLOADED,永远不会被检索到")
        return 3
    print(f"  ✅ confirmed={n}")

    print(f"[3/4] 等待索引(最多 {args.wait}s)…")
    deadline = time.time() + args.wait
    status = "?"
    while time.time() < deadline:
        status = _indexed(cid, doc_id)
        print(f"  vector_index_status={status}")
        # 终态是 ACTIVE(不是 COMPLETE)。ApeRAG 的状态枚举只有
        # PENDING / CREATING / ACTIVE / DELETING / FAILED——等 COMPLETE 会永远
        # 等不到,于是这个脚本恒定报"索引未完成",而索引其实早建好了。
        # 实测真服务上 PENDING → CREATING → ACTIVE 用了 15 秒。
        if status in ("ACTIVE", "FAILED"):
            break
        time.sleep(5)
    if status != "ACTIVE":
        print(f"  ⚠️  索引未在 {args.wait}s 内完成(status={status})。"
              "这不一定是失败——建索引是异步的,可能只是慢;但检索验证会跳过。")
        if not args.keep:
            w.delete_document(cid, doc_id)
        return 4

    print("[4/4] 检索验证(能不能召回刚写的内容)…")
    rows = aperag_search("紫水晶护腕的售后凭证编号", top_k=5) or []
    hit = any(MARKER[:12] in str(r.get("text") or "") for r in rows)
    for r in rows[:3]:
        print(f"  - 《{r.get('doc')}》 {str(r.get('text') or '')[:50]}")
    print("  ✅ 召回成功" if hit else "  ❌ 没召回到刚写的内容")

    if not args.keep:
        print("\n清理测试文档…", "✅" if w.delete_document(cid, doc_id) else "❌ 删除失败,请手动清理")
    else:
        print(f"\n--keep:测试文档保留在库里 document_id={doc_id}")
    return 0 if hit else 5


if __name__ == "__main__":
    sys.exit(main())
