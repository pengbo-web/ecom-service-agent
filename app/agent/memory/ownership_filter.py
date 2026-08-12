"""长期记忆的归属过滤:别把**别人的订单**从画像里念给这个买家听。

**实测泄漏**(走查安全时抓到,不是构造出来的假设):

`app/sessions/memory/1.json`(用户 `1` 的长期画像)里存着这么一条 fact:

    订单ORD-20240115-001（物流单号SF1234567890）已发货，当前正在派送中，已抵达上海浦东区

而 `ORD-20240115-001` 在库里属于用户 **小明**,`tracking_number` 就是 **SF1234567890**
——**是真数据,不是模型编的**。以用户 `1` 的身份在界面上问这个单号,客服回了:

    我注意到您名下有一笔已发货的Nike鞋订单:ORD-20240115-001 在历史记忆中曾被记录为
    "已发货,物流单号SF1234567890,正在派送中,已抵达上海浦东区"

**归属校验挡住了工具调用,长期记忆却绕过它,把另一个买家的运单号和派送进度讲了出去**,
而且还说成"您名下"。`app/agent/tools/ownership.py` 的 `owned_order` 守的是工具路径;
召回路径(`_long_term_section` 注入 + `recall_user_memory` 工具)对 fact 没有任何归属检查。

**这类污染是一次性的、永久的**:`owned_order` 的门控写着"auth_enabled=False 放行"——
开发期关掉鉴权跑过的对话,会把当时看到的订单写进当时那个 memory user 的画像;之后把鉴权
打开**并不会**清掉已经写进去的东西,它每一轮都继续被注入。所以修在**读取侧**而不是写入侧:
写入侧只能防新的,读取侧连已经被污染的画像一起兜住。

判据与 `owned_order` 保持同一套(同一个真源、同一个门控):

- `auth_enabled=False`:单机教学模式,没有"归属"这回事,不过滤(与 owned_order 一致);
- 订单在库里、且属主不是当前用户 → **整条 fact 丢掉**;
- 订单在库里查不到 → 保留(查不到就没有谁的隐私可泄);
- 查询本身抛错 → **丢掉**(fail-closed:验不了就不念,与 owned_order 拿不到身份即拒同理)。

**整条丢而不做局部涂抹**:fact 是自由文本,把单号抹掉仍会留下"已抵达上海浦东区"这种
同样属于别人的信息,而可靠地改写一句自然语言比丢掉它难得多、也更容易留下残渣。
"""

from __future__ import annotations

import re

#: 候选订单号 token。故意宽:`ORD-20240115-001`、`ORD-20260802-66F8`、`ACC-SHIP-FRESH`
#: 都要能被捞出来。宽不等于误伤——捞出来之后还要**在库里查得到**才会被判为别人的订单,
#: 查不到的一律保留。
#:
#: **不能用 `\b` 做边界**(第一版就是这么写的,结果抓不到那个真实泄漏)。实际文本是
#: `订单ORD-20240115-001（物流单号SF1234567890）`——中文字符在 Python 的 Unicode 正则里
#: **也算 word 字符**,于是 `单` 与 `O` 之间根本没有 `\b`,整条 fact 被判成"没提到任何订单"。
#: 改用"前后不是 ASCII 字母数字"的环视:中文、括号、标点都能正常断开。
_ID_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,7}(?:-[A-Za-z0-9]{2,12}){1,3}(?![A-Za-z0-9])")


def referenced_order_ids(text: str) -> list[str]:
    """从一段文本里捞出所有**可能是订单号**的 token(按出现顺序、去重)。"""
    seen, out = set(), []
    for m in _ID_TOKEN_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _owner_of(order_id: str):
    """订单属主;库里没有这一单返回 None。查询异常向上抛(由调用方按 fail-closed 处理)。"""
    from app.db import get_db
    order = get_db().get_order(order_id)
    return None if order is None else order.get("user")


def fact_belongs_to(content: str, user_id: str) -> bool:
    """这条 fact 可以念给 `user_id` 听吗。

    只要它提到了**任何一笔属于别人的订单**就返回 False。查询异常时返回 False
    (fail-closed:验不了就别念)。
    """
    for oid in referenced_order_ids(content):
        try:
            owner = _owner_of(oid)
        except Exception:      # noqa: BLE001 验不了 = 不念(见模块 docstring)
            return False
        if owner is not None and str(owner) != str(user_id):
            return False
    return True


def filter_owned(contents, user_id: str) -> tuple[list, list]:
    """把一批 fact 正文分成 (可以念的, 被拦下的)。

    返回两份而不是只返回保留的那份:**拦掉多少必须能被观测到**——静默过滤会让
    "记忆里怎么少了一条"变成一个查不动的问题(与本仓库 anomaly_scope / 回流丢弃
    披露同一条纪律)。
    """
    from app.config.settings import settings

    items = list(contents)
    if not getattr(settings, "auth_enabled", False):
        # 单机教学模式没有"归属"这回事,保持既有行为(与 owned_order 的门控一致)
        return items, []
    if not user_id:
        # 拿不到当前用户时不做判定:此处的 user_id 是画像文件自己的属主(不是请求上下文),
        # 空值意味着这份画像本身没有主,谈不上"念给谁听",按不过滤处理。
        return items, []

    kept, dropped = [], []
    for c in items:
        (kept if fact_belongs_to(_text_of(c), user_id) else dropped).append(c)
    return kept, dropped


def _text_of(item) -> str:
    """既接受 `MemoryFact`,也接受纯字符串(两个调用方给的形状不同)。"""
    return item if isinstance(item, str) else str(getattr(item, "content", "") or "")
