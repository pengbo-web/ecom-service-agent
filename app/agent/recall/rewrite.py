"""多轮查询改写:把口语化/指代式的最新消息改写为自包含检索查询。

预召回按用户原句做检索,多轮追问("那运费呢?""第二种呢?")的指代和省略
会让向量/全文检索双双失准——这是客服对话的常态,生产 RAG 的标准前置步。

容错红线:LLM 失败/超时/空输出/首问无上下文,一律返回原句,绝不阻塞主流程。
超时复用 recall_kb_timeout_s(热路径预算);resilient 代理不支持 per-request
timeout 参数时自动降级为无 timeout 参数调用(仍有代理自身的超时兜底)。
"""

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)

_PROMPT = (
    "把用户最新消息改写成一条自包含的知识库检索查询:结合最近对话补全其中的"
    "指代与省略;如果已经自包含,原样返回。只输出改写后的查询,不要任何解释。"
)


def rewrite_for_recall(client, model: str, messages: list[dict],
                       query: str | None) -> str | None:
    if not settings.recall_rewrite_enabled or not query:
        return query
    users = [m.get("content", "") for m in messages if m.get("role") == "user"]
    if len(users) < 2:
        return query          # 首问没有可补全的上下文,省一次 LLM
    recent = users[-settings.recall_rewrite_max_turns:]
    context = "\n".join(f"- {u}" for u in recent[:-1])
    req = dict(
        model=model, temperature=0.0, max_tokens=80,
        messages=[{"role": "system", "content": _PROMPT},
                  {"role": "user", "content": f"最近对话:\n{context}\n\n用户最新消息:{query}"}],
    )
    try:
        try:
            resp = client.chat.completions.create(
                timeout=settings.recall_kb_timeout_s, **req)
        except TypeError:      # 代理不接受 per-request timeout
            resp = client.chat.completions.create(**req)
        text = (resp.choices[0].message.content or "").strip()
        return text or query
    except Exception:
        logger.warning("recall query rewrite failed, use raw query", exc_info=True)
        return query
