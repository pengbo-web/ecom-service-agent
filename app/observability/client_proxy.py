"""透明代理：包装 OpenAI 客户端，为 LLM 调用埋 span 并采集 token。

不改核心 chat.py —— 服务层临时把 agent.client 换成本代理即可。
"""


def _record_usage(span, resp) -> None:
    usage = getattr(resp, "usage", None)
    if usage is not None:
        span.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        span.completion_tokens = getattr(usage, "completion_tokens", 0) or 0


class _TracedCompletions:
    def __init__(self, real, tracer, span_name):
        self._real = real
        self._tracer = tracer
        self._span_name = span_name

    def create(self, **kwargs):
        with self._tracer.span(self._span_name, "llm") as sp:
            resp = self._real.create(**kwargs)
            _record_usage(sp, resp)
            return resp

    def parse(self, **kwargs):
        with self._tracer.span(self._span_name, "llm") as sp:
            resp = self._real.parse(**kwargs)
            _record_usage(sp, resp)
            return resp

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TracedChat:
    def __init__(self, real, tracer, span_name):
        self._real = real
        self.completions = _TracedCompletions(real.completions, tracer, span_name)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TracedBeta:
    def __init__(self, real, tracer):
        self._real = real
        self.chat = _TracedChat(real.chat, tracer, "llm.beta.parse")

    def __getattr__(self, name):
        return getattr(self._real, name)


class TracingClient:
    def __init__(self, real_client, tracer):
        self._real = real_client
        self.chat = _TracedChat(real_client.chat, tracer, "llm.chat.create")
        if hasattr(real_client, "beta"):
            self.beta = _TracedBeta(real_client.beta, tracer)

    def __getattr__(self, name):
        return getattr(self._real, name)
