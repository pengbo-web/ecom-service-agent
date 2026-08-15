"""记忆提取 Prompt：短期记忆 (STM) 和长期记忆 (LTM) 的事实抽取。
提示词正文已外置到 prompts/memory/*.md。"""

from prompts import get

STM_EXTRACTION_PROMPT = get("memory/stm_extraction")
LTM_EXTRACTION_PROMPT = get("memory/ltm_extraction")
LTM_CURATION_PROMPT = get("memory/ltm_curation")
