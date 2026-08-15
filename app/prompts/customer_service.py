"""电商客服系统提示词。

提示词正文已外置到 prompts/customer_service/system_prompt.md，
本模块仅保留常量别名供其它模块引用。
"""

from prompts import get

SYSTEM_PROMPT = get("customer_service/system_prompt")
