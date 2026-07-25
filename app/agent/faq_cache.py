"""FAQ 语义缓存:高频问答预热直答(对齐《企业级智能客服》2.5 缓存预热,命中率73%)。

存储:JSON 文件 {"entries": [{"q": 问题, "a": 答案, "emb": [向量]}]};
种子:scripts/build_faq_cache.py 离线解析 常见问题FAQ.md 的 Q/A 对预热入库。
服务:lookup(query) 用与 KB 相同的 embedding 模型算余弦,>=阈值即命中直答——
政策类 FAQ 与用户无关,可安全跨用户复用;订单/账户类问题由 QU 门控挡在缓存外。

容错红线:embedding 失败/文件损坏一律当未命中,回退正常 Agent 流程。
"""

import json
import logging
import math
from pathlib import Path

from app.config.settings import settings

logger = logging.getLogger(__name__)

_singleton = None


def _cos(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class FaqCache:
    def __init__(self, path: str):
        self.path = Path(path)
        self.entries: list[dict] = []
        self._embedder = None
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", [])
        except Exception:
            logger.warning("faq cache load failed, start empty", exc_info=True)
            self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": self.entries}, ensure_ascii=False),
                             encoding="utf-8")

    def _embed(self, text: str) -> list[float]:
        if self._embedder is None:
            from app.agent.rag.embedder import Embedder
            self._embedder = Embedder(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.embedding_model,
                timeout=settings.recall_kb_timeout_s,
                max_retries=settings.recall_kb_embed_retries,
            )
        return self._embedder.encode_one(text)

    def add(self, question: str, answer: str) -> None:
        self.entries.append({"q": question, "a": answer,
                             "emb": self._embed(question)})
        self._save()

    def lookup(self, query: str):
        """命中返回 {"question","answer","score"};未命中/关闭/失败返回 None。"""
        if not settings.faq_cache_enabled or not query or not self.entries:
            return None
        try:
            q_vec = self._embed(query)
        except Exception:
            logger.warning("faq cache embed failed, treat as miss", exc_info=True)
            return None
        best, best_score = None, 0.0
        for e in self.entries:
            s = _cos(q_vec, e.get("emb") or [])
            if s > best_score:
                best, best_score = e, s
        if best is not None and best_score >= settings.faq_cache_min_score:
            return {"question": best["q"], "answer": best["a"],
                    "score": round(best_score, 4)}
        return None


def get_faq_cache() -> FaqCache:
    global _singleton
    if _singleton is None:
        _singleton = FaqCache(settings.faq_cache_path)
    return _singleton


def reset_faq_cache() -> None:
    global _singleton
    _singleton = None
