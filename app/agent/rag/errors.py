"""RAG/向量子系统的专用异常类型。

`EmbeddingIndexMismatchError` 专用于"持久化索引/缓存记录的 embedding 模型或
向量维度,与当前配置不一致"这一类**结构性配置错误**——它不是网络抖动、
超时这类瞬时故障,而是"部署换了模型却没重建索引/缓存"的确定性错误,换模型
前不重建,每次加载都会复现,绝不会自己恢复。

正因如此,这一类错误必须在加载处(索引/缓存首次构造时)明确地往外抛,
不能被上层"embedding 失败就当未命中/无知识库"的常规 fail-soft 兜底悄悄吞掉——
那类兜底是为瞬时故障设计的(这一次失败,下一次可能就好了);而维度/模型
不匹配一旦发生，会一直失败到操作员重建索引为止，悄悄降级只会让整条检索
链路长期"看起来正常、其实每次都在算错误的相似度"，这正是本任务要根除的
那类 bug。调用方在 `except Exception` 兜底时应显式排除这个类型（先
`except EmbeddingIndexMismatchError: raise` 再兜底其它异常），让它继续往外抛。
"""


class EmbeddingIndexMismatchError(RuntimeError):
    """持久化索引/缓存记录的 embedding 模型或向量维度与当前配置不一致。

    触发时机:索引/缓存加载（load）阶段，比对持久化时记下的
    embedding_model / embedding_dim 与当前 settings.embedding_model /
    settings.embedding_dimension。
    """
