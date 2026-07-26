"""生产环境安全前置校验:production + 默认/空密钥 → 拒绝启动;dev 不校验。"""

import pytest

from app.config.settings import Settings, verify_production_secrets, DEFAULT_AUTH_SECRET


def _s(**kw):
    base = dict(environment="production", auth_enabled=True,
                auth_secret="a-strong-random-secret", admin_token="admin-strong")
    base.update(kw)
    return Settings(**base)


def test_dev_never_blocks():
    verify_production_secrets(_s(environment="dev", auth_secret=DEFAULT_AUTH_SECRET,
                                 admin_token=""))   # 不抛


def test_prod_default_secret_rejected():
    with pytest.raises(RuntimeError):
        verify_production_secrets(_s(auth_secret=DEFAULT_AUTH_SECRET))


def test_prod_empty_admin_token_rejected():
    with pytest.raises(RuntimeError):
        verify_production_secrets(_s(admin_token=""))


def test_prod_with_strong_secrets_ok():
    verify_production_secrets(_s())   # 不抛


def test_prod_auth_off_skips_secret_check():
    # auth 关闭时不要求 AUTH_SECRET,但仍要求 admin_token(管理端点)
    verify_production_secrets(_s(auth_enabled=False, auth_secret=DEFAULT_AUTH_SECRET))
