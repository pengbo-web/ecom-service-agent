"""LLM 调用错误分类（借鉴 nanobot providers/base.py 的做法，防依赖具体 SDK 异常类）。

分三类:
- "fatal"     : 重试/切换都没用（400/401/403/404/422、内容策略、参数非法）→ 直接抛。
- "transient" : 同模型重试有望恢复（429 速率限制、5xx、超时、连接错误）→ 退避重试,耗尽再切。
- "switch"    : 同模型重试无望但换模型有望（欠费/配额耗尽、模型过载/不存在）→ 直接切备用。

判定优先级:显式 status_code → 异常类名 → 错误文本正则。全部离线,不 import openai。
"""

import re

_FATAL_STATUS = {400, 401, 403, 404, 422}
_TRANSIENT_STATUS = {408, 500, 502, 503, 504}

_FATAL_NAME = ("badrequest", "authentication", "permissiondenied", "notfound",
               "unprocessableentity", "invalidrequest")
_TRANSIENT_NAME = ("timeout", "connection", "internalserver", "apiconnection")

# 429 需再细分:欠费/配额 → switch;纯速率限制 → transient
_QUOTA_MARKERS = ("insufficient_quota", "exceeded your current quota", "billing",
                  "arrears", "欠费", "余额不足", "配额")
_SWITCH_MARKERS = ("overloaded", "model_not_found", "does not exist",
                   "model is currently overloaded", "过载", "模型不存在")
_FATAL_MARKERS = ("content_filter", "content policy", "invalid_request_error",
                  "内容", "违规")


def _status_of(exc: Exception):
    return getattr(exc, "status_code", None) or getattr(exc, "status", None)


def classify_error(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    status = _status_of(exc)

    # 1) 显式状态码
    if status in _FATAL_STATUS:
        return "fatal"
    if status == 429:
        if any(m in text for m in _QUOTA_MARKERS):
            return "switch"
        return "transient"          # 未标明的 429 默认当速率限制,重试
    if status in _TRANSIENT_STATUS:
        return "transient"

    # 2) 异常类名
    if any(k in name for k in _FATAL_NAME):
        return "fatal"
    if "ratelimit" in name:
        return "switch" if any(m in text for m in _QUOTA_MARKERS) else "transient"
    if any(k in name for k in _TRANSIENT_NAME):
        return "transient"

    # 3) 文本正则兜底
    if any(m in text for m in _FATAL_MARKERS):
        return "fatal"
    if any(m in text for m in _QUOTA_MARKERS) or any(m in text for m in _SWITCH_MARKERS):
        return "switch"
    if "timeout" in text or "timed out" in text or "connection" in text:
        return "transient"

    # 默认:当作可切换(不重试主、直接试备用),比直接抛更稳
    return "switch"


_RETRY_AFTER_HEADER = re.compile(r"retry[-_ ]after(?:-ms)?['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)",
                                 re.IGNORECASE)


def retry_after_seconds(exc: Exception) -> float | None:
    """从异常的响应头/结构化字段/文本里解析 Retry-After（秒）。取不到返回 None。"""
    val = getattr(exc, "retry_after", None)
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    text = str(exc)
    m = _RETRY_AFTER_HEADER.search(text)
    if m:
        v = float(m.group(1))
        # 含 -ms 的按毫秒
        if "ms" in text[max(0, m.start() - 20):m.end()].lower():
            v /= 1000.0
        return v
    return None
