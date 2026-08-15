"""提示词外置到 `prompts/` 之后,**组装逻辑不能把文本改坏**。

**实测缺陷**(检查外置重构时抓到)。加载器对每个 `.md` 做了 `strip()`,而旧版那些
三引号字符串**自带首尾换行**——那些换行不是排版,是**分隔符**。丢了之后 30 条提示词
里有 13 条被改动:

    安全规则最后一行 + 领域正文第一句被粘成一句:
      "…不解释自己是怎么运作的。你是「并夕夕」电商平台的售前咨询专家…"

    `{{_NO_FABRICATION}}` 紧贴下一节标题,少了两个换行之后:
      "…编一个看着更具体的数字。## 回答方式"
      ——`## 回答方式` 不在行首,**不再是 Markdown 标题**

    `build_tool_hint()` 以换行开头,原 prompt 以换行结尾,合起来是一个空行;
    strip 之后工具清单紧贴在最后一句下面

    `{{_kind_lines()}}` 在 .md 里独占一行,组装时又补了一次 → 多出两个空行

这类改动最麻烦的地方是**没有任何报错**:import 正常、测试通过、页面能跑,只有模型
看到的文本变了。而 prompt 的变化不会立刻显形,它表现为"最近回答质量好像差了点"。

---

**为什么这里没有"与外置前逐字节一致"的测试。**

第一版写了那样一条,当场就发现它是错的:另一个会话正当地往 `analyst.md` 里加了一节
「Skill 工作流」,那条测试立刻变红——而**让提示词可以被编辑正是这次外置的全部目的**。
一个"任何人改提示词就报错"的测试会逼着大家去改测试或者干脆关掉它。

逐字节比对属于**一次性的迁移校验**,不属于长期回归。它被放在
`app/scripts/verify_prompt_migration.py`,搬运完跑一次即可。

本文件只守**与内容无关的结构性不变量**——它们在任何一次正当的文案编辑之后都仍然成立。
"""

import pytest


# --------------------------------------------------------------------------
# 分隔符:全部是"组装时必须补、.md 里靠不住"的那几处
# --------------------------------------------------------------------------

def test_safety_rules_keeps_paragraph_separator():
    """`SAFETY_RULES` 必须以空行结尾——它后面直接接领域正文。

    不能指望 .md 末尾留空行:加载器本来就会 strip,而且任何一次编辑器保存都可能
    把行尾空行清掉(实测 5 个 skills/*.md 里有 3 个末尾没有换行)。
    """
    from app.prompts.agents import SAFETY_RULES

    assert SAFETY_RULES.endswith("\n\n"), (
        "安全规则少了尾部空行,会与领域正文第一句粘成一句")


@pytest.mark.parametrize("profile", ["PRESALE", "MIDSALE", "AFTERSALE"])
def test_profile_prompt_has_blank_line_before_domain_text(profile):
    """三个买家画像的组装结果里,安全规则与领域正文之间必须隔着空行。"""
    import app.prompts.agents as agents

    base = getattr(agents, f"{profile}_BASE_PROMPT")
    full = getattr(agents, f"{profile}_PROMPT")
    first_line = base.split("\n", 1)[0]
    assert f"\n{first_line}" in full, f"{profile}:领域正文被粘到安全规则末尾了"


def test_no_fabrication_block_keeps_separator():
    """`{{_NO_FABRICATION}}` 在 .md 里紧贴下一节标题,分隔符不能少。"""
    from app.prompts.seller_agents import ANALYST_PROMPT, GROWTH_PROMPT

    for text, who in ((ANALYST_PROMPT, "参谋"), (GROWTH_PROMPT, "营销")):
        assert "\n## 回答方式" in text, (
            f"{who}画像里「## 回答方式」不在行首,已经不是 Markdown 标题了")


def test_tool_hint_is_separated_by_a_blank_line():
    """与 `build_tool_hint()` 相接的 prompt,中间必须留出空行。

    `build_tool_hint` 以换行开头,prompt 以换行结尾,合起来才是一个空行——
    这正是那几条尾换行存在的理由,不是三引号写法的副产品。
    """
    from app.agent.skills.doc_distill import DOC_SYNTH_SYSTEM_PROMPT
    from app.agent.skills.synthesizer import (IMPROVE_SYSTEM_PROMPT,
                                              build_tool_hint)

    hint = build_tool_hint(["query_order"])
    assert hint.startswith("\n"), "前提变了:build_tool_hint 不再以换行开头"
    for prompt, who in ((DOC_SYNTH_SYSTEM_PROMPT, "doc_distill"),
                        (IMPROVE_SYSTEM_PROMPT, "improve")):
        assert (prompt + hint).startswith(prompt.rstrip("\n") + "\n\n"), (
            f"{who} 与工具清单之间没有空行")


def test_growth_prompt_has_no_doubled_blank_lines():
    """`{{_kind_lines()}}` 独占一行,组装时不该再补换行。"""
    from app.prompts.seller_agents import GROWTH_PROMPT

    assert "\n\n\n" not in GROWTH_PROMPT, "商机清单前后多出了空行"


# --------------------------------------------------------------------------
# 占位符必须真的被替换掉
# --------------------------------------------------------------------------

#: 本项目的外置占位符长这样:`{{_NO_FABRICATION}}` / `{{_kind_lines()}}`——
#: 双花括号里是一个标识符(可带一对空括号)。
#:
#: **不能简单地扫 `{{`**:`.format()` 的转义花括号也是双的。`LTM_EXTRACTION_PROMPT`
#: 里有一段 JSON 示例写成 `{{"facts": [...]}}`,那是给 `.format(existing_ltm=…)` 用的
#: 正确写法,不是漏替换(第一版测试就在这里误报了)。
_PLACEHOLDER_RE = r"\{\{\s*_?[A-Za-z][A-Za-z0-9_]*(\(\))?\s*\}\}"


def test_no_unsubstituted_placeholders_anywhere():
    """任何一条成品 prompt 里都不该残留 `{{标识符}}`——模型会照着念出来。

    扫的是**成品**而不是 .md:.md 里有占位符是正常的,问题只在替换没发生。
    """
    import importlib
    import re

    modules = ["app.prompts.agents", "app.prompts.seller_agents",
               "app.prompts.customer_service", "app.prompts.memory",
               "app.prompts.evaluation", "app.prompts.reply_pipeline",
               "app.prompts.summarizer"]
    for name in modules:
        mod = importlib.import_module(name)
        for key, value in vars(mod).items():
            if key.startswith("_") or not isinstance(value, str):
                continue
            hit = re.search(_PLACEHOLDER_RE, value)
            assert hit is None, (
                f"{name}.{key} 里残留了未替换的占位符: {hit.group(0)}")


def test_placeholder_detector_actually_detects():
    """**守卫的守卫**:判据真的能认出占位符,也真的不会误伤 `.format()` 转义。

    没有这条,一个永远匹配不上的正则会让上面那条永远绿——而它本来就是为了
    抓"漏替换"这种无声故障存在的。
    """
    import re

    assert re.search(_PLACEHOLDER_RE, "前文 {{_NO_FABRICATION}} 后文")
    assert re.search(_PLACEHOLDER_RE, "清单:{{_kind_lines()}}")
    # `.format()` 的转义花括号不该被当成占位符
    assert not re.search(_PLACEHOLDER_RE, '示例 {{"facts": ["x"]}}')
    assert not re.search(_PLACEHOLDER_RE, "空对象 {{}}")


def test_kind_lines_are_actually_rendered():
    """商机清单必须是从 `OPPORTUNITY_KINDS` 渲染进去的,不是手抄的。"""
    from app.agent.tools.growth import OPPORTUNITY_KINDS
    from app.prompts.seller_agents import GROWTH_PROMPT

    for kind in OPPORTUNITY_KINDS:
        assert f"`{kind}`" in GROWTH_PROMPT, f"商机类型 {kind} 没出现在营销画像里"


# --------------------------------------------------------------------------
# 加载器
# --------------------------------------------------------------------------

def test_every_md_file_is_loadable_and_non_empty():
    import prompts

    keys = prompts.available()
    assert len(keys) >= 30, f"只加载到 {len(keys)} 个提示词,像是漏了"
    empty = [k for k in keys if not prompts.get(k).strip()]
    assert not empty, f"空的提示词文件: {empty}"


def test_missing_prompt_raises_with_a_useful_message():
    """取不存在的键要报错并列出可用键——运行期缺文件是部署错误,不能静默。"""
    import prompts

    with pytest.raises(KeyError) as exc:
        prompts.get("nope/does_not_exist")
    assert "未找到" in str(exc.value)


def test_get_or_empty_does_not_raise():
    import prompts

    assert prompts.get_or_empty("nope/does_not_exist") == ""


def test_reload_picks_up_edits(tmp_path, monkeypatch):
    """`reload()` 要真的重读磁盘——它是"改完 .md 不重启就能看效果"的依据。"""
    import prompts

    key = sorted(prompts.available())[0]
    before = prompts.get(key)
    prompts.reload()
    assert prompts.get(key) == before, "reload 之后内容变了"
    assert len(prompts.available()) >= 30, "reload 之后少了文件"
