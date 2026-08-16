"""`/api/seller/stream` 的 SSE 帧形状 —— 这个端点从引入起就是坏的。

单独成文件而不是并进 `test_seller_orchestrator.py`:那个文件此刻有并行会话
未提交的改动(shared_context 后端切换),`git add` 整文件会把别人的在改代码
一起扫进我的提交 —— 本轮已经这样误伤过一次 `app/db/database.py`。
"""
def test_seller_stream_yields_encodable_sse_text_not_dicts():
    """**症状极具迷惑性,前端表现是"永远转圈"。**

    `run_seller_streaming` 与买家侧 `run_agent_streaming` 一样 yield **dict**
    (事件协议在那一层定义),由端点负责序列化成 SSE 文本。买家侧写的是
    `yield _sse_frame(event)`,而 `/api/seller/stream` 漏了这一步,把 dict 直接
    交给了 `StreamingResponse`。

    后果:200 + text/event-stream 的响应头**先发出去了**,然后 starlette 在
    `chunk.encode()` 上抛 `AttributeError: 'dict' object has no attribute
    'encode'`,连接被掐断且**一个数据帧都没有**。前端拿到的是"请求成功但永远
    没有内容",于是一直停在「分析中…」——既没有报错也没有超时。

    这个端点从引入起就是坏的,只是此前没有前端调它。
    """
    import json

    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.config.settings import settings

    client = TestClient(create_app())
    headers = {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}
    with client.stream("POST", "/api/seller/stream",
                       json={"session_id": "sse-shape-probe", "message": "你好"},
                       headers=headers) as r:
        assert r.status_code == 200
        frames = [ln for ln in r.iter_lines() if ln.strip()]

    assert frames, "一个数据帧都没有 —— 前端会永远停在 loading"
    for ln in frames:
        assert ln.startswith("data: "), f"不是 SSE 帧格式: {ln[:60]!r}"
        json.loads(ln[len("data: "):])          # 必须是可解析的 JSON
    assert any('"type": "done"' in ln or '"type":"done"' in ln for ln in frames), \
        "没有 done 帧,前端不知道该收尾"
