"""一次性校验:提示词外置到 `prompts/` 之后,组装出来的成品与外置前逐字节相同。

    python -m app.scripts.verify_prompt_migration                 # 对默认基线比
    python -m app.scripts.verify_prompt_migration --baseline abc1 # 指定基线 commit

**为什么是脚本而不是测试。** 逐字节比对只在"搬运刚做完、还没人动过文案"的那一刻
成立。一旦有人正当地编辑提示词——而那正是外置的目的——它必然变红。第一版确实写成了
测试,当场就被另一个会话给 `analyst.md` 加的一节「Skill 工作流」打红了。
一个"任何人改提示词就报错"的测试,只会逼人去改测试或关掉它。

长期守护的是**与内容无关的结构性不变量**,在
`tests/test_prompt_externalization.py`(分隔符、占位符替换、加载器)。

**这个脚本抓到过什么**(2026-08-16 那次跑):30 条里 13 条不一致——

    8 条  分隔符被 strip 掉,安全规则与领域正文粘成一句、`## 回答方式` 不在行首
    5 条  尾部换行被 strip 掉,其中 3 条要与 `build_tool_hint()` 相接,空行没了
"""

from __future__ import annotations

import argparse
import ast
import importlib
import subprocess
import types

#: 外置重构的前一个 commit。
DEFAULT_BASELINE = "c1a6f2f"

#: `app/prompts/` 下的模块——这些能整份 exec(依赖很轻)。
PROMPT_MODULES = ["agents", "customer_service", "evaluation", "memory",
                  "reply_pipeline", "seller_agents", "summarizer"]

#: 原本内联在业务模块里的提示词。这些**不 exec 历史版本**,只用 AST 取字面量:
#: 它们会拉起 db/settings 一整条依赖链,执行旧版既慢又可能因环境变化而失败,
#: 而我们要的只是那几个常量。
INLINE_MODULES = {
    "app/agent/skills/doc_distill.py": "app.agent.skills.doc_distill",
    "app/agent/skills/golden_corpus.py": "app.agent.skills.golden_corpus",
    "app/agent/skills/synthesizer.py": "app.agent.skills.synthesizer",
    "app/agent/skills/user_modeling.py": "app.agent.skills.user_modeling",
    "app/agent/understanding.py": "app.agent.understanding",
    "app/multi_agent/collab.py": "app.multi_agent.collab",
    "app/multi_agent/shared_context.py": "app.multi_agent.shared_context",
}

MIN_LEN = 20        # 短字符串不是提示词(键名、分隔符之类)


def _git_show(ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{ref}:{path}"],
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 and r.stdout else None


def _baseline_prompt_module(baseline: str, name: str) -> dict[str, str]:
    """整份 exec 历史版本的 `app/prompts/<name>.py`,取出公开的长字符串。"""
    src = _git_show(baseline, f"app/prompts/{name}.py")
    if src is None:
        return {}
    mod = types.ModuleType(f"_baseline_{name}")
    exec(compile(src, f"baseline/{name}.py", "exec"), mod.__dict__)
    return {k: v for k, v in vars(mod).items()
            if not k.startswith("_") and isinstance(v, str) and len(v) >= MIN_LEN}


def _baseline_literals(baseline: str, path: str) -> dict[str, str]:
    """只用 AST 取模块级的字符串字面量赋值,不执行任何代码。"""
    src = _git_show(baseline, path)
    if src is None:
        return {}
    out: dict[str, str] = {}
    for node in ast.parse(src).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
            continue
        value = node.value.value
        if not isinstance(value, str) or len(value) < MIN_LEN:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _compare(label: str, expected: dict[str, str], mod_name: str) -> list[str]:
    mod = importlib.import_module(mod_name)
    problems = []
    for key, old in expected.items():
        new = getattr(mod, key, None)
        if new is None:
            problems.append(f"{label}.{key}: 外置后消失了")
        elif new != old:
            problems.append(
                f"{label}.{key}: 旧 {len(old)} 字 / 新 {len(new)} 字")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE,
                        help=f"外置前的 commit(默认 {DEFAULT_BASELINE})")
    args = parser.parse_args(argv)

    if _git_show(args.baseline, "app/prompts/agents.py") is None:
        print(f"基线 {args.baseline} 不可达(浅克隆?历史被裁剪?),无法比对。")
        return 2

    checked = 0
    problems: list[str] = []

    for name in PROMPT_MODULES:
        expected = _baseline_prompt_module(args.baseline, name)
        checked += len(expected)
        problems += _compare(name, expected, f"app.prompts.{name}")

    for path, mod_name in INLINE_MODULES.items():
        expected = _baseline_literals(args.baseline, path)
        checked += len(expected)
        problems += _compare(mod_name.split(".")[-1], expected, mod_name)

    print(f"基线 {args.baseline} · 比对 {checked} 条提示词")
    if not problems:
        print("全部逐字节一致。")
        return 0
    print(f"{len(problems)} 条不一致:")
    for p in problems:
        print("  -", p)
    print()
    print("提示:若这些是**有意的文案修改**,那是正常的——本脚本只用于搬运刚做完的那一刻。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
