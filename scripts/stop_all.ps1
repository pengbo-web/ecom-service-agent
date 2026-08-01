<#
  停止一键启动拉起的服务(hmdp Java、hmdp-mcp、agent)。
  默认保留 Redis / MySQL(共享基础设施)。加 -All 连 ApeRAG 容器一起停。
#>
param([switch]$All)
chcp 65001 > $null
$ROOT    = Split-Path -Parent $PSScriptRoot
$RUN_DIR = Join-Path $ROOT ".run"
$APERAG_DIR = "D:\2026项目\ApeRAG"

function Stop-ByPid($name){
  $pf = Join-Path $RUN_DIR "$name.pid"
  if(Test-Path $pf){
    $procId = (Get-Content $pf | Select-Object -First 1).Trim()
    if($procId){
      try{ Stop-Process -Id ([int]$procId) -Force -ErrorAction Stop; Write-Host "  已停止 $name (PID $procId)" -ForegroundColor Green }
      catch{ Write-Host "  $name (PID $procId) 已不在运行" -ForegroundColor DarkGray }
    }
    Remove-Item $pf -ErrorAction SilentlyContinue
  } else { Write-Host "  $name 无 PID 记录,跳过" -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "停止 并夕夕智能客服 服务 ..." -ForegroundColor Cyan
Stop-ByPid "agent"
Stop-ByPid "hmdp-mcp"
Stop-ByPid "hmdp"

if($All){
  if(Test-Path $APERAG_DIR){
    Write-Host "  停止 ApeRAG 容器 ..." -ForegroundColor Gray
    Push-Location $APERAG_DIR; docker compose stop *> $null; Pop-Location
  }
  Write-Host "  (Redis / MySQL 为共享服务,未停止)" -ForegroundColor DarkGray
} else {
  Write-Host "  (Redis / MySQL / ApeRAG 保留;如需全停加 -All)" -ForegroundColor DarkGray
}
Write-Host "完成。" -ForegroundColor Green
Write-Host ""
