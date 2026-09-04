<#
.SYNOPSIS
  Opens the running AEGIS console (and optionally API docs) in default browser on Windows.

.PARAMETER Api
  Also open API documentation (http://127.0.0.1:8000/docs).

.EXAMPLE
  .\scripts\dev-open.ps1
  .\scripts\dev-open.ps1 -Api
#>

param(
    [switch]$Api,
    [switch]$Help
)

if ($Help) {
    Write-Host "Usage: .\scripts\dev-open.ps1 [-Api]"
    exit 0
}

$FrontendUrl = "http://127.0.0.1:3000"
$BackendDocsUrl = "http://127.0.0.1:8000/docs"

$frontendUp = $false
try {
    $resp = Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    $frontendUp = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
} catch {}

if ($frontendUp) {
    Write-Host "  [+] Console is up - opening $FrontendUrl" -ForegroundColor Green
    Start-Process $FrontendUrl
} else {
    Write-Host "  [!] Nothing is responding at $FrontendUrl yet." -ForegroundColor Yellow
    Write-Host "  Run .\scripts\dev-up.ps1 first, then re-run this." -ForegroundColor DarkGray
    exit 1
}

if ($Api) {
    $backendUp = $false
    try {
        $resp = Invoke-WebRequest -Uri $BackendDocsUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        $backendUp = ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
    } catch {}

    if ($backendUp) {
        Write-Host "  [+] Opening API docs - $BackendDocsUrl" -ForegroundColor Green
        Start-Process $BackendDocsUrl
    } else {
        Write-Host "  [!] Backend not responding at $BackendDocsUrl - skipping" -ForegroundColor Yellow
    }
}
