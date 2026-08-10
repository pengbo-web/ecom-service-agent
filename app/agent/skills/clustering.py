"""会话语义聚类:自进化闭环的**输入端**。

替换的是 `synthesizer.INTENT_KEYWORDS` 那张朴素关键词表:

    INTENT_KEYWORDS = [("refund", ["退款","退货"]), ("logistics", ["物流","快递","到哪"]),
                       ("invoice", ["发票"]), ("bargain", ["优惠","便宜","降价"])]

四个桶、先中先得、只看首条 user 消息。测试阶段样本少时看不出问题;**生产上
几十万通对话里,"鞋子穿着挤脚想换大一码"这类根本落不进任何桶,或者落错桶**
——而落桶结果直接决定了合成出什么 skill,输入端的失真会一路传到产出。

语义聚类正是向量该干的活。这也是整个"共享向量记忆池"里**最先值得做**的一块:
它不需要等数据积累(现在做,生产数据涌进来时它已经在位;反过来等数据多了再做,
那批数据会先被关键词聚类糟蹋一遍),也不需要新组件(embedding 能力项目里已经
在跑:FAQ 缓存、KB 检索都用同一个 `Embedder`)。

---

**算法选型:贪心阈值聚类,不引入 sklearn。**

样本量在几百到几千的量级,而且这是**离线**脚本(`skill_synth_enabled` 默认关)。
贪心聚类(逐条比对已有簇心,超过阈值就并入,否则自立门户)是 O(n·k),对这个
量级足够,且:
  - 零新依赖(项目没装 sklearn,为一个离线脚本引入它不划算);
  - 结果可解释——每个簇能指出"它和谁最像、相似度多少",而 KMeans 给不出这个;
  - 不需要预先指定簇数 k(而我们恰恰不知道有几类意图)。

**fail-soft 是硬要求**:embedding 调不通(端点故障/维度不匹配/没配 key)时
一律回落关键词聚类。自进化是离线增强,绝不能因为向量这一步挂了就整个跑不动。
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

#: 归并阈值:与簇心余弦相似度 >= 此值才并入。
#:
#: 取 0.75 而不是更高:同一意图的不同说法("想退货" / "鞋子挤脚想换大一码")
#: 在通用 embedding 下往往落在 0.7~0.85;阈值定太高会退化成"每条自成一簇",
#: 那还不如关键词聚类。定太低则不同意图被揉进一簇,合成出的 skill 会四不像。
#: 这个值应该在真实语料上复核——见模块末尾 `describe_clusters` 的用途。
SIM_THRESHOLD = 0.75

#: 单条样本用于聚类的文本上限。首条 user 消息通常很短;截断只为防异常长文本
#: 把 embedding 请求撑爆。
MAX_TEXT_CHARS = 500


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_all(texts: list[str]) -> list[list[float]] | None:
    """批量 embedding。失败返回 None(调用方据此回落关键词聚类)。"""
    from app.agent.rag.embedder import Embedder
    from app.config.settings import settings

    try:
        embedder = Embedder(api_key=settings.openai_api_key,
                            base_url=settings.openai_base_url,
                            model=settings.embedding_model)
        return embedder.encode([t[:MAX_TEXT_CHARS] for t in texts])
    except Exception:  # noqa: BLE001 离线增强,失败回落关键词
        logger.warning("会话聚类 embedding 失败,回落关键词聚类", exc_info=True)
        return None


def cluster_texts(texts: list[str],
                  threshold: float = SIM_THRESHOLD) -> list[list[int]] | None:
    """把文本按语义聚类,返回**下标分组**;embedding 不可用时返回 None。

    返回下标而不是文本本身:调用方持有的是完整会话样本,只需要知道"哪几条归
    一类",不需要本模块碰它们的其余字段。

    簇心用**首条成员的向量**而不是动态平均:平均会让簇心随并入顺序漂移,
    同一批样本换个顺序就聚出不同结果——离线合成的输入端必须可复现,否则
    "为什么这次合成出的 skill 不一样"永远查不清。
    """
    from app.config.settings import settings

    if not getattr(settings, "skill_semantic_clustering_enabled", True):
        return None          # 关开关 = 调用方回落关键词聚类,逐字节回到改造前
    if not texts:
        return []
    vectors = _embed_all(texts)
    if vectors is None or len(vectors) != len(texts):
        return None

    clusters: list[list[int]] = []
    centroids: list[list[float]] = []
    for i, vec in enumerate(vectors):
        best_j, best_sim = -1, 0.0
        for j, c in enumerate(centroids):
            sim = _cos(vec, c)
            if sim > best_sim:
                best_j, best_sim = j, sim
        if best_j >= 0 and best_sim >= threshold:
            clusters[best_j].append(i)
        else:
            clusters.append([i])
            centroids.append(vec)          # 簇心 = 首条成员,不随并入漂移
    return clusters


def describe_clusters(texts: list[str], clusters: list[list[int]]) -> list[dict]:
    """把聚类结果渲染成可读结构,供离线调参时人工核对。

    存在的理由:`SIM_THRESHOLD` 这个值只能在**真实语料**上定。没有一个能一眼
    看出"这簇里都是些什么"的出口,调阈值就只能靠猜——而猜错的后果(不同意图
    被揉进一簇)要等到合成出一份四不像的 SKILL.md 才发现。
    """
    return [{"size": len(idx),
             "samples": [texts[i][:60] for i in idx[:5]]}
            for idx in clusters]
