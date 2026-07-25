"""文档→领域标签:意图过滤检索的数据面(对齐《企业级智能客服》2.2 三级索引一二级)。

标签是"该文档主要服务哪些领域"的软标注:boost 模式只影响排序绝不丢行;
strict 模式过滤但空结果回退全量;未收录的文档视为全域(宁多勿漏)。
"""

from app.config.settings import settings

DOC_DOMAIN_TAGS: dict[str, set] = {
    "退换货政策": {"aftersale"},
    "售后维修与三包": {"aftersale"},
    "投诉与纠纷处理": {"aftersale"},
    "账户与安全": {"aftersale"},
    "物流异常与赔付标准": {"midsale", "aftersale"},
    "发票与支付说明": {"midsale", "aftersale"},
    "订单管理规则": {"midsale"},
    "配送说明": {"presale", "midsale"},
    "优惠券与促销规则": {"presale"},
    "价格保护政策": {"presale", "aftersale"},
    "会员权益": {"presale", "aftersale"},
    "特殊品类服务规则": {"presale", "aftersale"},
    "常见问题FAQ": {"presale", "midsale", "aftersale"},
}


def _doc_tags(doc: str) -> set | None:
    name = (doc or "").rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return DOC_DOMAIN_TAGS.get(name)


def _doc_matches(doc: str, domain: str) -> bool:
    tags = _doc_tags(doc)
    if tags is None:
        return True          # 未收录文档视为全域,不误杀
    return domain in tags


def rank_by_domain(rows: list, domain: str | None) -> list:
    """按检索域调整行序:off/无域=原样;boost=匹配前置(稳定);strict=过滤(空则回退)。"""
    mode = settings.recall_domain_mode
    if mode == "off" or not domain or not rows:
        return rows
    if mode == "strict":
        matching = [r for r in rows if _doc_matches(r.get("doc", ""), domain)]
        return matching if matching else rows
    # boost:只前置**明确**标了本域的行;未收录文档不升不降,与其余行保持原相对顺序
    def _tagged(r) -> bool:
        tags = _doc_tags(r.get("doc", ""))
        return tags is not None and domain in tags
    matching = [r for r in rows if _tagged(r)]
    others = [r for r in rows if not _tagged(r)]
    return matching + others
