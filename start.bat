@echo off
chcp 65001 >nul
rem 双击本文件即可启动 Web 服务（自动使用项目 venv，无需命令行）
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv 虚拟环境，请先创建并安装依赖：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo 正在启动 并夕夕智能客服 服务...
echo 启动后用浏览器打开： http://127.0.0.1:8010/
echo 关闭本窗口即停止服务。
echo.

".venv\Scripts\python.exe" run_api.py

echo.
echo 服务已停止。
pause
