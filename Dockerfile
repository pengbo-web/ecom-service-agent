FROM python:3.11-slim

WORKDIR /app

# 依赖分层缓存：先装依赖，再拷代码
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY run_api.py ./
COPY mcp_server/ ./mcp_server/

# 非 root 运行
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8010

CMD ["python", "run_api.py"]
