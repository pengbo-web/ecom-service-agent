"""规则快路径：高频简单意图直接规则回复，跳过 Agent（省 LLM 成本/延迟）。

规则优先 + LLM 兜底：命中则秒回；未命中返回 None，交给正常 Agent 流程。
借鉴 XianyuAutoAgent 的规则路由思路。
"""

import re
from typing import Optional

# 每项：(意图, 判定正则, 回复)。仅匹配"纯寒暄/感谢/告别"类短消息。
_RULES = [
    ("greeting",
     re.compile(r"^(你好|您好|哈喽|嗨|在吗|在么|hi|hello)[\s!！~。.]*$", re.IGNORECASE),
     "您好，我是并夕夕智能客服小夕 😊 请问需要查订单、看物流、咨询商品还是售后呢？"),
    ("thanks",
     re.compile(r"(谢谢|感谢|多谢|thx|thanks|辛苦了)", re.IGNORECASE),
     "不客气～能帮到您就好 😊 还有其他可以帮您的吗？"),
    ("bye",
     re.compile(r"^(再见|拜拜|bye|goodbye|没事了|不用了)[\s!！~。.]*$", re.IGNORECASE),
     "感谢您的咨询，祝您购物愉快，欢迎下次光临并夕夕 👋"),
]


def match_fast_path(text: str) -> Optional[dict]:
    t = (text or "").strip()
    if not t or len(t) > 20:      # 过长的一律走 Agent
        return None
    for intent, pat, reply in _RULES:
        if pat.search(t):
            return {"intent": intent, "reply": reply}
    return None
