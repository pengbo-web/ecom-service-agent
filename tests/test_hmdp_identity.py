"""hmdp token → userId 解析(读共享 Redis Hash login:token:{token} 的 id)。"""

import fakeredis

from app.api.hmdp_identity import resolve_hmdp_user


def test_resolve_from_shared_redis():
    r = fakeredis.FakeStrictRedis()
    r.hset("login:token:tk-abc", mapping={"id": "5", "nickName": "小明", "icon": ""})
    assert resolve_hmdp_user("tk-abc", redis_client=r) == "5"


def test_resolve_missing_or_empty():
    r = fakeredis.FakeStrictRedis()
    assert resolve_hmdp_user("nope", redis_client=r) is None
    assert resolve_hmdp_user("", redis_client=r) is None
    assert resolve_hmdp_user(None, redis_client=r) is None
