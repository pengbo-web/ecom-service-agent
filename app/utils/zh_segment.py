"""中文分词公共 util:jieba 预分词的唯一实现(WS2,技术方案 §3)。

memory_store(记忆 FTS)与 archive_fts(归档会话 FTS)共用这一个实现——两处各写
一份分词,_token 口径漂移会让"同一个词在两个索引里查不到同一种结果",而这类
漂移不报错、只表现为召回 quietly 变差。

只放纯函数:不含 IO、不含开关判断(门控由各调用方自己持有)。
"""

from __future__ import annotations

# 分词口径版本戳:换分词器/换切法时必须 +1,索引侧据此整体重建。
SEGMENTER_VERSION = "jieba-cut_for_search-1"


def segment(text: str) -> str:
    """jieba 搜索模式分词,空格连接,供 FTS5 MATCH 使用。

    为什么必须预分词:FTS5 默认 unicode61 把连续中文整段视为一个 token,实测
    content 含「跑鞋」时 MATCH '跑鞋' 恒 0 命中(见 memory_store docstring)。
    """
    import jieba
    tokens = [t for t in jieba.cut_for_search(text) if t.strip()]
    return " ".join(tokens)


def segment_tokens(text: str) -> list[str]:
    """同 `segment` 但返回 token 列表(查询侧拼装 MATCH 表达式用)。"""
    import jieba
    return [t for t in jieba.cut_for_search(text) if t.strip()]
