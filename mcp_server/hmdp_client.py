"""hmdp REST 的最小 httpx 封装。token 走 `authorization` 头(hmdp 约定:裸 token,无 Bearer)。"""

from __future__ import annotations

import httpx


class HmdpClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8085", timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, token):
        return {"authorization": token} if token else {}

    def get_json(self, path: str, params: dict | None = None, token: str | None = None) -> dict:
        r = httpx.get(self.base_url + path, params=params,
                      headers=self._headers(token), timeout=self.timeout)
        return r.json()

    def post_json(self, path: str, body: dict | None = None, token: str | None = None) -> dict:
        r = httpx.post(self.base_url + path, json=body or {},
                       headers=self._headers(token), timeout=self.timeout)
        return r.json()

    def put_json(self, path: str, body: dict | None = None, token: str | None = None) -> dict:
        r = httpx.put(self.base_url + path, json=body or {},
                      headers=self._headers(token), timeout=self.timeout)
        return r.json()
