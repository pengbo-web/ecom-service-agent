from app.resilience.errors import classify_error, retry_after_seconds


class FakeErr(Exception):
    def __init__(self, msg="", status_code=None, retry_after=None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code
        if retry_after is not None:
            self.retry_after = retry_after


# 命名模拟 openai 异常类
class BadRequestError(FakeErr): pass
class RateLimitError(FakeErr): pass
class APITimeoutError(FakeErr): pass
class InternalServerError(FakeErr): pass


def test_fatal_status():
    for code in (400, 401, 403, 404, 422):
        assert classify_error(FakeErr(status_code=code)) == "fatal"


def test_429_rate_limit_is_transient():
    assert classify_error(FakeErr("Too Many Requests", status_code=429)) == "transient"


def test_429_quota_is_switch():
    assert classify_error(FakeErr("insufficient_quota: 余额不足", status_code=429)) == "switch"


def test_5xx_transient():
    for code in (500, 502, 503, 504):
        assert classify_error(FakeErr(status_code=code)) == "transient"


def test_by_class_name():
    assert classify_error(BadRequestError("bad")) == "fatal"
    assert classify_error(APITimeoutError("timed out")) == "transient"
    assert classify_error(InternalServerError("boom")) == "transient"
    assert classify_error(RateLimitError("rate limit")) == "transient"
    assert classify_error(RateLimitError("insufficient_quota")) == "switch"


def test_text_fallback():
    assert classify_error(Exception("model is currently overloaded")) == "switch"
    assert classify_error(Exception("content policy violation")) == "fatal"
    assert classify_error(Exception("connection reset")) == "transient"


def test_retry_after_parsing():
    assert retry_after_seconds(FakeErr(retry_after=5)) == 5.0
    assert retry_after_seconds(FakeErr("please wait, retry-after: 12")) == 12.0
    assert retry_after_seconds(FakeErr("nothing here")) is None
