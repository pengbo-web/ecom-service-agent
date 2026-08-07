"""向量化封装：调用 OpenAI Embeddings 接口。

- 支持批量编码（list[str] → list[list[float]]）。
- 可配置 model 与 base_url（与 chat 模型共用一套 OpenAI 客户端配置）。
- 返回原始 list[float]，由调用方决定如何持久化（这里用 json，不引入 numpy 依赖）。

阶段一 gap⑤:客户端改用 `make_openai_client` drop-in 包装——门控关/未装时
返回的仍是原生 `OpenAI(**kwargs)`,与改动前逐字节一致(热路径 FAQ 缓存/
知识库召回、离线建索引脚本共用这一个类,零行为差异);门控开时 embedding
调用会被 langfuse.openai 自动上报为 "embedding" 类型的观察,嵌进调用方
当前所处的 span/trace(如 background_trace 或某个 stage)下。
"""

from typing import Iterable

from app.observability.langfuse_client import make_openai_client


class Embedder:
    """OpenAI Embeddings 同步封装。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "text-embedding-3-small",
        batch_size: int = 10,  # DashScope text-embedding-v3 单批上限为 10
        timeout: float | None = None,       # None=SDK 默认(离线建索引可容忍慢);热路径应显式传短超时
        max_retries: int | None = None,     # None=SDK 默认(2);热路径应传 0——重试累积曾致单次 906s
    ):
        client_kwargs: dict = {"api_key": api_key, "base_url": base_url}
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        self._client = make_openai_client(**client_kwargs)
        self._model = model
        self._batch_size = batch_size

    @property
    def model(self) -> str:
        return self._model

    def encode(self, texts: Iterable[str]) -> list[list[float]]:
        """批量编码，自动按 batch_size 分批请求。"""
        texts = list(texts)
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            resp = self._client.embeddings.create(model=self._model, input=batch)
            out.extend(item.embedding for item in resp.data)
        return out

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]
