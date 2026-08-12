# 启动黑马点评(hmdp)后端。
#
# 为什么需要这个脚本:hmdp 挂掉时,从"页面上商品是空的"回溯到"hmdp 没在跑"要走一串
# 排查——而这一串里有两个坑,实测都踩过:
#
#   1. **端口是 8085,不是 8081。** ecom 的 `settings.hmdp_base_url` 指向 8085
#      (application.yaml 里 `server.port: 8085`)。而本机 8081 被一个 Docker 端口转发
#      占着、会返回一个 HTML 页面——照着 8081 探测会得出"hmdp 活着但返回 HTML"这种
#      完全错误的结论,白绕一大圈。
#   2. **依赖有三个**:MySQL(3306)、Redis(6379)、以及 8085 空闲。Redis 在 Docker 里,
#      Docker 引擎卡死时端口仍然监听但不应答(见 memory 里那条),此时 hmdp 会启动
#      失败或极慢。
#
# 所以这个脚本做的事就是:先把三个前置条件挨个查明白并**说清哪一条不满足**,再启动。
# 光"启动失败"这四个字对下一次排查没有任何帮助。

$ErrorActionPreference = "Stop"

$HmdpDir = "D:\BaiduNetdiskDownload\02-实战篇\代码\hm-dianping"
$Jar     = Join-Path $HmdpDir "target\hm-dianping-0.0.1-SNAPSHOT.jar"
$Port    = 8085
$LogFile = Join-Path $env:TEMP "hmdp.log"

function Test-Port([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $p); $c.Close(); return $true } catch { return $false }
}

Write-Host "=== 前置检查 ===" -ForegroundColor Cyan

# Java:jar 是 Java 8 编的,用更高版本未必能跑。
#
# **不要用 `& java -version 2>&1` 探测。** Windows PowerShell 5.1 对原生 exe 做 stderr
# 重定向时,会把每一行 stderr 包成 ErrorRecord(NativeCommandError)并把 $? 置为 $false
# ——即便进程退出码是 0。而 `java -version` 恰好把版本信息写在 stderr,于是配上
# $ErrorActionPreference="Stop" 就会误报"java 不可用"(实测踩过这一条)。
# Get-Command 只查 PATH、不执行进程,没有这个陷阱。
$java = Get-Command java -ErrorAction SilentlyContinue
if (-not $java) { Write-Host "  [X] java 不在 PATH 里 —— 装 JDK 8 或把它加进 PATH" -ForegroundColor Red; exit 1 }
Write-Host "  [OK] java 可用（$($java.Source)）"

if (-not (Test-Path $Jar)) {
    Write-Host "  [X] 找不到 jar: $Jar" -ForegroundColor Red
    Write-Host "       先在 $HmdpDir 里打包(mvn package),或确认路径是否变了" -ForegroundColor Yellow
    exit 1
}
Write-Host "  [OK] jar 存在"

# 源码比 jar 新 = 直接跑 jar 会跑到旧代码上,这种"改了没生效"极难查,所以要提醒
$newer = Get-ChildItem -Path (Join-Path $HmdpDir "src") -Recurse -Include *.java,*.yaml,*.xml -ErrorAction SilentlyContinue |
         Where-Object { $_.LastWriteTime -gt (Get-Item $Jar).LastWriteTime } | Select-Object -First 1
if ($newer) {
    Write-Host "  [!] 源码比 jar 新($($newer.Name))—— 直接跑 jar 会用到旧代码,需要重新打包" -ForegroundColor Yellow
}

if (-not (Test-Port 3306)) { Write-Host "  [X] MySQL(3306) 连不上 —— hmdp 起不来" -ForegroundColor Red; exit 1 }
Write-Host "  [OK] MySQL(3306) 可连"

if (-not (Test-Port 6379)) { Write-Host "  [X] Redis(6379) 连不上 —— hmdp 起不来" -ForegroundColor Red; exit 1 }
# 端口通 ≠ 会应答:Docker 引擎卡死时端口仍监听但永不回包(实测踩过)。
# 这里不做 PING(PowerShell 里没有现成的 redis 客户端),只提示这个可能。
Write-Host "  [OK] Redis(6379) 端口可连（注意：端口通不等于会应答，Docker 引擎卡死时会这样）"

if (Test-Port $Port) {
    Write-Host "  [!] $Port 已被占用 —— hmdp 可能已经在跑了" -ForegroundColor Yellow
    Write-Host "       验证：curl http://127.0.0.1:$Port/product/1" -ForegroundColor Yellow
    exit 0
}
Write-Host "  [OK] $Port 空闲"

Write-Host ""
Write-Host "=== 启动 ===" -ForegroundColor Cyan
Write-Host "  日志: $LogFile"
Start-Process -FilePath "java" -ArgumentList "-jar", "`"$Jar`"" `
    -WorkingDirectory $HmdpDir -RedirectStandardOutput $LogFile `
    -RedirectStandardError "$LogFile.err" -WindowStyle Hidden

# 实测冷启动约 24 秒(Spring Boot + Redisson 连接池初始化),给到 90 秒
for ($i = 1; $i -le 90; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Port $Port) {
        Write-Host "  [OK] hmdp 就绪（${i}s）—— http://127.0.0.1:$Port" -ForegroundColor Green
        exit 0
    }
}

Write-Host "  [X] 90 秒内没起来。日志尾部:" -ForegroundColor Red
if (Test-Path $LogFile) { Get-Content $LogFile -Tail 25 }
if (Test-Path "$LogFile.err") { Get-Content "$LogFile.err" -Tail 15 }
exit 1
