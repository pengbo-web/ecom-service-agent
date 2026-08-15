"""Generator ≠ Evaluator:写东西的模型不能给自己打分。

论文(SkillEvo,arXiv 2608.13120)把这一条列为**唯一的架构性硬要求**——编辑器
用一个模型、Verifier/模拟用户/治理层用另一个,理由是自审会造成循环依赖:同一个
模型既产出答案又判定答案好不好,它判的其实是"这像不像我会写的东西"。

本仓库改造前三处全是 `settings.model_name`:

    ① 沙箱里跑的客服 Agent —— 被评的答案是它写的
    ② LLM-as-judge         —— 给那个答案打分的是它自己
    ③ skill 合成/改进的编辑器 —— 门禁在评它自己改出来的 SKILL.md

②对①、③对②都是自审。而门禁的三个指标里 `avg_result_score`(含
`answer_quality` / `faithfulness`)与 `avg_process_score`(含 `process_soundness`)
**全部来自 judge**,也就是说自进化闭环里"候选到底更好了没有"这个判定,
主要由被判定方自己的同款模型给出。

---

**为什么默认值仍然是"同一个模型"。**

换裁判模型要有第二个可用端点。硬把默认值改成某个模型名,会让所有没配那个端点的
部署在第一次跑门禁时报 404 —— 那是把一条架构建议变成一次线上故障。

所以这里的做法是:**留空=沿用主模型(行为逐字节不变),但把"此刻是自审"这件事
在每一份评测报告、每一次门禁结论、管理端页面上如实说出来。**

这与本仓库 `degraded`、`anomaly_scope`、`data_scope`、"未经人工审核"是同一条纪律:
**把"这是什么"说清楚,比把数字做好看更重要。** 一个不知道自己在自审的 81 分,
比一个标着"自审"的 81 分危险得多。

配置方式(`.env`):

    EVAL_JUDGE_MODEL=qwen-max          # 裁判换一个模型即可,端点留空则复用主端点
    EVAL_JUDGE_BASE_URL=https://...    # 需要换端点时才填
    EVAL_JUDGE_API_KEY=sk-...          # 留空复用主 key
    SKILL_EDITOR_MODEL=deepseek-chat   # 写 SKILL.md 的编辑器
"""

from __future__ import annotations

#: 三个角色。命名跟着论文走,免得同一件事在代码里有三种叫法。
ROLE_AGENT = "agent"        # 沙箱里跑的客服 Agent(产出被评的答案)
ROLE_JUDGE = "judge"        # LLM-as-judge(给答案打分)
ROLE_EDITOR = "editor"      # skill 合成/改进的编辑器(写 SKILL.md)


def agent_model() -> str:
    """沙箱里跑客服 Agent 用的模型 —— 就是线上那个,不可替换。"""
    from app.config.settings import settings

    return settings.model_name


def judge_model() -> str:
    """LLM-as-judge 用的模型。留空 = 沿用主模型(此时是自审,见模块 docstring)。"""
    from app.config.settings import settings

    return (settings.eval_judge_model or "").strip() or settings.model_name


def editor_model() -> str:
    """写/改 SKILL.md 的编辑器模型。留空 = 沿用主模型。"""
    from app.config.settings import settings

    return (settings.skill_editor_model or "").strip() or settings.model_name


def judge_client():
    """裁判用的 OpenAI 客户端。没单独配端点/key 时复用主端点的那一套。

    单独成函数而不是让调用方各自拼:换裁判端点只该改一个地方,
    而且**不能顺手把主客户端也换掉**——被评的答案必须仍由线上那个模型产出,
    否则评的就不是线上行为了。
    """
    from app.config.settings import settings
    from app.observability.langfuse_client import make_openai_client

    base_url = (settings.eval_judge_base_url or "").strip() or settings.openai_base_url
    api_key = (settings.eval_judge_api_key or "").strip() or settings.openai_api_key
    return make_openai_client(api_key=api_key, base_url=base_url)


def independence_report() -> dict:
    """当前配置下,评判链上哪几对是独立的、哪几对是自审。

    返回的 `note` 是要**原样贴到评测报告与门禁结论里**的那句话。措辞刻意不说
    "配置错误"——同模型是一个有代价的取舍,不是 bug;但它必须显形。
    """
    agent, judge, editor = agent_model(), judge_model(), editor_model()
    self_review = []
    if judge == agent:
        self_review.append("裁判与被评 Agent 同模型")
    if judge == editor:
        self_review.append("裁判与 skill 编辑器同模型")

    independent = not self_review
    if independent:
        note = f"评判独立:Agent={agent} / 裁判={judge} / 编辑器={editor}"
    else:
        note = ("⚠ 自审(" + "、".join(self_review) + f"):模型均为 {agent}。"
                "judge 打出的 answer_quality / faithfulness / process_soundness "
                "是自评,门禁的 avg_result_score 与 avg_process_score 主要由它们构成"
                "——这些分数可以用来看趋势,不足以作为「候选确实更好」的独立证据。"
                "配 EVAL_JUDGE_MODEL 换一个裁判模型即可解除。")

    return {
        "agent_model": agent, "judge_model": judge, "editor_model": editor,
        "independent": independent, "self_review": self_review, "note": note,
    }
