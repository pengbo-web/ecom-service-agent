"""hmdp REST 的最小 httpx 封装。token 走 `authorization` 头(hmdp 约定:裸 token,无 Bearer)。"""

from __future__ import annotations

import httpx


class HmdpClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8085", timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, token):
        return {"authorization": token} if token else {}

    @staticmethod
    def _safe(resp) -> dict:
        """把 httpx 响应安全解析成 dict:401(空 body)/5xx(HTML)/非 JSON 一律降级成
        hmdp 风格的 {success:false,errorMsg}(而非抛异常),让上层工具优雅返回错误。"""
        if resp.status_code == 401:
            return {"success": False, "errorMsg": "未登录或登录已过期"}
        try:
            data = resp.json()
        except (ValueError, TypeError):
            return {"success": False, "errorMsg": f"hmdp 返回异常({resp.status_code})"}
        return data if isinstance(data, dict) else {"success": False, "errorMsg": "hmdp 返回格式异常"}

    def _call(self, method: str, path: str, **kw) -> dict:
        # 内网地址绕过系统代理。这是 AI **全部** hmdp 工具(查订单/物流/退款/
        # 下单)的唯一出口:开发机上挂着 HTTP_PROXY 而 NO_PROXY 没带回环时,
        # 这一层会整片超时,而每个工具的降级文案都是"无法连接 hmdp",
        # Agent 拿到的是一句错误、买家拿到的是一段临场编出来的解释。
        # 见 app/net/internal_http.py。
        from app.net.internal_http import internal_client, warn_if_proxy_would_break

        url = self.base_url + path
        try:
            with internal_client(url, timeout=self.timeout) as c:
                resp = c.request(method, url, **kw)
        except httpx.HTTPError as e:
            warn_if_proxy_would_break(url)
            return {"success": False, "errorMsg": f"无法连接 hmdp:{e.__class__.__name__}"}
        return self._safe(resp)

    def get_json(self, path: str, params: dict | None = None, token: str | None = None) -> dict:
        return self._call("GET", path, params=params, headers=self._headers(token))

    def post_json(self, path: str, body: dict | None = None, token: str | None = None) -> dict:
        return self._call("POST", path, json=body or {}, headers=self._headers(token))

    def put_json(self, path: str, body: dict | None = None, token: str | None = None) -> dict:
        return self._call("PUT", path, json=body or {}, headers=self._headers(token))
