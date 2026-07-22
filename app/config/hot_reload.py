"""配置签名式热更新:重读 .env/环境变量,比对影响行为的字段,原地更新 settings 单例。

不 watch 文件、不轮询——由显式触发(POST /api/config/reload)驱动:
线上调限流阈值 / 成本预算 / 转人工置信度 / 切主备模型,改完 .env 调一次即生效,免重启进程。
只更新白名单里的"影响行为"字段,避免把 memory_dir 等运行期不应变的字段一起改乱。
"""

from app.config.settings import Settings, settings

# 支持热更、且影响运行时行为的字段白名单
HOT_FIELDS = (
    "rate_limit_per_min",
    "daily_request_budget",
    "hitl_confidence_threshold",
    "model_name",
    "fallback_model",
    "fallback_base_url",
)


def config_signature(s=None) -> dict:
    """当前热更字段的快照,用于比对是否变化。"""
    s = s or settings
    return {f: getattr(s, f, None) for f in HOT_FIELDS}


def reload_settings(fresh=None) -> list[str]:
    """重新读取配置,原地更新 settings 单例中的热更字段,返回发生变化的字段名列表。

    fresh 可注入(便于测试);默认重新实例化 Settings() 从 .env/环境读取。
    """
    fresh = fresh if fresh is not None else Settings()
    changed = []
    for f in HOT_FIELDS:
        new = getattr(fresh, f, None)
        if getattr(settings, f, None) != new:
            setattr(settings, f, new)
            changed.append(f)
    return changed
