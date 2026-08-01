"""把 demo hmdp 身份写入 Redis(login:token:{demo_token} = {id,nickName,icon})。

一键启动脚本调用:令 DEMO_HMDP_TOKEN 成为 hmdp 后端认可的有效登录会话,
使前端零登录即可以该身份(小鱼同学,已有订单)聊真实 hmdp 数据。
输出用 ASCII,避免 Windows 控制台(gbk)重定向乱码。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)  # 让 pydantic 从项目根读 .env

from app.config.settings import settings  # noqa: E402
from app.api.hmdp_identity import seed_demo_hmdp_identity  # noqa: E402

ok = seed_demo_hmdp_identity(
    settings.demo_hmdp_token, settings.demo_hmdp_user_id, settings.demo_hmdp_nickname
)
status = "OK" if ok else "FAILED (Redis down?)"
print(f"demo identity seeded: {status} user_id={settings.demo_hmdp_user_id} "
      f"token={settings.demo_hmdp_token}")
