"""评估 Prompt：LLM-as-judge 对客服表现打分（第9期）。

三个 judge 对应需要「理解语义」才能判定、规则代码搞不定的维度：
- ANSWER_QUALITY_PROMPT：回答质量（结果指标）
- HALLUCINATION_PROMPT：是否对照工具返回产生幻觉（结果指标）
- PROCESS_SOUNDNESS_PROMPT：工具选择与顺序是否合理（过程指标）

均要求 temperature=0.0、只输出 JSON（JSON 示例用 {{ }} 转义以兼容 .format）。
提示词正文已外置到 prompts/evaluation/*.md。
"""

from prompts import get

ANSWER_QUALITY_PROMPT = get("evaluation/answer_quality")
HALLUCINATION_PROMPT = get("evaluation/hallucination")
PROCESS_SOUNDNESS_PROMPT = get("evaluation/process_soundness")
