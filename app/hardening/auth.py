"""管理接口鉴权：X-Admin-Token 令牌校验（令牌为空时不鉴权）。"""

from fastapi import Header, HTTPException


def make_admin_auth(token: str):
    def _dep(x_admin_token: str = Header(default="")):
        if token and x_admin_token != token:
            raise HTTPException(status_code=401, detail="需要有效的管理令牌")
    return _dep
