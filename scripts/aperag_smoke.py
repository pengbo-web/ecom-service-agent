"""ApeRAG 检索冒烟:验证 collection 就绪、混合检索能命中政策文档。

用法: .venv/Scripts/python.exe scripts/aperag_smoke.py [查询]
依赖 .env 的 APERAG_BASE_URL / APERAG_API_KEY / APERAG_COLLECTION_ID。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许以脚本方式直跑

import httpx

from app.config.settings import settings


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "七天无理由退货怎么退"
    if not settings.aperag_api_key or not settings.aperag_collection_id:
        print("缺少 APERAG_API_KEY / APERAG_COLLECTION_ID,先按手册完成初始化")
        return 1
    url = (f"{settings.aperag_base_url.rstrip('/')}/api/v1/collections/"
           f"{settings.aperag_collection_id}/searches")
    resp = httpx.post(
        url,
        json={"query": query,
              "vector_search": {"topk": 3, "similarity": settings.aperag_min_similarity},
              "fulltext_search": {"topk": 3},
              "rerank": False},
        headers={"Authorization": f"Bearer {settings.aperag_api_key}"},
        timeout=30,
    )
    print("HTTP", resp.status_code)
    if resp.status_code != 200:
        print(resp.text[:300])
        return 1
    items = resp.json().get("items") or []
    print(f"命中 {len(items)} 条:")
    for it in items[:5]:
        src = str(it.get("source") or "")
        print(f"[{it.get('recall_type')}] score={float(it.get('score') or 0):.3f} src={src}")
        print("   ", (it.get("content") or "")[:80].replace("\n", " "))
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
