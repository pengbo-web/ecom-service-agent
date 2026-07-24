"""Langfuse 接入(可选体验层):OpenAI 客户端的 drop-in 替换。

`langfuse.openai.OpenAI` 与官方 OpenAI 客户端构造参数完全兼容,自动把每次
chat.completions.create 上报为 generation(prompt/补全/token/耗时),用于
体验成熟观测平台的 UI(localhost:3000)。

门控:`settings.langfuse_enabled` 默认 False——关闭或未安装 langfuse 包时,
返回原生 OpenAI 客户端,零行为差异(best-effort,导入失败静默回退)。
与自研 tracer 并存:TracingClient 在服务层包最外层,两边各记各的。
"""

from __future__ import annotations

import os

from openai import OpenAI

from app.config.settings import settings


def make_openai_client(**kwargs) -> OpenAI:
    """按门控返回 langfuse 包装客户端或原生客户端;构造参数原样透传。"""
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
