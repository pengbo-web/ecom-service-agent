"""FAQ 语义缓存:高频问答预热直答(对齐《企业级智能客服》2.5 缓存预热,命中率73%)。

存储:JSON 文件 {"embedding_model", "embedding_dim", "entries": [{"q","a","emb"}]};
种子:scripts/build_faq_cache.py 离线解析 常见问题FAQ.md 的 Q/A 对预热入库。
服务:lookup(query) 用与 KB 相同的 embedding 模型算余弦,>=阈值即命中直答——
政策类 FAQ 与用户无关,可安全跨用户复用;订单/账户类问题由 QU 门控挡在缓存外。

容错红线:embedding 单次调用失败/文件损坏一律当未命中,回退正常 Agent 流程——
这是给"这一次网络抖动/超时"设计的 fail-soft，见 lookup()/lookup_with_state()。

**唯一的例外**(W1 L1):文件里记的 embedding_model/embedding_dim 与当前配置
不一致，说明换了模型却没重建缓存——这是确定性的部署错误，不是瞬时故障，
每次加载都会复现。这一类在 _load() 里直接抛 EmbeddingIndexMismatchError，
不并入上面的容错红线(get_faq_cache() 的调用方不应该把它也吞掉)。

三态区分(W1 L1):lookup_with_state() 返回 hit / miss / unavailable 三种状态——
过去 miss(真的没匹配到)和 unavailable(embedding 调用失败)在 trace 里长得
一模一样，都是"没直答"，这正是整个子系统 404 很久没人发现的原因之一。
lookup() 保留原样(只返回命中字典或 None)，给已有调用方/测试兼容。
"""

import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import settings
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.observability.embedding_health import record_embedding_failure

logger = logging.getLogger(__name__)


@dataclass
class FaqLookupOutcome:
    """FAQ 缓存本轮查询结果，三态互斥。"""

    state: str                  # "hit" | "miss" | "unavailable"
    hit: dict | None = None     # state=="hit" 时是 {"question","answer","score"}
    error: str | None = None    # state=="unavailable" 时的错误摘要(供 trace 诊断)

_singleton = None
_lock = threading.Lock()


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
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # 文件损坏是瞬时/环境问题(而非模型换了没重建),照旧容错清空启动。
            logger.warning("faq cache load failed, start empty", exc_info=True)
            self.entries = []
            return

        entries = data.get("entries", []) if isinstance(data, dict) else []
        stored_model = (data.get("embedding_model") or "") if isinstance(data, dict) else ""
        stored_dim = (data.get("embedding_dim") or 0) if isinstance(data, dict) else 0

        # 维度/模型不匹配 = 换了 embedding 模型却没重建缓存,是确定性的部署
        # 错误(不是这一次网络抖动),必须在这里明确抛出——不能被 get_faq_cache()
        # 的调用方当成"embedding 失败=未命中"的常规容错吞掉。旧文件没记这两个
        # 字段时(stored_model/stored_dim 为空)按"未知,不校验"处理,避免刚上线
        # 这道校验就把历史文件炸掉；本次修复会用 scripts/build_faq_cache.py
        # 重建一次，之后的文件都会带上这两个字段。
        if entries:
            if stored_model and stored_model != settings.embedding_model:
                raise EmbeddingIndexMismatchError(
                    f"FAQ 缓存({self.path})记录的 embedding 模型({stored_model!r})"
                    f"与当前配置(settings.embedding_model={settings.embedding_model!r})"
                    f"不一致。请先运行 `python scripts/build_faq_cache.py` 用当前模型"
                    f"重建缓存，不能直接拿旧模型的向量继续算相似度。"
                )
            if stored_dim and stored_dim != settings.embedding_dimension:
                raise EmbeddingIndexMismatchError(
                    f"FAQ 缓存({self.path})记录的向量维度({stored_dim})与当前配置"
                    f"(settings.embedding_dimension={settings.embedding_dimension})"
                    f"不一致。请先运行 `python scripts/build_faq_cache.py` 重建缓存。"
                )
        self.entries = entries

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dimension,
            "entries": self.entries,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

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
        """命中返回 {"question","answer","score"};未命中/关闭/失败返回 None。

        兼容旧接口:miss 与 unavailable 在这里都折叠成 None,不区分——
        需要区分三态时用 lookup_with_state()（chat.py 的调用点已切过去）。
        """
        return self.lookup_with_state(query).hit

    def lookup_with_state(self, query: str) -> FaqLookupOutcome:
        """三态查询:
        - hit         命中且分数达阈值,附带答案
        - miss        缓存关闭/无候选/分数不够(真的没有匹配的问题)
        - unavailable embedding 调用失败(网络/超时/模型不可用等瞬时故障),
                      附带错误摘要——这是与 miss 唯一的区别，过去两者在
                      trace 里完全一样，是本任务要修的可见性缺口。
        """
        if not settings.faq_cache_enabled or not query or not self.entries:
            return FaqLookupOutcome(state="miss")
        try:
            q_vec = self._embed(query)
        except Exception as exc:      # 容错红线:单次 embedding 失败=未命中,不打断主流程
            logger.warning("faq cache embed failed, treat as miss", exc_info=True)
            record_embedding_failure("faq_cache", exc)
            return FaqLookupOutcome(state="unavailable", error=str(exc)[:200])
        best, best_score = None, 0.0
        for e in self.entries:
            s = _cos(q_vec, e.get("emb") or [])
            if s > best_score:
                best, best_score = e, s
        if best is not None and best_score >= settings.faq_cache_min_score:
            q, a = best.get("q"), best.get("a")
            if q and a:
                return FaqLookupOutcome(
                    state="hit",
                    hit={"question": q, "answer": a, "score": round(best_score, 4)},
                )
        return FaqLookupOutcome(state="miss")


def get_faq_cache() -> FaqCache:
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = FaqCache(settings.faq_cache_path)
    return _singleton


def reset_faq_cache() -> None:
    global _singleton
    _singleton = None
