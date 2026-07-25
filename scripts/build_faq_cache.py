"""FAQ 缓存预热:解析 常见问题FAQ.md 的 Q/A 对,embedding 后写入缓存文件。

用法: .venv/Scripts/python.exe scripts/build_faq_cache.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.faq_cache import FaqCache
from app.config.settings import settings


def parse_faq(md_text: str) -> list[tuple[str, str]]:
    """### Qn：问题\n答案(至下一个 #) → (问题, 答案) 对。"""
    pairs = []
    blocks = re.split(r"^### Q\d+[：:]", md_text, flags=re.M)[1:]
    for b in blocks:
        lines = b.strip().splitlines()
        if not lines:
            continue
        q = lines[0].strip()
        body = []
        for ln in lines[1:]:
            if ln.startswith("#"):
                break
            body.append(ln)
        a = "\n".join(body).strip()
        if q and a:
            pairs.append((q, a))
    return pairs


def main() -> int:
    md = Path("app/agent/rag/knowledge/常见问题FAQ.md").read_text(encoding="utf-8")
    pairs = parse_faq(md)
    cache = FaqCache(settings.faq_cache_path)
    cache.entries = []                      # 重建:幂等
    for q, a in pairs:
        cache.add(q, a)
        print("cached:", q)
    print(f"total {len(pairs)} entries -> {settings.faq_cache_path}")
    return 0 if pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
