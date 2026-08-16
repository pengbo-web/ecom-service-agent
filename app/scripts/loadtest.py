"""真实 HTTP 压测:看吞吐、延迟分位、错误率,以及并发下的正确性。

**为什么需要它**:此前所有并发测试都是**进程内十几个线程**的构造场景(直接调
`db.pay_order` / `apply_refund`)。那能验判定逻辑,验不到:真实 HTTP 栈、SQLite 写锁
在多请求下的表现、`SessionManager` 的按会话锁、Redis 断路器、以及幂等键在真并发下
到底只建一笔没有。

**同时压"性能"和"正确性"**:只看 QPS 的压测会漏掉最贵的问题——20 个并发下单
如果建出 20 笔订单,吞吐再高也是灾难。所以下单场景固定校验"数据库里真实多了几笔"。

    python -m app.scripts.loadtest --scenario read   --concurrency 20 --requests 200
    python -m app.scripts.loadtest --scenario order  --concurrency 20 --requests 20
    python -m app.scripts.loadtest --scenario chat   --concurrency 4  --requests 8
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from collections import Counter

import httpx

BASE = "http://127.0.0.1:8010"


def _percentile(values: list[float], pct: float) -> float:
    """第 pct 百分位(线性插值)。空列表返回 0。

    自己算而不是依赖 numpy:压测脚本不该为了一个分位数拉一个重依赖。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _login(user: str) -> dict:
    """拿一个 token;用户不存在就先建。返回 Authorization 头。"""
    from app.db import get_db

    if get_db().get_user(user) is None:
        get_db().create_user(user, f"压测 {user}")
    with httpx.Client(base_url=BASE, timeout=30) as c:
        tok = c.post("/api/auth/login", json={"user_id": user}).json()["token"]
    # **把压测流量标出来。** 不标的话它会以默认的 `live` 落进 skill_traces,
    # 而看门狗拿那张表算成功率、`rate < 0.6 → 自动回滚` —— 一轮压测就能把一份
    # 没问题的 skill 从线上换掉。实测那 41 条 `ab*`/`trk*` 失败正是这么来的。
    # 服务端只接受"往非真实方向标",所以这个头不可能被用来伪造真实性。
    return {"Authorization": f"Bearer {tok}", "X-Traffic-Source": "loadtest"}


def _order_count(user: str) -> int:
    from app.db import get_db

    conn = get_db().connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE user = ?", (user,)).fetchone()[0]
    finally:
        conn.close()


def run(scenario: str, concurrency: int, requests: int, user: str, timeout: float,
        distinct: bool = False) -> dict:
    """`distinct=True` 时每个线程用**独立买家 + 独立会话**。

    这个开关不是为了好看,是因为不加它测出来的数字**会误导**:`SessionManager`
    按 session 加锁(同一个买家不能两轮并行处理,那是对的),所以同会话压测测到的是
    **排队**而不是单轮延迟。实测同一会话并发 3 / 6 请求:p50 22943ms、总耗时 55.8s
    ——而 55.8/6 ≈ 9.3s 才是单轮真实耗时。把 23s 当成"聊天延迟"报出去是错的。
    """
    headers = _login(user)

    # chat 必须带 session_id(缺了是 422——第一版漏了,压出来一片 422)。
    session_id = ""
    if scenario == "chat" and not distinct:
        with httpx.Client(base_url=BASE, timeout=30) as c:
            session_id = c.post("/api/conversation/open", json={"user_id": user},
                                headers=headers).json()["conversation_id"]

    # 每线程一套身份(distinct 模式):模拟多个买家同时说话,压的是服务端真实并发能力
    per_thread: dict[int, tuple[dict, str]] = {}
    thread_seq = {"n": 0}
    latencies: list[float] = []
    codes: Counter = Counter()
    lock = threading.Lock()
    counter = {"left": requests}

    # 同一批请求共用一个幂等键:order 场景要验的正是"同键只建一笔"。
    shared_key = f"loadtest-{int(time.time())}"
    before = _order_count(user) if scenario == "order" else 0

    def identity() -> tuple[dict, str]:
        """本线程用的 (认证头, 会话ID)。distinct 模式下每个线程一套并缓存。"""
        if not distinct:
            return headers, session_id
        tid = threading.get_ident()
        if tid not in per_thread:
            with lock:
                thread_seq["n"] += 1
                idx = thread_seq["n"]
            u = f"{user}-{idx}"
            h = _login(u)
            sid = ""
            if scenario == "chat":
                with httpx.Client(base_url=BASE, timeout=30) as c:
                    sid = c.post("/api/conversation/open", json={"user_id": u},
                                 headers=h).json()["conversation_id"]
            per_thread[tid] = (h, sid)
        return per_thread[tid]

    def one(client: httpx.Client) -> tuple[int, float]:
        headers_, session_ = identity()
        t0 = time.perf_counter()
        if scenario == "read":
            r = client.get("/api/products", headers=headers_)
        elif scenario == "order":
            r = client.post("/api/order", json={"item_id": "1", "quantity": 1},
                            headers={**headers_, "Idempotency-Key": shared_key})
        elif scenario == "chat":
            r = client.post("/api/chat",
                            json={"message": "你们的退货政策是什么？", "user_id": user,
                                  "session_id": session_},
                            headers=headers_)
        else:
            raise SystemExit(f"未知场景: {scenario}")
        return r.status_code, (time.perf_counter() - t0) * 1000

    def worker():
        # 每个线程一个 client:共用一个 client 会把连接池变成瓶颈,压出来的是
        # 客户端的极限而不是服务端的。
        with httpx.Client(base_url=BASE, timeout=timeout) as client:
            while True:
                with lock:
                    if counter["left"] <= 0:
                        return
                    counter["left"] -= 1
                try:
                    code, ms = one(client)
                except Exception as exc:  # noqa: BLE001 客户端异常也是错误率的一部分
                    code, ms = f"exc:{type(exc).__name__}", 0.0
                with lock:
                    codes[code] += 1
                    if isinstance(code, int):
                        latencies.append(ms)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    result = {
        "scenario": scenario, "concurrency": concurrency, "requests": requests,
        "wall_s": round(wall, 2),
        "qps": round(requests / wall, 1) if wall else 0,
        "codes": dict(codes),
        "p50_ms": round(_percentile(latencies, 50)),
        "p95_ms": round(_percentile(latencies, 95)),
        "p99_ms": round(_percentile(latencies, 99)),
        "max_ms": round(max(latencies)) if latencies else 0,
    }
    if scenario == "order":
        # **正确性断言**:只看 QPS 的压测会漏掉最贵的问题。
        result["orders_created"] = _order_count(user) - before
        result["idempotent_ok"] = result["orders_created"] == 1
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", default="read", choices=["read", "order", "chat"])
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--user", default="loadtest")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--distinct", action="store_true",
                   help="每线程独立买家+独立会话(测真实多买家并发,不测同会话排队)")
    args = p.parse_args(argv)

    r = run(args.scenario, args.concurrency, args.requests, args.user, args.timeout,
            distinct=args.distinct)
    print(f"场景={r['scenario']} 并发={r['concurrency']} 请求={r['requests']}")
    print(f"  耗时 {r['wall_s']}s   QPS {r['qps']}")
    print(f"  延迟 p50 {r['p50_ms']}ms  p95 {r['p95_ms']}ms  p99 {r['p99_ms']}ms  max {r['max_ms']}ms")
    print(f"  状态码 {r['codes']}")
    if "orders_created" in r:
        mark = "✓" if r["idempotent_ok"] else "✗ 幂等失效"
        print(f"  实际新建订单 {r['orders_created']} 笔  {mark}")
    return 0 if r.get("idempotent_ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
