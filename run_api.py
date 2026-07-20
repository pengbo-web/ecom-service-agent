"""本地启动 API 服务：python run_api.py

端口可通过 .env 的 API_PORT 配置（默认 8010，避开常被占用的 8000）。
"""

import uvicorn

from app.api.app import create_app
from app.config.settings import settings
from app.utils.console import enable_utf8_stdout

enable_utf8_stdout()  # Windows gbk 控制台下避免 emoji 打印崩溃

app = create_app()

if __name__ == "__main__":
    print(f"服务地址: http://{settings.api_host}:{settings.api_port}/")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
