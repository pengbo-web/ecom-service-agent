"""长期记忆：跨会话持久化用户知识。

将用户在多次会话中表现出的偏好、身份、行为模式等提取并持久化为 JSON，
在新会话启动时加载并注入 prompt，让 Agent 具备"记住老客户"的能力。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import OpenAI

from app.agent.memory.extraction import extract_long_term_facts
from app.agent.memory.fts_store import MemoryFtsStore
from app.config.settings import settings


@dataclass
class MemoryFact:
    """一条长期记忆事实。"""
    content: str
    category: str  # identity / preference / behavior / issue / other
    created_at: str
    source_session: str = ""


class LongTermMemory:
    """跨会话长期记忆：持久化用户知识。"""

    def __init__(
        self,
        user_id: str = "default",
        memory_dir: str = "app/sessions/memory",
        max_facts: int = 50,
        curate_enabled: bool = False,
    ):
        self.user_id = user_id
        self.memory_dir = Path(memory_dir)
        self.max_facts = max_facts
        self.curate_enabled = curate_enabled
        self.facts: list[MemoryFact] = []
        self.interaction_summaries: list[dict] = []
        self._fts: MemoryFtsStore | None = None

    @property
    def memory_path(self) -> Path:
        return self.memory_dir / f"{self.user_id}.json"

    def _ensure_fts(self) -> MemoryFtsStore | None:
        """按 settings.memory_fts_enabled 门控懒建 FTS 索引存储。

        关闭时恒返回 None,不建库、不落任何多余文件,行为与改造前完全一致。
        """
        if not settings.memory_fts_enabled:
            return None
        if self._fts is None:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self._fts = MemoryFtsStore(str(self.memory_dir / "memory_fts.db"))
        return self._fts

    def load(self) -> None:
        """从 JSON 文件加载用户的长期记忆。"""
        if not self.memory_path.exists():
            return
        try:
            with self.memory_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        for item in data.get("facts", []):
            self.facts.append(MemoryFact(
                content=item["content"],
                category=item.get("category", "other"),
                created_at=item.get("created_at", ""),
                source_session=item.get("source_session", ""),
            ))
        self.interaction_summaries = data.get("interaction_summaries", [])

    def save(self) -> None:
        """持久化到 JSON 文件（原子写入）。"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "version": 1,
            "user_id": self.user_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "facts": [asdict(f) for f in self.facts],
            "interaction_summaries": self.interaction_summaries,
        }

        tmp_path = self.memory_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.memory_path)

        # 全量重同步 FTS 索引:facts 列表可能被 curate 整体替换,增量同步易漂移,
        # n<=max_facts(<=50)时全量重建成本可忽略。
        fts = self._ensure_fts()
        if fts is not None:
            fts.clear(self.user_id)
            for fact in self.facts:
                fact_id = hashlib.md5(fact.content.encode("utf-8")).hexdigest()[:12]
                fts.index(self.user_id, fact_id, fact.content)

    def add_facts(self, new_facts: list[MemoryFact]) -> None:
        """添加新事实，自动去重并裁剪到 max_facts。"""
        existing_contents = {f.content.lower() for f in self.facts}
        for fact in new_facts:
            if fact.content.lower() not in existing_contents:
                self.facts.append(fact)
                existing_contents.add(fact.content.lower())

        if len(self.facts) > self.max_facts:
            self.facts = self.facts[-self.max_facts:]

    def add_interaction_summary(self, summary: str) -> None:
        self.interaction_summaries.append({
            "summary": summary,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })

    def extract_and_save(
        self,
        client: OpenAI,
        model: str,
        messages: list[dict],
        summary: Optional[str],
    ) -> None:
        """从会话消息中提取长期记忆事实并保存。"""
        if not messages and not summary:
            return

        new_facts, interaction_summary = extract_long_term_facts(
            client, model, messages, summary, self.facts,
        )

        if new_facts:
            self._merge_facts(client, model, new_facts)
        if interaction_summary:
            self.add_interaction_summary(interaction_summary)

        self.save()

    def _merge_facts(self, client: OpenAI, model: str, new_facts: list[MemoryFact]) -> None:
        """并入新事实:启用策展则 LLM 合并/纠正/淘汰,失败降级回 add_facts。"""
        if self.curate_enabled:
            from app.agent.memory.curation import curate_facts
            curated = curate_facts(client, model, self.facts, new_facts, self.max_facts)
            if curated is not None:
                self.facts = curated
                return
        self.add_facts(new_facts)

    def recall(self, query: str, top_k: int = 5) -> list[str]:
        """按 query 通过 FTS 索引召回相关事实 content 列表。

        FTS 不可用(门控关闭/未建库)或 query 为空时返回 []。
        """
        if not query:
            return []
        fts = self._ensure_fts()
        if fts is None:
            return []
        return fts.search(self.user_id, query, top_k)

    def build_prompt_section(self, query: str | None = None) -> str | None:
        """生成注入 system prompt 的长期记忆片段。

        query 为 None、FTS 不可用或无命中时,facts 部分保持原有全量单段格式，
        与改造前完全一致。有命中时，facts 部分拆成"与当前问题相关的记忆"
        （命中事实，按召回顺序）与"其他历史记忆"（其余事实，原顺序，
        不与命中段重复）两段。
        """
        if not self.facts and not self.interaction_summaries:
            return None

        parts = []
        if self.facts:
            hit_contents = self.recall(query, top_k=5) if query else []

            hit_facts: list[MemoryFact] = []
            if hit_contents:
                by_content: dict[str, MemoryFact] = {}
                for f in self.facts:
                    by_content.setdefault(f.content, f)
                seen: set[str] = set()
                for content in hit_contents:
                    f = by_content.get(content)
                    if f is not None and f.content not in seen:
                        hit_facts.append(f)
                        seen.add(f.content)

            if hit_facts:
                hit_set = {f.content for f in hit_facts}
                other_facts = [f for f in self.facts if f.content not in hit_set]

                relevant_text = "\n".join(
                    f"- [{f.category}] {f.content}" for f in hit_facts
                )
                parts.append(f"与当前问题相关的记忆：\n{relevant_text}")

                if other_facts:
                    other_text = "\n".join(
                        f"- [{f.category}] {f.content}" for f in other_facts
                    )
                    parts.append(f"其他历史记忆：\n{other_text}")
            else:
                facts_text = "\n".join(
                    f"- [{f.category}] {f.content}" for f in self.facts
                )
                parts.append(f"该用户的历史记忆（来自过往会话）：\n{facts_text}")

        if self.interaction_summaries:
            recent = self.interaction_summaries[-3:]
            summaries_text = "\n".join(f"- {s['summary']}" for s in recent)
            parts.append(f"最近的交互记录：\n{summaries_text}")

        return "\n\n".join(parts)

    def reset(self) -> None:
        """清空该用户的长期记忆（文件也删除，FTS 索引也清空）。"""
        self.facts = []
        self.interaction_summaries = []
        if self.memory_path.exists():
            self.memory_path.unlink()
        fts = self._ensure_fts()
        if fts is not None:
            fts.clear(self.user_id)
