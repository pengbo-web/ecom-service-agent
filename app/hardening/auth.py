"""管理接口鉴权：X-Admin-Token 令牌校验（令牌为空时不鉴权）。"""

import hmac

from fastapi import Header, HTTPException


def make_admin_auth(token: str):
    def _dep(x_admin_token: str = Header(default="")):
        # 常量时间比较:防 token 计时侧信道(逐字符早退会泄漏前缀匹配长度)
        if token and not hmac.compare_digest(x_admin_token or "", token):
            raise HTTPException(status_code=401, detail="需要有效的管理令牌")
    return _dep
