"""本地启动 API 服务：python run_api.py"""

import uvicorn

from app.api.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
