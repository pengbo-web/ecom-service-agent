"""生产环境安全前置校验:production + 默认/空密钥 → 拒绝启动;dev 不校验。"""

import pytest

from app.config.settings import Settings, verify_production_secrets, DEFAULT_AUTH_SECRET


def _s(**kw):
    # demo_mode 必须显式钉成 False:`Settings` 在构造时会读 `.env`,而本机 .env
    # 里是 DEMO_MODE=true —— 不钉的话这些用例会因为**开发机的运行配置**而失败,
    # 而失败原因与被测行为毫无关系(与 tests/conftest.py 顶部那段说明同一条纪律)。
    base = dict(environment="production", auth_enabled=True, demo_mode=False,
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


def test_prod_auth_off_is_now_rejected():
    """**语义已收紧**:生产环境不再允许 `AUTH_ENABLED=false`。

    原用例断言的是"auth 关掉时不要求 AUTH_SECRET"——那半句逻辑本身没错(密钥
    用不上就别强求),但它隐含允许了"生产环境把鉴权整个关掉",而那意味着回退到
    **自报 user_id**:任何人只要在请求里写上别人的 id 就能读写其订单与长期记忆。

    密钥弱是"**可能**被攻破",鉴权关掉是**默认就对所有人开放**——两者不是同一
    量级,不该共用"配置自由"这个理由。所以现在直接拒绝启动。

    (`AUTH_SECRET` 那条判据仍然只在 auth_enabled 时生效,原意图保留。)
    """
    with pytest.raises(RuntimeError, match="AUTH_ENABLED"):
        verify_production_secrets(_s(auth_enabled=False, auth_secret=DEFAULT_AUTH_SECRET))
