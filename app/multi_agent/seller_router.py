"""卖家域路由:把店主的话分给参谋(analyst)或增长(growth)。

与买家 Router 的差别只有取值域和兜底目标。兜底刻意选 **analyst**(只读)——
路由不确定时应该落到不会产生任何副作用的那一侧。
"""

from typing import List, Optional

from openai import OpenAI

from app.prompts.seller_agents import SELLER_ROUTER_PROMPT

SELLER_AGENTS = {"analyst", "growth"}
SELLER_DEFAULT = "analyst"          # 兜底落只读侧


class SellerRouter:
    """用 LLM 对店主意图分类,路由到卖家侧画像。"""

    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def route(self, user_input: str, history: Optional[List[dict]] = None) -> str:
        recent_context = ""
        if history:
            lines = []
            for m in history[-4:]:
                if m.get("role") not in ("user", "assistant"):
                    continue
                content = m.get("content", "")
                if content and len(content) < 200:
                    role = "店主" if m["role"] == "user" else "助手"
                    lines.append(f"{role}: {content}")
            if lines:
                recent_context = "\n最近对话：\n" + "\n".join(lines) + "\n"

        prompt = SELLER_ROUTER_PROMPT.format(user_input=user_input)
        if recent_context:
            prompt = recent_context + "\n" + prompt

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=10,
        )
        raw = (response.choices[0].message.content or "").strip().lower()
        for key in SELLER_AGENTS:
            if key in raw:
                return key
        return SELLER_DEFAULT
