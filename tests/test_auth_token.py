"""HMAC token:签发/校验/过期/篡改。纯函数,全离线。"""

from app.auth.token import sign_token, verify_token

SECRET = "test-secret"


def test_sign_and_verify_roundtrip():
    t = sign_token("小明", SECRET, ttl_s=60, now=1000.0)
    assert verify_token(t, SECRET, now=1030.0) == "小明"


def test_expired_returns_none():
    t = sign_token("u1", SECRET, ttl_s=60, now=1000.0)
    assert verify_token(t, SECRET, now=1061.0) is None


def test_tampered_returns_none():
    t = sign_token("u1", SECRET, ttl_s=60, now=1000.0)
    payload, sig = t.rsplit(".", 1)
    assert verify_token(payload + "." + ("0" * len(sig)), SECRET, now=1010.0) is None
    assert verify_token(t, "wrong-secret", now=1010.0) is None


def test_garbage_inputs_return_none_not_raise():
    for bad in ("", "not-a-token", "a.b", "a.b.c", "。。", None if False else "x." ):
        assert verify_token(bad, SECRET, now=1010.0) is None


def test_user_id_with_separator_chars_survives():
    """user_id 含 '|' 之外的任意合法字符(中文/下划线/连字符)往返无损。"""
    for uid in ("小明", "user_a-1", "ABC123"):
        t = sign_token(uid, SECRET, ttl_s=60, now=1000.0)
        assert verify_token(t, SECRET, now=1001.0) == uid
