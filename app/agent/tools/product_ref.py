"""商品标识的归一:同一件商品在这个库里有两种写法。

**这不是洁癖,是一个实测到的静默失效。** 数据说话——同一份 ecom.db 里:

    order_items.sku   'HMDP-1'、'ACC-P1'、'SHOE-270-BK-42' …
    carts.sku         '1'、'2'、'ACC-P1'
    products.product_id  'ACC-P1'、'SHOE-270-BK-42' …（没有 'HMDP-1'，也没有 '1'）

`HMDP-{id}` 是 `POST /api/order` 为 hmdp 渠道商品**造**的本地 sku(见
`app/api/app.py` 里 `f"HMDP-{req.item_id}"`),而购物车那条路(`add_to_cart`)直接
把裸 `item_id` 当 sku 存。前端两处传的都是 hmdp 的 `product.id`(裸 id)。

于是任何"拿商品标识做比较"的地方都会静默对不上。已确证两处:

1. **买家侧应答提示对每一个商品都永不注入。** 参谋诊断的 `subject` 来自
   `order_items.sku`(`HMDP-1`),而 `render_buyer_hints` 拿它和前端传的
   `current_item_id`(`1`)比——永远不等。整个"跨 Agent 经验回流"的商品级通路
   是断的,而且不报错、不留日志,只是永远没有提示。
2. **诊断相关性闸对弃单商机永远判"不适用"。** `refund_rate_high` 的 subject 是
   `HMDP-1`,而 `abandoned_cart` 商机的 sku 来自 `carts.sku`(`1`)。闸本身没错,
   是两边的命名空间没对齐。

还有一处**潜在**的(当前数据没触发,但 SQL 层面可证):`abandoned_cart` 用
`LEFT JOIN products ON p.product_id = c.sku` 取单价,而 `products` 里根本没有 `'1'`
这一行(实测 0 行)。一旦出现一条 active 的 hmdp 购物车,金额就取不到、退回中性值
——而 growth.py 那段注释恰好写着"之前没取,导致弃单商机一律按金额未知走中性值…
是白丢的信息"。取是取了,join 键对不上,所以那个修复对 hmdp 商品实际仍未生效。

**本模块只做比较侧的归一,不动存储。** 统一存储要改写路径 + 迁移既有行,风险
高得多;而所有已确证的失效都发生在**比较**那一刻。把归一放在一处具名,比在每个
比较点各写一遍 `startswith("HMDP-")` 可靠——后者迟早漏一处,而漏掉的表现是静默的。
"""

from __future__ import annotations

from typing import Optional

#: `POST /api/order` 为 hmdp 渠道商品造本地 sku 时用的前缀。**唯一来源**:
#: `app/api/app.py` 里那句 `f"HMDP-{req.item_id}"`。这里具名是为了让"这是一个
#: 约定"变得可搜索——它此前只是一个裸字面量。
HMDP_SKU_PREFIX = "HMDP-"


def canonical_item_ref(value: Optional[str]) -> str:
    """把商品标识归一到同一命名空间:剥掉 `HMDP-` 前缀,去空白。

    `canonical_item_ref("HMDP-1") == canonical_item_ref("1") == "1"`

    只剥这一个前缀,不做任何模糊匹配:真实 sku(`ACC-P1`、`SHOE-270-BK-42`)原样
    返回。宁可少归一一种写法,也不能把两个不同商品判成同一个——那会让客服带着
    A 商品的注意事项去回答 B 商品,或者把 A 的诊断写进关于 B 的触达话术。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith(HMDP_SKU_PREFIX):
        return s[len(HMDP_SKU_PREFIX):].strip()
    return s


def same_item(a: Optional[str], b: Optional[str]) -> bool:
    """两个商品标识是否指同一件商品。任一为空 → False(不知道不算相等)。"""
    ca, cb = canonical_item_ref(a), canonical_item_ref(b)
    return bool(ca) and ca == cb


def matches_any_item(target: Optional[str], candidates) -> bool:
    """`target` 是否命中 `candidates` 里的任意一个(归一后比较)。

    空 candidates → False:"不知道涉及哪些商品"不能当"命中"用(这条纪律与
    `collab._diagnosis_applies_to` 一致——宁可少引用一条诊断,也不能对买家说
    一件关于别的商品的事)。
    """
    ct = canonical_item_ref(target)
    if not ct:
        return False
    return any(ct == canonical_item_ref(c) for c in (candidates or []))
