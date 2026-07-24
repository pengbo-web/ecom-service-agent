"""H3 Skill 离线合成入口（半自动）：从 session_archive 冷归档聚类样本，
LLM 归纳候选 SKILL.md。

半自动铁律：本脚本只把候选写到 app/agent/skills/definitions/_candidates/，
绝不触碰 definitions/ 正式目录、绝不自动生效——必须人工审核候选内容后手动
移入 app/agent/skills/definitions/，SkillManager 才会加载它。

用法：
  python -m app.scripts.synthesize_skills [N]

N：读取最近 N 条归档会话样本（默认 50）。
需先在 .env 中开启 skill_synth_enabled=True（默认关闭，离线工具不静默做事）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.agent.skills.synthesizer import synthesize_skills  # noqa: E402

CANDIDATES_DIR = "app/agent/skills/definitions/_candidates"


def main() -> None:
    if not settings.skill_synth_enabled:
        print(
            "skill_synth_enabled=False，已跳过（离线工具默认关闭，不静默做事）。\n"
            "如需运行，请在 .env 中设置 skill_synth_enabled=True 后重试。"
        )
        return

    limit = 50
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(f"忽略非法参数: {sys.argv[1]}，使用默认 limit=50")

    samples = get_db().list_recent_archives(limit)
    if not samples:
        print("没有可用的归档会话样本，退出。")
        return

    print(f"读取到 {len(samples)} 条归档会话样本，开始聚类合成候选 skill...")

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    out_paths = synthesize_skills(
        client, settings.model_name, samples, out_dir=CANDIDATES_DIR,
    )

    if not out_paths:
        print("未产出候选 skill（样本不足两条同类，或 LLM 输出未通过校验）。")
        return

    print(f"共产出 {len(out_paths)} 个候选 skill：")
    for p in out_paths:
        print(f"  - {p}")
    print("\n注意：候选文件不会被 SkillManager 自动加载。")
    print("请人工审核以上候选内容后，再手动移入 app/agent/skills/definitions/ 使其生效。")


if __name__ == "__main__":
    main()
