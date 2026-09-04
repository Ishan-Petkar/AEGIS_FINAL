<#
.SYNOPSIS
  Stops the AEGIS Operations Console stack on Windows:
  stops replay, frontend (Next.js), and backend (uvicorn).
  Postgres is left running (shared system service).

.DESCRIPTION
  Native PowerShell equivalent of scripts/dev-down.sh.
  Robust by port, not just by PID: sweeps ports 3000 and 8000 to catch child processes.
#>

$ErrorActionPreference = "SilentlyContinue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$DevDir = Join-Path $RepoRoot ".dev"

$BackendPidFile = Join-Path $DevDir "backend.pid"
$FrontendPidFile = Join-Path $DevDir "frontend.pid"
$BackendPort = 8000
$FrontendPort = 3000

function Write-Ok($msg)   { Write-Host "  [+] $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  [.] $msg" -ForegroundColor DarkGray }
function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }

function Stop-ByPidFile($pidFile, $name) {
    if (Test-Path $pidFile) {
        $p = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($p -and ($p.Trim() -match '^\d+$')) {
            $pidNum = [int]$p.Trim()
            try {
                Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue
                Write-Info "$name : stopped pid $pidNum"
            } catch {}
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

function Sweep-Port($port, $name) {
    $pids = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($conns) {
            $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
        }
    } catch {}

    if (-not $pids -or $pids.Count -eq 0) {
        $lines = netstat -ano 2>$null | Select-String ":$port\s+.*LISTENING"
        foreach ($line in $lines) {
            $parts = ($line.ToString().Trim() -split '\s+')
            if ($parts.Length -ge 5) {
                $pidVal = $parts[-1]
                if ($pidVal -match '^\d+$' -and [int]$pidVal -ne 0) {
                    $pids += [int]$pidVal
                }
            }
        }
        $pids = $pids | Select-Object -Unique
    }

    if ($pids -and $pids.Count -gt 0) {
        foreach ($p in $pids) {
            if ($p -ne $PID -and $p -ne 0 -and $p -ne 4) {
                try {
                    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
                } catch {}
            }
        }
        Start-Sleep -Seconds 1
        Write-Ok "$name : port $port freed"
    } else {
        Write-Ok "$name : nothing listening on port $port"
    }
}

# 1. Stop replay
Write-Step "Stopping replay (best-effort)"
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/replay/stop" -Method Post -TimeoutSec 2 -ErrorAction Stop
    Write-Info "Replay stop requested"
} catch {
    Write-Info "Backend not reachable - skipping"
}

# 2. Stop frontend
Write-Step "Stopping frontend"
Stop-ByPidFile $FrontendPidFile "Frontend"
Sweep-Port $FrontendPort "Frontend"

# 3. Stop backend
Write-Step "Stopping backend"
Stop-ByPidFile $BackendPidFile "Backend"
Sweep-Port $BackendPort "Backend"

Write-Host "`n  PostgreSQL was left running - it is a shared system service.`n" -ForegroundColor DarkGray
