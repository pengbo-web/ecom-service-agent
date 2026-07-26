from app.db import get_db


def _searchable_text(product: dict) -> str:
    # name/category 在 DB schema 中可为 NULL(category 尤甚);直接索引会 TypeError,
    # 用 (... or "") 兜底,防单个脏数据行让整个模糊搜索崩溃。
    return " ".join([
        product.get("name") or "",
        product.get("category") or "",
        product.get("description") or "",
        " ".join(str(v) for v in (product.get("specs") or {}).values()),
    ]).lower()


def _matched_keywords(product: dict, keywords: list[str]) -> list[str]:
    """返回该商品命中的关键词子集。"""
    text = _searchable_text(product)
    return [kw for kw in keywords if kw in text]


def _match_score(product: dict, keywords: list[str]) -> int:
    """计算商品与关键词列表的匹配度（命中关键词数量）。"""
    return len(_matched_keywords(product, keywords))


def _public_view(product: dict) -> dict:
    """面向顾客的商品视图：剔除 floor_price（议价底价，绝不能暴露给模型/顾客）。"""
    return {k: v for k, v in product.items() if k != "floor_price"}


def query_product(keyword: str) -> dict:
    """根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。"""
    db = get_db()
    exact = db.get_product(keyword)
    if exact:
        return {"success": True, "products": [_public_view(exact)]}

    keywords = [kw.lower() for kw in keyword.split() if kw.strip()]
    if not keywords:
        keywords = [keyword.lower()]

    scored = [(p, s) for p in db.all_products() if (s := _match_score(p, keywords)) > 0]
    scored.sort(key=lambda ps: ps[1], reverse=True)   # 匹配度高的排前面

    if not scored:
        # 如实返回空,绝不编造不存在的商品(与"结果如实汇报"一致)
        return {
            "success": True,
            "products": [],
            "note": (f"未找到与「{keyword}」匹配的商品。请如实告知顾客暂无此类商品，"
                     f"可建议更换关键词或推荐其他在售品类；切勿编造不存在的商品，也不要用相同关键词重复检索。"),
        }

    best_score = scored[0][1]
    top = [p for p, s in scored if s == best_score]   # 只返回最高匹配档,不掺弱匹配
    result = {"success": True, "products": [_public_view(p) for p in top]}

    # 部分匹配诚实告知:没有商品命中全部关键词时,标出未命中的词,
    # 让模型如实回复"该属性无匹配商品"并推荐替代,而不是误以为检索坏了反复重搜。
    if best_score < len(keywords):
        matched = _matched_keywords(top[0], keywords)
        unmatched = [kw for kw in keywords if kw not in matched]
        result["partial_match"] = True
        result["unmatched_keywords"] = unmatched
        result["note"] = (
            f"没有同时满足「{keyword}」全部条件的商品；以下为最接近的结果，"
            f"但未命中：{'、'.join(unmatched)}。请如实告知顾客该属性（如颜色）暂无匹配商品，"
            f"可推荐现有替代款或询问是否放宽条件；切勿用相同关键词重复检索。"
        )
    return result
