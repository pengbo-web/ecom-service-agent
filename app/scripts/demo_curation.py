"""Phase 5 记忆策展效果演示:同一批"脏"事实,朴素 add_facts vs LLM 策展 并排对比。

运行:  .venv\\Scripts\\python.exe -m app.scripts.demo_curation
需要 .env 里配好可用的 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME(策展要真实调一次 LLM)。
本脚本不写任何持久文件,只在内存里跑,安全可重复执行。
"""

from __future__ import annotations

from app.utils.console import enable_utf8_stdout
from app.config.settings import settings
from app.agent.memory.long_term import MemoryFact, LongTermMemory
from app.agent.memory.curation import curate_facts


def _build_client():
    if settings.resilience_enabled:
        from app.resilience.factory import make_resilient_client
        return make_resilient_client()
    from openai import OpenAI
    return OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)


def _fact(content: str, category: str = "preference", created_at: str = "2024-01-01T00:00:00") -> MemoryFact:
    return MemoryFact(content=content, category=category, created_at=created_at)


# 已存的老记忆(含近义重复、即将被纠正的会员等级、一次性琐事)
EXISTING = [
    _fact("用户喜欢红色", "preference", "2023-01-01T09:00:00"),
    _fact("用户偏好红色款式的商品", "preference", "2023-06-01T09:00:00"),
    _fact("用户是黄金会员", "identity", "2023-02-01T09:00:00"),
    _fact("用户咨询过一次物流延迟,已解决", "issue", "2023-03-01T09:00:00"),
    _fact("用户喜欢运动鞋", "preference", "2023-04-01T09:00:00"),
]
# 本次会话新提取到的事实(会员升级=对旧事实的纠正,红色系=又一条近义)
NEW = [
    _fact("用户已升级为钻石会员", "identity", "2024-07-21T10:00:00"),
    _fact("用户偏好红色系", "preference", "2024-07-21T10:00:00"),
]


def _print_facts(title: str, facts: list) -> None:
    print(f"\n{title}({len(facts)} 条):")
    for f in facts:
        print(f"  - [{f.category}] {f.content}   (created_at={f.created_at})")


def main() -> None:
    enable_utf8_stdout()

    _print_facts("【输入·已有长期记忆】", EXISTING)
    _print_facts("【输入·本次新提取】", NEW)

    # A) 朴素 add_facts:精确小写去重 + FIFO 截断(现状行为)
    naive = LongTermMemory(memory_dir="app/sessions/_demo_tmp", max_facts=50)
    naive.facts = list(EXISTING)
    naive.add_facts(NEW)
    _print_facts("【A · 朴素 add_facts】近义各留一条、会员等级新旧并存", naive.facts)

    # B) LLM 策展:合并近义 / 就地纠正 / 按重要性淘汰
    print("\n正在调用 LLM 策展(需要真实模型,请稍候)...")
    client = _build_client()
    curated = curate_facts(client, settings.model_name, EXISTING, NEW, max_facts=50)
    if curated is None:
        print("\n⚠️  策展调用失败(网络/密钥/返回非法)——线上会自动降级回 A 的朴素结果,记忆不受损。")
        return
    _print_facts("【B · LLM 策展】近义合并、会员纠正为钻石、琐事可被淘汰", curated)

    print("\n对比要点:")
    print("  · 3 条红色相关 → 策展后应合并成 1 条")
    print("  · 黄金会员(旧) 应被 钻石会员(新) 就地纠正,不再新旧并存")
    print("  · 一次性且已解决的物流琐事 可被淘汰")
    print("  · 保留下来的老事实,created_at 仍是原始时间(不重置年龄)")


if __name__ == "__main__":
    main()
