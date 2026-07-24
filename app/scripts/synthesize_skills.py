"""H3 Skill 离线合成入口（半自动）：从 session_archive 冷归档聚类样本，
LLM 归纳候选 SKILL.md。

半自动铁律：本脚本只把候选写到 app/agent/skills/definitions/_candidates/，
绝不触碰 definitions/ 正式目录、绝不自动生效——必须人工审核候选内容后手动
移入 app/agent/skills/definitions/，SkillManager 才会加载它。

用法：
  python -m app.scripts.synthesize_skills [N]

N：读取最近 N 条归档会话样本（默认 50）。
需先在 .env 中开启 skill_synth_enabled=True（默认关闭，离线工具不静默做事）。

H3.5 追加：把入口从"只做聚类创建"扩为完整离线闭环（三步，均离线、半自动）：
  ① 用户建模：按 user_id 分组归档样本，逐用户调 user_modeling.model_user
     归纳行为偏好标签，直接写入结构化档案（H3.4 已定语义：低风险追加操作，
     不需要人工确认候选）。
  ② 聚类创建：沿用现有 synthesize_skills（不变）。
  ③ 失败自改进：对 definitions/ 正式库里每个已入库 skill，挑出与其相关的
     失败会话样本（朴素规则判定 + 关键词关联），跑 synthesizer.improve_skill
     产出改进候选（同样只产候选，绝不覆盖正式目录）。
各步 try/except 隔离：单步异常只打印该步失败、不影响其他两步（fail-soft）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.agent.skills.loader import SkillManager  # noqa: E402
from app.agent.skills.synthesizer import (  # noqa: E402
    INTENT_KEYWORDS,
    improve_skill,
    synthesize_skills,
)
from app.agent.skills.user_modeling import model_user  # noqa: E402

CANDIDATES_DIR = "app/agent/skills/definitions/_candidates"
DEFINITIONS_DIR = "app/agent/skills/definitions"

# ---------- ③ 失败自改进：失败样本判定 / skill 关联（朴素规则，离线启发式） ----------

# assistant 消息命中即判负：显式转人工的托词
HANDOFF_KEYWORDS = ["转人工", "人工客服"]
# 同一条 assistant 消息里"抱歉"重复出现 >=2 次，视为反复道歉/搞不定
APOLOGY_KEYWORD = "抱歉"
APOLOGY_MIN_COUNT = 2
# summary 命中即判负：人工总结时已标注投诉/不满
SUMMARY_FAILURE_KEYWORDS = ["投诉", "不满"]

# 意图关键词平铺（复用 synthesizer.INTENT_KEYWORDS，用于 skill<->失败样本关联）
_ALL_INTENT_KEYWORDS = [kw for _label, kws in INTENT_KEYWORDS for kw in kws]


def is_failure_sample(archived: dict) -> bool:
    """朴素规则判定一条归档会话是否为"失败会话"（离线启发式，不追求精确，
    人工最终把关——本函数只用于粗筛送去改进的候选案例）。

    命中任一即判负：
    - 任一 assistant 消息 content 含"转人工"/"人工客服"；
    - 任一 assistant 消息 content 里"抱歉"出现 >=2 次；
    - summary 含"投诉"/"不满"。
    """
    for msg in archived.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content") or "")
        if any(kw in content for kw in HANDOFF_KEYWORDS):
            return True
        if content.count(APOLOGY_KEYWORD) >= APOLOGY_MIN_COUNT:
            return True

    summary = str(archived.get("summary") or "")
    if any(kw in summary for kw in SUMMARY_FAILURE_KEYWORDS):
        return True

    return False


def _first_user_message(messages: list[dict]) -> str:
    for msg in messages or []:
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def related_failures(skill_name: str, skill_desc: str, failures: list[dict]) -> list[dict]:
    """失败样本与某个 skill 是否相关：失败样本首条 user 消息与该 skill 的
    description 有任一共同的意图关键词（复用 synthesizer.INTENT_KEYWORDS
    这张表）即视为相关，朴素规则，不引入分类模型。

    `skill_name` 暂未参与判定逻辑，仅为接口完整性保留（便于调用方/未来按名
    过滤或记录日志）。
    """
    del skill_name  # 当前判定只依据 description + 关键词表，不使用 name

    common_keywords = [kw for kw in _ALL_INTENT_KEYWORDS if kw in skill_desc]
    if not common_keywords:
        return []

    related: list[dict] = []
    for failure in failures:
        user_msg = _first_user_message(failure.get("messages") or [])
        if any(kw in user_msg for kw in common_keywords):
            related.append(failure)
    return related


# ---------- 三步闭环：可注入依赖，供测试直接调用（不跑 main/真 LLM） ----------


def run_user_modeling(client, model: str, archives: list[dict], store=None) -> dict[str, list[str]]:
    """步骤①：按 user_id 分组归档样本，逐用户建模，标签直写档案。

    单用户样本 <2 条跳过（样本太少不建模，不调 LLM）。返回 {user_id: tags}
    （只含实际建模过的用户）。
    """
    groups: dict[str, list[dict]] = {}
    for item in archives:
        user_id = item.get("user_id")
        if not user_id:
            continue
        groups.setdefault(user_id, []).append(item)

    result: dict[str, list[str]] = {}
    for user_id, samples in groups.items():
        if len(samples) < 2:
            continue
        result[user_id] = model_user(client, model, user_id, samples, store=store)
    return result


def _read_skill_content(skills_dir: str, name: str) -> str | None:
    path = Path(skills_dir) / name / "SKILL.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def run_improvements(
    client, model: str, archives: list[dict], skills_dir: str, out_dir: str,
) -> list[Path]:
    """步骤③：对正式库里每个已入库 skill，挑相关失败样本跑改进，产出候选。

    - 归档里没有失败样本 → 不建 SkillManager、不调 LLM，直接返回 []。
    - 有失败样本但与某 skill 无关联 → 该 skill 跳过（不调 LLM）。
    - 改进候选写 out_dir/<name>/SKILL.md（synthesizer.improve_skill 内部
      已做 name 一致性兜底、坏输出 fail-soft）。
    """
    failures = [a for a in archives if is_failure_sample(a)]
    if not failures:
        return []

    manager = SkillManager(skills_dir=skills_dir, enabled=True)
    out_paths: list[Path] = []
    for entry in manager.get_catalog():
        name = entry["name"]
        description = entry["description"]

        cases = related_failures(name, description, failures)
        if not cases:
            continue

        content = _read_skill_content(skills_dir, name)
        if content is None:
            continue

        result = improve_skill(
            client, model, {"name": name, "content": content}, cases, out_dir,
        )
        if result is not None:
            out_paths.append(result)

    return out_paths


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

    print(f"读取到 {len(samples)} 条归档会话样本，开始三步离线闭环（建模/创建/自改进）...")

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    model = settings.model_name

    # 各步 try/except 隔离：离线工具 fail-soft，单步异常不拖垮其他两步。
    user_tags: dict[str, list[str]] = {}
    try:
        user_tags = run_user_modeling(client, model, samples)
    except Exception as exc:
        print(f"① 用户建模失败，跳过本步: {exc}")

    candidate_paths: list[Path] = []
    try:
        candidate_paths = synthesize_skills(client, model, samples, out_dir=CANDIDATES_DIR)
    except Exception as exc:
        print(f"② 聚类创建失败，跳过本步: {exc}")

    improve_paths: list[Path] = []
    try:
        improve_paths = run_improvements(
            client, model, samples, skills_dir=DEFINITIONS_DIR, out_dir=CANDIDATES_DIR,
        )
    except Exception as exc:
        print(f"③ 失败自改进失败，跳过本步: {exc}")

    print("\n===== 离线闭环摘要 =====")
    print(f"用户建模: {len(user_tags)} 个用户，标签 {user_tags}")
    print(f"新候选 skill: {[str(p) for p in candidate_paths]}")
    print(f"改进候选: {[str(p) for p in improve_paths]}")
    print("\n注意：以上候选/标签中，候选文件不会被 SkillManager 自动加载。")
    print("请人工审核候选内容后，再手动移入 app/agent/skills/definitions/ 使其生效。")


if __name__ == "__main__":
    main()
