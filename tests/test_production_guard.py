"""生产环境启动前置校验。

机制早就有(`verify_production_secrets`),但它此前**只查密钥**,漏了两条更危险的:
`DEMO_MODE=true` 与 `AUTH_ENABLED=false`。

危险程度的差别在于:密钥弱是"**可能**被攻破",而这两条是**默认就对所有人开放**,
不需要任何攻击动作 —— 打开页面就是别人的账号。
"""

from __future__ import annotations

import pytest

from app.config.settings import DEFAULT_AUTH_SECRET, Settings, verify_production_secrets

_OK = dict(auth_enabled=True, auth_secret="k" * 40, admin_token="t" * 40,
           demo_mode=False)


def _prod(**over) -> Settings:
    return Settings(environment="production", **{**_OK, **over})


def test_clean_production_config_passes():
    verify_production_secrets(_prod())      # 不该抛


def test_demo_mode_blocks_startup():
    """DEMO_MODE=true 在生产是**无鉴权**,不是鉴权弱。

    前端会自动以 demo_user_id 登录并跳过登录卡片 —— 任何访问者直接以那个真实买家
    身份进入:看他的订单与收货地址、下单、申请退款。
    """
    with pytest.raises(RuntimeError) as e:
        verify_production_secrets(_prod(demo_mode=True))
    assert "DEMO_MODE" in str(e.value)
    # 报错必须说清后果,不能只说"配置不合规"——运维要能立刻判断严重性
    assert "订单" in str(e.value) or "身份进入" in str(e.value)


def test_auth_disabled_blocks_startup():
    """AUTH_ENABLED=false 回退到自报 user_id:写上别人的 id 就能读写其订单与长期记忆。"""
    with pytest.raises(RuntimeError) as e:
        verify_production_secrets(_prod(auth_enabled=False, auth_secret=""))
    assert "AUTH_ENABLED" in str(e.value)


@pytest.mark.parametrize("over,needle", [
    (dict(auth_secret=DEFAULT_AUTH_SECRET), "AUTH_SECRET"),
    (dict(auth_secret=""), "AUTH_SECRET"),
    (dict(admin_token=""), "ADMIN_TOKEN"),
])
def test_secret_problems_still_blocked(over, needle):
    """既有的三条不能因为新增两条而失效。"""
    with pytest.raises(RuntimeError) as e:
        verify_production_secrets(_prod(**over))
    assert needle in str(e.value)


def test_all_problems_are_reported_at_once():
    """一次列全,不是修一个报一个。

    运维改配置是一次性动作;逐条报错会让他重启四次才知道全部问题。
    """
    with pytest.raises(RuntimeError) as e:
        verify_production_secrets(Settings(
            environment="production", demo_mode=True, auth_enabled=False,
            auth_secret="", admin_token=""))
    msg = str(e.value)
    assert "DEMO_MODE" in msg and "AUTH_ENABLED" in msg and "ADMIN_TOKEN" in msg


def test_dev_is_not_checked():
    """dev(默认)不校验 —— 保持本机/教学零配置可跑。

    这条是刻意的:把生产纪律强加到开发环境上,只会让人把 ENVIRONMENT 永久设成 dev,
    那样生产校验就永远不会触发。
    """
    verify_production_secrets(Settings(environment="dev", demo_mode=True,
                                       auth_enabled=False, auth_secret="",
                                       admin_token=""))


@pytest.mark.parametrize("env", ["production", "prod", "PRODUCTION", " Prod "])
def test_production_aliases(env):
    with pytest.raises(RuntimeError):
        verify_production_secrets(Settings(environment=env, demo_mode=True, **{
            k: v for k, v in _OK.items() if k != "demo_mode"}))


def test_guard_is_actually_wired_into_startup():
    """校验函数存在但没人调 = 没有校验。

    本项目已经吃过多次"能力写好了但零调用方"的亏(routing.describe、
    budget_status、start_followup),所以这条钉的是**接线**。
    """
    import inspect

    from app.api import app as app_module

    assert "verify_production_secrets" in inspect.getsource(app_module)
