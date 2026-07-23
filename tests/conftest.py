"""测试全局夹具:强制会话存储用 file 后端,套件不依赖外部 .env(如本机 .env 设了 redis)。

需要 redis 后端的测试自行 set_session_store(RedisSessionStore(fakeredis...)) 覆盖即可。
"""

import pytest

from app.session.store import FileSessionStore, set_session_store


@pytest.fixture(autouse=True)
def _force_file_session_store():
    set_session_store(FileSessionStore())
    yield
    set_session_store(None)
