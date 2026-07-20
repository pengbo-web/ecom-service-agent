"""本地启动 API 服务：python run_api.py

端口可通过 .env 的 API_PORT 配置（默认 8010，避开常被占用的 8000）。
"""

import sys

import uvicorn

from app.api.app import create_app
from app.config.settings import settings

# Windows 控制台默认 gbk 编码，Agent 输出的 emoji（💭🔧💾 等）会触发
# UnicodeEncodeError 导致请求崩溃；统一把标准流切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

app = create_app()

if __name__ == "__main__":
    print(f"服务地址: http://{settings.api_host}:{settings.api_port}/")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
