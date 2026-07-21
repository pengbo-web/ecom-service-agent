"""按 settings 构造 ResilientChatClient（主 + 可选备用）。"""

from openai import OpenAI

from app.config.settings import settings
from app.resilience.breaker import CircuitBreaker
from app.resilience.llm_client import ResilientChatClient


def make_resilient_client() -> ResilientChatClient:
    primary = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        max_retries=0,                 # SDK 重试关掉,由本层统一管
        timeout=settings.llm_timeout_s,
    )

    secondary = None
    if settings.fallback_model and settings.fallback_base_url:
        secondary = OpenAI(
            api_key=settings.fallback_api_key or settings.openai_api_key,
            base_url=settings.fallback_base_url,
            max_retries=0,
            timeout=settings.llm_timeout_s,
        )

    breaker = CircuitBreaker(
        threshold=settings.breaker_threshold,
        cooldown=settings.breaker_cooldown_s,
    )
    return ResilientChatClient(
        primary=primary,
        secondary=secondary,
        primary_model=settings.model_name,
        secondary_model=settings.fallback_model or settings.model_name,
        breaker=breaker,
        max_retries=settings.llm_max_retries,
        retry_after_cap=settings.llm_retry_after_cap_s,
    )
