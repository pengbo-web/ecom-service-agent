<#
  一键启动:并夕夕智能客服(接真实黑马点评数据 + ApeRAG 知识库)
  ------------------------------------------------------------------
  按依赖顺序拉起全部服务,健康探测通过后自动打开浏览器,开箱即可体验:
    Redis(6379) → MySQL(3306) → hmdp Java(8085) → hmdp-mcp(9123)
    → ApeRAG(8100,docker) → 播 demo 身份 → agent+SPA(8010) → 开浏览器
  demo 模式:自动以预置 hmdp 身份(小鱼同学,已有订单)进入,零登录零验证码。
  再次运行安全:已在跑的服务自动跳过(幂等)。停止:scripts\stop_all.ps1
#>
chcp 65001 > $null
$OutputEncoding = [System.Text.Encoding]::UTF8

# ---------- 路径配置(如目录不同,改这里) ----------
$ROOT      = Split-Path -Parent $PSScriptRoot            # 项目根
$VENV_PY   = Join-Path $ROOT ".venv\Scripts\python.exe"
$HMDP_DIR  = "D:\BaiduNetdiskDownload\02-实战篇\代码\hm-dianping"
$APERAG_DIR= "D:\2026项目\ApeRAG"
$JAVA_HOME = "C:\Program Files\Amazon Corretto\jdk1.8.0_502"
$MVN       = "C:\Users\pb166\tools\apache-maven-3.9.16\bin\mvn.cmd"
$RUN_DIR   = Join-Path $ROOT ".run"
New-Item -ItemType Directory -Force $RUN_DIR | Out-Null

function Log($m,$c="Gray"){ Write-Host ("  " + $m) -ForegroundColor $c }
function Step($m){ Write-Host ""; Write-Host ("▶ " + $m) -ForegroundColor Cyan }
function PortUp($p){ [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) }
function WaitPort($p,$name,$timeout=90){
  $t=0; while($t -lt $timeout){ if(PortUp $p){ Log "$name (:$p) 就绪 ✓" Green; return $true }; Start-Sleep 2; $t+=2 }
  Log "$name (:$p) 等待超时(${timeout}s)" Yellow; return $false
}
# 后台启动一个常驻进程,日志落 .run\<name>.log,PID 记入 .run\<name>.pid
function StartSvc($name,$file,$svcArgs,$workdir,$envs){
  $log = Join-Path $RUN_DIR ($name + ".log")
  if($envs){ foreach($k in $envs.Keys){ Set-Item ("Env:" + $k) $envs[$k] } }
  $p = Start-Process -FilePath $file -ArgumentList $svcArgs -WorkingDirectory $workdir `
        -RedirectStandardOutput $log -RedirectStandardError ($log + ".err") `
        -WindowStyle Hidden -PassThru
  $p.Id | Out-File (Join-Path $RUN_DIR ($name + ".pid")) -Encoding ascii
  Log ("$name 已拉起 (PID " + $p.Id + "),日志 " + $log)
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "   并夕夕智能客服 · 一键启动(真实 hmdp 数据 + ApeRAG)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# ---------- 0) 前置检查 ----------
Step "前置检查"
if(-not (Test-Path $VENV_PY)){ Log "未找到 venv:$VENV_PY —— 先建: python -m venv .venv 并 pip install -r requirements.txt" Red; exit 1 }
if(-not (Test-Path (Join-Path $ROOT "web\dist\index.html"))){ Log "web\dist 未构建 —— 先: cd webui; npm install; npm run build" Red; exit 1 }
Log "venv、web\dist 就绪 ✓" Green
$dockerOk = $false; try{ docker info *> $null; $dockerOk = ($LASTEXITCODE -eq 0) }catch{}
if(-not $dockerOk){ Log "Docker 未运行(Redis/ApeRAG 需要它)。请启动 Docker Desktop 后重试。" Yellow }

# ---------- 1) Redis ----------
Step "Redis (:6379)"
if(PortUp 6379){ Log "Redis 已在运行,跳过 ✓" Green }
elseif($dockerOk){
  docker start ecom-redis *> $null
  if($LASTEXITCODE -ne 0){ docker run -d --name ecom-redis -p 6379:6379 redis:7-alpine *> $null }
  WaitPort 6379 "Redis" 30 | Out-Null
} else { Log "Redis 未运行且 Docker 不可用,后续会话存储/身份反查会失败" Red }

# ---------- 2) MySQL(校验,不自启第三方服务) ----------
Step "MySQL (:3306, 库 heimadp)"
if(PortUp 3306){ Log "MySQL 已在运行 ✓" Green }
else {
  try{ Start-Service -Name "MySQL80","mysql" -ErrorAction SilentlyContinue }catch{}
  if(-not (WaitPort 3306 "MySQL" 20)){ Log "MySQL 未起 —— hmdp 后端将无法连库,请手动启动 MySQL 服务" Red }
}

# ---------- 3) hmdp Java 后端 (:8085) ----------
Step "hmdp Java 后端 (:8085)"
if(PortUp 8085){ Log "hmdp 已在运行,跳过 ✓" Green }
else {
  $jar = Get-ChildItem "$HMDP_DIR\target" -Filter "*.jar" -ErrorAction SilentlyContinue |
         Where-Object { $_.Name -notmatch 'sources|javadoc|original' } | Select-Object -First 1
  $env:JAVA_HOME = $JAVA_HOME
  if($jar){
    Log ("用预编译 jar 启动:" + $jar.Name)
    StartSvc "hmdp" ($JAVA_HOME + "\bin\java.exe") @("-jar", $jar.FullName) $HMDP_DIR $null
  } elseif(Test-Path $MVN){
    Log "未找到 jar,回退 mvn spring-boot:run(首次较慢)"
    StartSvc "hmdp" $MVN @("-f", ($HMDP_DIR + "\pom.xml"), "spring-boot:run", "-DskipTests") $HMDP_DIR @{ JAVA_HOME=$JAVA_HOME }
  } else { Log "既无 jar 也无 Maven,无法启动 hmdp" Red }
  WaitPort 8085 "hmdp 后端" 120 | Out-Null
}

# ---------- 4) hmdp-mcp (:9123) ----------
Step "hmdp-mcp 工具服务 (:9123)"
if(PortUp 9123){ Log "9123 已占用(确保是 hmdp_server 而非 mock server.py)✓" Yellow }
else {
  StartSvc "hmdp-mcp" $VENV_PY @("-m", "mcp_server.hmdp_server") $ROOT $null
  WaitPort 9123 "hmdp-mcp" 40 | Out-Null
}

# ---------- 5) ApeRAG(docker,后台起,不阻塞:agent 有本地降级) ----------
Step "ApeRAG 知识库 (:8100, docker)"
if(PortUp 8100){ Log "ApeRAG 已在运行 ✓" Green }
elseif($dockerOk -and (Test-Path $APERAG_DIR)){
  # 后台拉起,不阻塞主流程(ApeRAG 8 容器冷启较慢;未就绪时 agent 自动降级本地索引)
  $ap = Join-Path $RUN_DIR "aperag.log"
  Start-Process -FilePath "docker" -ArgumentList @("compose","up","-d") -WorkingDirectory $APERAG_DIR `
    -RedirectStandardOutput $ap -RedirectStandardError ($ap + ".err") -WindowStyle Hidden | Out-Null
  Log "ApeRAG 容器后台拉起中(冷启约 1-2 分钟,不阻塞);未就绪时 agent 降级本地索引,不影响体验" Green
} else { Log "跳过 ApeRAG(Docker 不可用或目录缺失);agent 走本地知识索引兜底" Yellow }

# ---------- 6) 播 demo hmdp 身份到 Redis(令 demo token 成为 hmdp 有效会话) ----------
Step "播种 demo 身份"
if(Test-Path $VENV_PY){
  & $VENV_PY (Join-Path $PSScriptRoot "seed_demo.py") 2>&1 | ForEach-Object { Log $_ }
}

# ---------- 7) agent + SPA (:8010) ----------
Step "agent + 智能客服前端 (:8010)"
if(PortUp 8010){ Log "agent 已在运行,跳过 ✓" Green }
else {
  StartSvc "agent" $VENV_PY @("run_api.py") $ROOT $null
  WaitPort 8010 "agent" 60 | Out-Null
}

# ---------- 8) 打开浏览器 ----------
Step "完成"
if(PortUp 8010){
  Start-Process "http://127.0.0.1:8010/"
  Write-Host ""
  Write-Host "  ✓ 体验入口:http://127.0.0.1:8010/  (已自动打开)" -ForegroundColor Green
  Write-Host "    demo 模式:自动以「小鱼同学」身份进入,可直接问:" -ForegroundColor Gray
  Write-Host "      · 我的订单有哪些   · 查一下我最近的订单物流   · 帮我看看能不能退款 / 砍砍价" -ForegroundColor Gray
  Write-Host ""
  Write-Host "  停止全部服务:powershell -File scripts\stop_all.ps1" -ForegroundColor DarkGray
} else {
  Write-Host ("  agent 未就绪,查看日志:" + $RUN_DIR + "\agent.log / agent.log.err") -ForegroundColor Red
}
Write-Host ""
