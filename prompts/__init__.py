"""提示词加载器：启动时一次性读取 prompts/ 下所有 .md 文件到内存。

使用方式：
    from prompts import get
    SYSTEM_PROMPT = get("customer_service/system_prompt")

键是 .md 文件相对于 prompts/ 的路径（去掉 .md 后缀），用 POSIX 正斜杠分隔。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent
_cache: dict[str, str] = {}


def _load_all() -> None:
    """扫描 prompts/ 下所有 .md 文件，以相对路径（无后缀）为键存入缓存。"""
    for md_file in sorted(_PROMPTS_DIR.rglob("*.md")):
        key = md_file.relative_to(_PROMPTS_DIR).with_suffix("").as_posix()
        try:
            _cache[key] = md_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("提示词文件读取失败，跳过: %s (%s)", md_file, exc)


_load_all()


def get(name: str) -> str:
    """获取提示词文本。

    Args:
        name: 路径键，如 ``"customer_service/system_prompt"``

    Returns:
        文件内容（已 strip）

    Raises:
        KeyError: 文件不存在时（启动时应该已发现所有文件，运行期缺失是部署错误）
    """
    try:
        return _cache[name]
    except KeyError:
        raise KeyError(
            f"提示词 '{name}' 未找到。已有的键: {sorted(_cache.keys())}"
        ) from None


def get_json(name: str):
    """获取 JSON 格式的提示词，解析为 Python 对象（dict / list）。

    用于结构化数据，如 buyer_hints 的提示词映射表。
    """
    raw = get(name)
    return json.loads(raw)


def get_or_empty(name: str) -> str:
    """安全版本：不存在时返回空串，不抛异常。"""
    return _cache.get(name, "")


def reload() -> None:
    """热重载所有提示词文件（开发调试用）。"""
    _cache.clear()
    _load_all()
    logger.info("提示词已重载，共 %d 个文件", len(_cache))


def available() -> list[str]:
    """列出所有已加载的提示词键（排序）。"""
    return sorted(_cache.keys())
