<#
.SYNOPSIS
  Starts the AEGIS Operations Console stack on Windows:
  PostgreSQL verification, FastAPI backend (uvicorn), Next.js console, and
  a live traffic replay.

.DESCRIPTION
  Native PowerShell equivalent of scripts/dev-up.sh. Idempotent and safe to re-run.
  Logs and PID files live in .dev\ at the repository root.

.PARAMETER NoReplay
  Start backend and frontend, but leave replay idle.

.PARAMETER Restart
  Stop-then-restart backend and frontend processes.

.PARAMETER Dataset
  Replay dataset name (default: "friday-morning").

.PARAMETER Speed
  Replay speed multiplier (default: 20.0).

.EXAMPLE
  .\scripts\dev-up.ps1
  .\scripts\dev-up.ps1 -NoReplay
  .\scripts\dev-up.ps1 -Restart
  .\scripts\dev-up.ps1 -Dataset "wednesday" -Speed 10.0
#>

param(
    [switch]$NoReplay,
    [switch]$Restart,
    [string]$Dataset = "friday-morning",
    [double]$Speed = 20.0,
    [switch]$Help
)

if ($Help) {
    Write-Host "Usage: .\scripts\dev-up.ps1 [-NoReplay] [-Restart] [-Dataset <name>] [-Speed <multiplier>]"
    exit 0
}

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

$DevDir = Join-Path $RepoRoot ".dev"
if (-not (Test-Path $DevDir)) {
    New-Item -ItemType Directory -Path $DevDir -Force | Out-Null
}

$BackendPidFile = Join-Path $DevDir "backend.pid"
$FrontendPidFile = Join-Path $DevDir "frontend.pid"
$BackendLog = Join-Path $DevDir "backend.log"
$BackendErrLog = Join-Path $DevDir "backend.err.log"
$FrontendLog = Join-Path $DevDir "frontend.log"
$FrontendErrLog = Join-Path $DevDir "frontend.err.log"

$BackendPort = 8000
$FrontendPort = 3000
$BackendUrl = "http://127.0.0.1:$BackendPort"
$FrontendUrl = "http://127.0.0.1:$FrontendPort"

function Write-Ok($msg)   { Write-Host "  [+] $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  [.] $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [x] $msg" -ForegroundColor Red }
function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }

function Test-PidAlive($pidFile) {
    if (-not (Test-Path $pidFile)) { return $false }
    $raw = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return $false }
    $p = $raw.ToString().Trim()
    if ($p -match '^\d+$') {
        $pidNum = [int]$p
        $proc = Get-Process -Id $pidNum -ErrorAction SilentlyContinue
        if ($null -ne $proc) {
            return $true
        }
    }
    return $false
}

function Wait-For-Http($url, $timeoutSeconds, $name) {
    $waited = 0
    while ($waited -lt $timeoutSeconds) {
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) {
                return $true
            }
        } catch {
            # retry
        }
        Start-Sleep -Seconds 1
        $waited++
    }
    Write-Fail "$name did not respond at $url within ${timeoutSeconds}s"
    return $false
}

# ---------------------------------------------------------------------------
# 0. Prerequisite check & auto-installation
# ---------------------------------------------------------------------------
Write-Step "Checking prerequisites"

$PythonBin = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $PythonBin)) {
    Write-Info "No Python venv found at $PythonBin. Creating virtual environment..."
    $sysPython = (Get-Command python.exe, py.exe, python3.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    if (-not $sysPython) {
        Write-Fail "Python executable not found on PATH. Please install Python 3.11+ first."
        exit 1
    }
    Write-Info "Running: $sysPython -m venv venv"
    & $sysPython -m venv (Join-Path $RepoRoot "venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PythonBin)) {
        Write-Fail "Failed to create Python virtual environment in venv\"
        exit 1
    }
    Write-Ok "Created Python virtual environment ($PythonBin)"

    Write-Info "Installing Python dependencies (requirements.txt & requirements-backend.txt)..."
    $PipBin = Join-Path $RepoRoot "venv\Scripts\pip.exe"
    $Req1 = Join-Path $RepoRoot "requirements.txt"
    $Req2 = Join-Path $RepoRoot "requirements-backend.txt"
    & $PipBin install -r $Req1 -r $Req2
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install Python dependencies"
        exit 1
    }
    Write-Ok "Python dependencies installed successfully"
} else {
    Write-Ok "Python venv present ($PythonBin)"
}

$NodeModules = Join-Path $RepoRoot "frontend\node_modules"
if (-not (Test-Path $NodeModules)) {
    Write-Info "frontend\node_modules missing. Installing frontend dependencies (npm install)..."
    $sysNpm = (Get-Command npm.cmd, npm.ps1, npm -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    if (-not $sysNpm) {
        Write-Fail "Node.js / npm not found on PATH. Please install Node.js 20+ first."
        exit 1
    }
    $FrontendDir = Join-Path $RepoRoot "frontend"
    Push-Location $FrontendDir
    & $sysNpm install
    $npmExit = $LASTEXITCODE
    Pop-Location
    if ($npmExit -ne 0 -or -not (Test-Path $NodeModules)) {
        Write-Fail "Failed to install frontend dependencies via npm"
        exit 1
    }
    Write-Ok "Frontend dependencies installed successfully"
} else {
    Write-Ok "Frontend dependencies present"
}

$Datasets = Join-Path $RepoRoot "datasets"
if (-not (Test-Path $Datasets)) {
    Write-Warn "datasets\ not found - replay and warmup will fail until placed at repo root."
} else {
    Write-Ok "datasets\ present"
}

# ---------------------------------------------------------------------------
# 1. Environment file
# ---------------------------------------------------------------------------
Write-Step "Checking environment file"
$EnvFile = Join-Path $RepoRoot ".env"
$EnvExample = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Copy-Item $EnvExample $EnvFile
        Write-Ok "Created .env from .env.example (defaults: aegis/aegis/aegis on 127.0.0.1:5432)"
    } else {
        Write-Warn ".env and .env.example are missing"
    }
} else {
    Write-Ok ".env present"
}

# ---------------------------------------------------------------------------
# 2. PostgreSQL
# ---------------------------------------------------------------------------
Write-Step "Checking PostgreSQL"

# Find psql
$PsqlPath = $null
$cmdPsql = Get-Command psql.exe -ErrorAction SilentlyContinue
if ($cmdPsql) {
    $PsqlPath = $cmdPsql.Source
} else {
    $candidates = @(
        (Get-ChildItem "D:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue),
        (Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue)
    )
    if ($candidates.Count -gt 0) {
        $PsqlPath = $candidates[0].FullName
    }
}

# Check port 5432
$tcpOk = $false
try {
    $socket = New-Object System.Net.Sockets.TcpClient
    $async = $socket.BeginConnect("127.0.0.1", 5432, $null, $null)
    $success = $async.AsyncWaitHandle.WaitOne(1500)
    if ($success -and $socket.Connected) {
        $tcpOk = $true
        $socket.EndConnect($async)
    }
    $socket.Close()
} catch {
    $tcpOk = $false
}

if (-not $tcpOk) {
    Write-Info "Postgres not accepting connections on 5432 - checking Windows service"
    $pgServices = Get-Service *postgres* -ErrorAction SilentlyContinue
    if ($pgServices) {
        foreach ($s in $pgServices) {
            if ($s.Status -ne "Running") {
                Write-Info "Starting service $($s.Name)..."
                Start-Service $s.Name -ErrorAction SilentlyContinue
            }
        }
    }
    Start-Sleep -Seconds 2
}

# Test connection with aegis user
$dbReachable = $false
if ($PsqlPath) {
    $env:PGPASSWORD = "aegis"
    $null = & $PsqlPath -h 127.0.0.1 -U aegis -d aegis -c "\q" 2>&1
    if ($LASTEXITCODE -eq 0) {
        $dbReachable = $true
        Write-Ok "Database 'aegis' reachable as user 'aegis'"
    } else {
        Write-Warn "Could not connect as aegis/aegis@aegis via psql. If not yet created, see docs/SETUP.md"
    }
} else {
    Write-Info "psql.exe not found in PATH - skipping direct DB check"
    $dbReachable = $true
}

# Initialize schema and seed if tables are missing
if ($dbReachable -and $PsqlPath) {
    $env:PGPASSWORD = "aegis"
    $tableCountOutput = & $PsqlPath -h 127.0.0.1 -U aegis -d aegis -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>&1
    $tableCount = 0
    if ($tableCountOutput) {
        [int]::TryParse($tableCountOutput.ToString().Trim(), [ref]$tableCount) | Out-Null
    }
    if ($tableCount -lt 5) {
        Write-Info "Schema missing or incomplete ($tableCount tables) - running backend.init_db"
        $env:PYTHONPATH = "src"
        & $PythonBin -m backend.init_db
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Schema created and assets seeded"
        } else {
            Write-Fail "backend.init_db failed"
            exit 1
        }
    } else {
        Write-Ok "Schema already present ($tableCount tables)"
    }
}

# ---------------------------------------------------------------------------
# 3. Model artifacts
# ---------------------------------------------------------------------------
Write-Step "Checking model artifacts"

$StreamingScorerPath = Join-Path $RepoRoot "artifacts\streaming_scorer.joblib"
if (Test-Path $StreamingScorerPath) {
    Write-Ok "streaming_scorer.joblib present"
} else {
    Write-Info "Building streaming_scorer.joblib (backend.warmup) - ~5s"
    $env:PYTHONPATH = "src"
    & $PythonBin -m backend.warmup
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Built streaming_scorer.joblib"
    } else {
        Write-Warn "backend.warmup failed - datasets\ not found or incomplete. Console will start without pre-warmed model."
    }
}

$SupervisedScorerPath = Join-Path $RepoRoot "artifacts\supervised_flow_scorer.joblib"
if (Test-Path $SupervisedScorerPath) {
    Write-Ok "supervised_flow_scorer.joblib present"
} else {
    Write-Info "Building supervised_flow_scorer.joblib (backend.warmup_supervised) - ~4s"
    $env:PYTHONPATH = "src"
    & $PythonBin -m backend.warmup_supervised
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Built supervised_flow_scorer.joblib"
    } else {
        Write-Warn "backend.warmup_supervised failed - console will start with 2 channels instead of 3."
    }
}

# ---------------------------------------------------------------------------
# 4. Backend (uvicorn)
# ---------------------------------------------------------------------------
Write-Step "Starting backend (uvicorn)"

if ($Restart -and (Test-PidAlive $BackendPidFile)) {
    Write-Info "Stopping existing backend (-Restart)"
    $oldPidStr = (Get-Content $BackendPidFile | Select-Object -First 1).Trim()
    if ($oldPidStr -match '^\d+$') {
        $oldPid = [int]$oldPidStr
        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

$backendAlive = Test-PidAlive $BackendPidFile
$backendResponding = $false
try {
    $resp = Invoke-WebRequest -Uri "$BackendUrl/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    $backendResponding = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
} catch {}

if ($backendAlive) {
    $currPid = (Get-Content $BackendPidFile | Select-Object -First 1).Trim()
    Write-Ok "Backend already running (pid $currPid)"
} elseif ($backendResponding) {
    Write-Warn "Something is already answering on port $BackendPort - leaving it alone"
} else {
    if (Test-Path $BackendLog) { Remove-Item $BackendLog -Force -ErrorAction SilentlyContinue }
    if (Test-Path $BackendErrLog) { Remove-Item $BackendErrLog -Force -ErrorAction SilentlyContinue }

    $env:PYTHONPATH = "src"
    $proc = Start-Process -FilePath $PythonBin `
        -ArgumentList "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "$BackendPort" `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $BackendLog `
        -RedirectStandardError $BackendErrLog `
        -PassThru

    $proc.Id | Out-File -FilePath $BackendPidFile -Encoding ascii
    Write-Info "Launched (pid $($proc.Id)), waiting for $BackendUrl/api/health ..."

    if (Wait-For-Http "$BackendUrl/api/health" 30 "Backend") {
        Write-Ok "Backend is up at $BackendUrl"
    } else {
        Write-Fail "Backend failed to become healthy - see $BackendLog and $BackendErrLog"
        exit 1
    }
}

try {
    $health = Invoke-RestMethod -Uri "$BackendUrl/api/health" -TimeoutSec 3 -ErrorAction SilentlyContinue
    if ($health -and $health.scorer_loaded) {
        Write-Ok "Detection model loaded"
    } else {
        Write-Warn "Detection model not loaded - check backend.log"
    }
} catch {}

# ---------------------------------------------------------------------------
# 5. Frontend (Next.js)
# ---------------------------------------------------------------------------
Write-Step "Starting frontend (Next.js)"

if ($Restart -and (Test-PidAlive $FrontendPidFile)) {
    Write-Info "Stopping existing frontend (-Restart)"
    $oldPidStr = (Get-Content $FrontendPidFile | Select-Object -First 1).Trim()
    if ($oldPidStr -match '^\d+$') {
        $oldPid = [int]$oldPidStr
        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

$frontendAlive = Test-PidAlive $FrontendPidFile
$frontendResponding = $false
try {
    $resp = Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    $frontendResponding = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
} catch {}

if ($frontendAlive) {
    $currPid = (Get-Content $FrontendPidFile | Select-Object -First 1).Trim()
    Write-Ok "Frontend already running (pid $currPid)"
} elseif ($frontendResponding) {
    Write-Warn "Something is already answering on port $FrontendPort - leaving it alone"
} else {
    if (Test-Path $FrontendLog) { Remove-Item $FrontendLog -Force -ErrorAction SilentlyContinue }
    if (Test-Path $FrontendErrLog) { Remove-Item $FrontendErrLog -Force -ErrorAction SilentlyContinue }

    # Launch npm via cmd.exe
    $proc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "npm --prefix frontend run dev -- --port $FrontendPort" `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $FrontendLog `
        -RedirectStandardError $FrontendErrLog `
        -PassThru

    $proc.Id | Out-File -FilePath $FrontendPidFile -Encoding ascii
    Write-Info "Launched (pid $($proc.Id)), waiting for $FrontendUrl ..."

    if (Wait-For-Http $FrontendUrl 120 "Frontend") {
        Write-Ok "Frontend is up at $FrontendUrl"
    } else {
        Write-Fail "Frontend failed to become healthy - see $FrontendLog and $FrontendErrLog"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# 6. Replay
# ---------------------------------------------------------------------------
if (-not $NoReplay) {
    Write-Step "Starting replay"
    $stats = $null
    try {
        $stats = Invoke-RestMethod -Uri "$BackendUrl/api/stats" -TimeoutSec 3 -ErrorAction SilentlyContinue
    } catch {}

    if ($stats -and $stats.running -eq $true) {
        Write-Ok "Replay already running"
    } else {
        $body = @{ dataset = $Dataset; speed = $Speed } | ConvertTo-Json
        try {
            $resp = Invoke-RestMethod -Uri "$BackendUrl/api/replay/start" `
                -Method Post `
                -Body $body `
                -ContentType "application/json" `
                -TimeoutSec 5 `
                -ErrorAction Stop

            if ($resp -and $resp.running -eq $true) {
                Write-Ok "Replay started: $Dataset at ${Speed}x"
            } else {
                Write-Warn "Could not verify replay status"
            }
        } catch {
            Write-Warn "Failed to trigger replay automatically: $($_.Exception.Message)"
            Write-Warn "You can start it from the console header in the browser."
        }
    }
} else {
    Write-Step "Replay"
    Write-Info "Skipped (-NoReplay) - start it from the console header in your browser."
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Step "AEGIS is up"
Write-Host "  Console:  $FrontendUrl" -ForegroundColor Green
Write-Host "  API:      $BackendUrl  (docs at $BackendUrl/docs)" -ForegroundColor Green
Write-Host "  Logs:     $BackendLog" -ForegroundColor DarkGray
Write-Host "            $FrontendLog" -ForegroundColor DarkGray
Write-Host "  Stop:     .\scripts\dev-down.ps1" -ForegroundColor Yellow
Write-Host ""
