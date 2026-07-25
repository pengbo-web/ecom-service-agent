"""极简登录态 token(HMAC-SHA256,纯标准库)。

格式:base64url(user_id|exp_unix) + "." + hmac_hexdigest。
演示边界:无密码——"谁是谁"由登录选择,但签发后身份由服务端签名背书,
请求体自报的 user_id 不再被信任。生产替换:login 换平台 JWT 签发,
verify 换 JWKS 验签,调用方接口不变。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional


def _sig(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign_token(user_id: str, secret: str, ttl_s: int, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl_s)
    raw = f"{user_id}|{exp}".encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload}.{_sig(payload.encode('ascii'), secret)}"


def verify_token(token: str, secret: str, now: float | None = None) -> Optional[str]:
    """校验并解出 user_id;过期/篡改/格式坏一律返回 None,绝不抛。"""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sig(payload.encode("ascii"), secret)):
            return None
        pad = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + pad).decode("utf-8")
        user_id, exp_s = raw.rsplit("|", 1)
        if (now if now is not None else time.time()) >= int(exp_s):
            return None
        return user_id or None
    except Exception:
        return None
