"""参谋专用差评洞察:全店与分商品的评分统计,词表匹配抽差评关键词。

**只读**:本模块不得出现任何 INSERT/UPDATE/DELETE,且必须归入
`registry.SELLER_ONLY_TOOLS`——全店差评数据是经营信息,买家画像绝不能借一句
"这个店差评多不多"就套到全店维度的数据。

差评关键词的抽取与 `app/evaluation/trace_to_case.py::keywords_from_reply` 共用
同一份匹配实现 `app.utils.keyword_match.match_known_terms`:只在系统已有词表
(订单状态标签/商品名/承诺类术语)里找差评正文里**确实出现**的词,不对文本
分词、不接第三方分词库、不调大模型;含数字的词一律剔除(订单号/日期/金额类,
不可能在下一条差评里原样复现)。两边词表来源不同(评测用回复词表、这里用
差评词表),但贪心最长匹配 + 数字剔除 + 双向包含判重这套算法只存一份——
避免像本仓库已经栽过三次的"手抄表悄悄漂移"那样,复制一份算法变成同一个
bug 的两处埋伏点。

与 `trace_to_case` 不同的一点:那边内部另起一次 `from app.db import get_db`
来读商品名,本模块的聚合函数把 `db` 作为显式参数传入,不在函数内部自行解析——
异常扫描(anomaly.py)和参谋工具各自持有一份在测试里被 monkeypatch 过的 db
引用,如果本模块自己再拿一次全局 get_db(),两边可能读到不同的库(一个是测试
临时库,一个是未被 patch 的真实/单例库),同一次扫描却对着两份不同的数据算。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from app.agent.skills.risk import COMMITMENT_KEYWORDS
from app.agent.tools.user_orders import STATUS_LABELS
from app.db import get_db
from app.utils.keyword_match import match_known_terms

# 差评判定:rating<=2,与 review_stats/anomaly_scan 同口径,三处不能各写一遍
# 字面量慢慢漂移。
BAD_RATING_MAX = 2

# 词表匹配的长度窗口,与 trace_to_case.keywords_from_reply 同一套纪律:
# 词表里最长的词也远短于上限,超限的不可能是本表词。数字过滤在
# match_known_terms 里统一处理,两边共用同一条规则。
_MIN_TERM_LEN = 2
_MAX_TERM_LEN = 10

# 差评洞察一次读取的评价行数上限,给"扫了多少"一个可见边界(与 anomaly.py
# 里 PRODUCT_SCAN_LIMIT 同一目的:不让一次巨量差评悄悄溢出扫描范围而不自知)。
REVIEW_SCAN_LIMIT = 2000


def _term_vocabulary(db) -> list[str]:
    """待匹配词表:订单状态标签 + 承诺类政策术语 + 商品名,长词优先
    (长词先匹配,避免短词抢先占位导致更具体的长词永远输给它的子串)。"""
    terms = set(STATUS_LABELS.values()) | set(COMMITMENT_KEYWORDS)
    try:
        for p in db.all_products():
            name = (p.get("name") or "").strip()
            if name:
                terms.add(name)
    except Exception:
        pass  # 商品库读取失败时跳过,不让抽词本身报错
    return sorted(terms, key=lambda t: (-len(t), t))


def _bad_terms(text: str, db, top_n: int = 5) -> list[str]:
    """从差评正文里抽词表命中的关键词。抽不出就返回 []——总比编造安全。

    匹配算法本身是 `app.utils.keyword_match.match_known_terms`,与
    `trace_to_case.keywords_from_reply` 共用同一份实现,这里只负责组装
    "差评用"的词表(见 `_term_vocabulary`)。
    """
    return match_known_terms(text, _term_vocabulary(db), top_n=top_n,
                             min_len=_MIN_TERM_LEN, max_len=_MAX_TERM_LEN)


def product_review_breakdown(db, window_days: int,
                             limit: int = REVIEW_SCAN_LIMIT) -> list[dict]:
    """按商品聚合窗口内评价:总数/差评数/差评率/均分/差评关键词。只读。

    `db` 由调用方显式传入(参谋工具与异常扫描各自持有已在测试里 monkeypatch
    过的那份引用),本函数不在内部另行解析 get_db()——见模块 docstring。

    这里同样不按订单后续状态过滤评价:一条评价一旦写下就计入统计,后来
    该订单是否被退款不影响它是否算差评——决策与理由见
    `Database.review_stats` 的 docstring(review finding 2)。
    """
    reviews = db.list_reviews(window_days=window_days, limit=limit)
    name_map: dict[str, str] = {}
    try:
        name_map = {p["product_id"]: p["name"] for p in db.all_products()}
    except Exception:
        pass

    ratings_by_sku: dict[str, list[int]] = defaultdict(list)
    bad_texts_by_sku: dict[str, list[str]] = defaultdict(list)
    for r in reviews:
        sku = r.get("sku")
        if not sku:
            continue
        ratings_by_sku[sku].append(int(r["rating"]))
        if int(r["rating"]) <= BAD_RATING_MAX:
            bad_texts_by_sku[sku].append(r.get("content") or "")

    products = []
    for sku, ratings in ratings_by_sku.items():
        total = len(ratings)
        bad_texts = bad_texts_by_sku.get(sku, [])
        bad_count = len(bad_texts)
        products.append({
            "sku": sku,
            "name": name_map.get(sku, sku),
            "total": total,
            "bad_count": bad_count,
            "bad_rate": (bad_count / total) if total else 0.0,
            "avg_rating": (sum(ratings) / total) if total else 0.0,
            "bad_terms": _bad_terms("".join(bad_texts), db, top_n=5),
        })
    return products


def review_insights(window_days: int = 7, top_n: int = 5) -> dict:
    """【店铺参谋专用】评价洞察:全店均分/差评率 + 差评 top 商品与其关键词。

    全只读:本函数不得出现任何 INSERT/UPDATE/DELETE。按差评数 DESC 排序——
    参谋要先看差评最集中的商品,不是评价数量最多的商品。
    """
    db = get_db()
    stats = db.review_stats(window_days=window_days)
    products = product_review_breakdown(db, window_days)
    products.sort(key=lambda p: p["bad_count"], reverse=True)
    top = products[:max(1, int(top_n))]

    return {
        "success": True,
        "window_days": int(window_days),
        "avg_rating": stats["avg_rating"],
        "total": stats["total"],
        "bad_rate": stats["bad_rate"],
        "products": [
            {"sku": p["sku"], "name": p["name"], "avg_rating": p["avg_rating"],
             "bad_count": p["bad_count"], "bad_terms": p["bad_terms"]}
            for p in top
        ],
    }
