"""测试全局夹具:强制会话存储用 file 后端,套件不依赖外部 .env(如本机 .env 设了 redis)。

需要 redis 后端的测试自行 set_session_store(RedisSessionStore(fakeredis...)) 覆盖即可。
"""

import pytest

from app.session.store import FileSessionStore, set_session_store
from app.session.lock import LocalSessionLock, set_session_lock
from app.session.idempotency import NullIdempotencyStore, set_idempotency_store


@pytest.fixture(autouse=True)
def _force_local_session_backends():
    set_session_store(FileSessionStore())        # 存储用 file
    set_session_lock(LocalSessionLock())         # 锁用进程内(不依赖 redis/env)
    set_idempotency_store(NullIdempotencyStore())  # 幂等默认关(需 redis 的测试自行注入)
    yield
    set_session_store(None)
    set_session_lock(None)
    set_idempotency_store(None)
