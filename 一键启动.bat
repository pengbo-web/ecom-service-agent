@echo off
chcp 65001 >nul
rem One-click start: agent + real hmdp data + ApeRAG, then open browser.
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1"
echo.
echo Services keep running in background. To stop: scripts\stop_all.ps1
pause >nul
