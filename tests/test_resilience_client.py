from app.resilience.llm_client import ResilientChatClient
from app.resilience.breaker import CircuitBreaker


class Err(Exception):
    def __init__(self, msg="", status_code=None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code


class ScriptedClient:
    """按脚本逐次返回/抛出;记录每次用的 model。chat.completions.create 与 beta.chat.completions.parse 共用脚本。"""
    def __init__(self, script):
        self.script = list(script)
        self.models = []
        outer = self

        class _Comp:
            def create(self, **kw):
                return outer._next(kw)
        class _Chat:
            completions = _Comp()
        class _BComp:
            def parse(self, **kw):
                return outer._next(kw)
        class _BChat:
            completions = _BComp()
        class _Beta:
            chat = _BChat()

        self.chat = _Chat()
        self.beta = _Beta()

    def _next(self, kw):
        self.models.append(kw.get("model"))
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _rc(primary, secondary=None, max_retries=2, breaker=None):
    return ResilientChatClient(
        primary=primary, secondary=secondary,
        primary_model="M1", secondary_model="M2",
        breaker=breaker or CircuitBreaker(threshold=3, cooldown=60, now=lambda: 0.0),
        max_retries=max_retries, sleep=lambda _s: None,
    )


def test_primary_success():
    p = ScriptedClient(["ok"])
    rc = _rc(p)
    assert rc.chat.completions.create(model="ignored") == "ok"
    assert p.models == ["M1"]           # model 被覆盖为主模型


def test_transient_then_success():
    p = ScriptedClient([Err("rate limit", status_code=429), "ok"])
    rc = _rc(p, max_retries=2)
    assert rc.chat.completions.create() == "ok"
    assert len(p.models) == 2           # 重试了一次


def test_transient_exhausted_switches_to_secondary():
    p = ScriptedClient([Err(status_code=429), Err(status_code=429)])
    s = ScriptedClient(["ok2"])
    rc = _rc(p, secondary=s, max_retries=1)
    assert rc.chat.completions.create() == "ok2"
    assert s.models == ["M2"]           # 备用用备用模型


def test_switch_error_skips_primary_retry():
    p = ScriptedClient([Err("insufficient_quota 余额不足", status_code=429)])
    s = ScriptedClient(["ok2"])
    rc = _rc(p, secondary=s, max_retries=3)
    assert rc.chat.completions.create() == "ok2"
    assert len(p.models) == 1           # 欠费不重试主,直接切


def test_fatal_raises_immediately():
    p = ScriptedClient([Err("bad", status_code=400)])
    s = ScriptedClient(["never"])
    rc = _rc(p, secondary=s)
    try:
        rc.chat.completions.create()
        assert False, "应抛出"
    except Err:
        pass
    assert s.models == []               # 致命错误不切备用


def test_breaker_open_goes_straight_to_secondary():
    p = ScriptedClient(["should-not-be-used"])
    s = ScriptedClient(["ok2"])
    b = CircuitBreaker(threshold=1, cooldown=999, now=lambda: 0.0)
    b.record_failure()                  # 跳闸
    rc = _rc(p, secondary=s, breaker=b)
    assert rc.chat.completions.create() == "ok2"
    assert p.models == []               # 主被熔断跳过


def test_no_secondary_raises_after_exhaust():
    p = ScriptedClient([Err(status_code=500), Err(status_code=500), Err(status_code=500)])
    rc = _rc(p, max_retries=2)
    try:
        rc.chat.completions.create()
        assert False
    except Err:
        pass
    assert len(p.models) == 3           # 无备用,重试耗尽后抛


def test_beta_parse_path_switches():
    p = ScriptedClient([Err(status_code=503)])
    s = ScriptedClient(["parsed"])
    rc = _rc(p, secondary=s, max_retries=0)
    assert rc.beta.chat.completions.parse() == "parsed"
