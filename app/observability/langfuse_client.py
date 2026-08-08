"""Langfuse 接入(可选体验层):OpenAI 客户端的 drop-in 替换。

`langfuse.openai.OpenAI` 与官方 OpenAI 客户端构造参数完全兼容,自动把每次
chat.completions.create 上报为 generation(prompt/补全/token/耗时),用于
体验成熟观测平台的 UI(localhost:3000)。

门控:`settings.langfuse_enabled` 默认 False——关闭或未安装 langfuse 包时,
返回原生 OpenAI 客户端,零行为差异(best-effort,导入失败静默回退)。
与自研 tracer 并存:TracingClient 在服务层包最外层,两边各记各的。

W1 服务化 L2:除非调用方已显式传了 `http_client`,否则这里会按 `timeout`
参数从 `app.observability.http_pool` 取进程级共享的 `httpx.Client`——
真正慢的是每个新客户端各自建一次的 SSL 上下文(~0.86s/次)，共享传输层后
同一 timeout 配置全进程只建一次；`api_key`/`base_url`/`model` 等会话相关
配置不受影响，仍按调用时的最新 settings 构造(见 http_pool.py 顶部说明)。
"""

from __future__ import annotations

import os

from openai import OpenAI

from app.config.settings import settings


def _with_shared_transport(kwargs: dict) -> dict:
    """未显式指定 http_client 时,注入进程级共享传输层(见模块顶部说明)。"""
    if "http_client" in kwargs:
        return kwargs   # 调用方自己管传输层(如测试注入 mock),不覆盖
    from app.observability.http_pool import get_shared_http_client
    return {**kwargs, "http_client": get_shared_http_client(kwargs.get("timeout"))}


def make_openai_client(**kwargs) -> OpenAI:
    """按门控返回 langfuse 包装客户端或原生客户端;构造参数原样透传。"""
    kwargs = _with_shared_transport(kwargs)
    if not settings.langfuse_enabled:
        return OpenAI(**kwargs)
    try:
        # SDK 从环境变量读取凭据;从 settings 注入,便于统一在 .env 配置
        if settings.langfuse_public_key:
            os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
        if settings.langfuse_secret_key:
            os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
        from langfuse.openai import OpenAI as LangfuseOpenAI
        return LangfuseOpenAI(**kwargs)
    except Exception:
        # 未安装/配置错:回退原生客户端,绝不影响对话主流程
        return OpenAI(**kwargs)
