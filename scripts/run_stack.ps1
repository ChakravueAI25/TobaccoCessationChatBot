# Starts everything a participant's phone needs, and says whether each part actually came up.
#
#     powershell -ExecutionPolicy Bypass -File scripts\run_stack.ps1
#
# There are three processes, not one, and they fail independently - which is the reason this
# script exists rather than a line in a README. The app degrades differently for each:
#
#   backend down  -> sign-in fails, nothing syncs, and the app still works offline
#   model down    -> every reply is the fallback, and nothing says so on screen
#   tunnel down   -> the phone cannot reach any of it from outside this network
#
# The middle one is the dangerous case. On 29 August the app looked like it was holding a
# conversation for a whole day while the model had served zero completions, so this script checks
# each part and prints what is actually true rather than "started".
#
# Stop everything with scripts\stop_stack.ps1.

$ErrorActionPreference = "Stop"

$root       = Resolve-Path (Join-Path $PSScriptRoot "..")
$llamaExe   = "$env:LOCALAPPDATA\quitsmoke-llamahost\llama-server.exe"
$modelPath  = (Join-Path $PSScriptRoot ".." | Join-Path -ChildPath "models/Qwen3-4B-Q4_K_M.gguf")
$ngrokUrl   = "favorably-immature-fountain.ngrok-free.dev"
$logDir     = Join-Path $root "logs"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-Port([int]$Port) {
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Start-Detached([string]$Exe, [string[]]$Arguments, [string]$LogName) {
    # `$Arguments`, not `$Args`: `$Args` is a PowerShell automatic variable holding the enclosing
    # scope's own arguments, so a parameter of that name is silently shadowed and arrives empty.
    #
    # -RedirectStandardOutput needs a real path, and the process must outlive this script, so
    # nothing here is piped back into the console.
    Start-Process -FilePath $Exe -ArgumentList $Arguments -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "$LogName.log") `
        -RedirectStandardError  (Join-Path $logDir "$LogName.err") | Out-Null
}

Write-Host ""
Write-Host "Quit Smoke - starting the study stack" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------- 1. the API
if (Test-Port 8000) {
    # Deliberately not "already running, nothing to do". `uvicorn` here runs without --reload, so
    # a process started before a code change is still serving the old code. That is not
    # hypothetical: it made a DELETE route answer 405 through the tunnel while its own tests
    # passed, and only an end-to-end call found it.
    Write-Host "  backend        already listening on 8000" -ForegroundColor Yellow
    Write-Host "                 NOTE: it holds the code it started with. Restart after any" -ForegroundColor Yellow
    Write-Host "                 change under api/ - stop_stack.ps1, then run this again." -ForegroundColor Yellow
} else {
    Push-Location $root
    Start-Detached "uv" @("run", "uvicorn", "api.app.main:app", "--host", "127.0.0.1", "--port", "8000") "uvicorn"
    Pop-Location
    Write-Host "  backend        starting on 8000..."
}

# ---------------------------------------------------------------- 2. the model
if (Test-Port 8081) {
    Write-Host "  model          already listening on 8081" -ForegroundColor Yellow
} elseif (-not (Test-Path $llamaExe)) {
    Write-Host "  model          NOT FOUND at $llamaExe" -ForegroundColor Red
    Write-Host "                 Build it with the app repo's scripts/promptlab/build_host_llama.ps1." -ForegroundColor Red
    Write-Host "                 The stack still works: every reply falls back, which is a" -ForegroundColor Red
    Write-Host "                 supported state rather than an outage." -ForegroundColor Red
} elseif (-not (Test-Path $modelPath)) {
    Write-Host "  model          weights missing at $modelPath" -ForegroundColor Red
} else {
    # -ngl 99 puts every layer it can on the GPU. -c 2048 matches the app's context length;
    # raising it costs video memory linearly and needs re-measuring first.
    # The model path is quoted because it contains a space ("ChakraVue AI").
    #
    # `Start-Process -ArgumentList` joins the array with spaces and does NOT quote elements, so
    # an unquoted path arrives at the executable split in two - llama-server reported
    # `invalid argument: AI\TobaccoCessationChatbot\...` and exited. The repo's older serve
    # script carried a comment about exactly this and it still caught me out.
    Start-Detached $llamaExe @("-m", "`"$modelPath`"", "-c", "2048", "-ngl", "99",
                               "--host", "127.0.0.1", "--port", "8081") "llama-server"
    Write-Host "  model          starting on 8081... (first load takes ~20s)"
}

# ---------------------------------------------------------------- 3. the tunnel
if (Test-Port 4040) {
    Write-Host "  tunnel         already running" -ForegroundColor Yellow
} else {
    Start-Detached "ngrok" @("http", "8000", "--url=$ngrokUrl") "ngrok"
    Write-Host "  tunnel         starting..."
}

Write-Host ""
# The model is the slow one - it maps 2.5 GB of weights before it answers.
Write-Host "  waiting for everything to answer (the model takes ~20s to load)..."
Start-Sleep -Seconds 25
Write-Host ""

# ---------------------------------------------------------------- what is actually true
#
# `curl.exe`, not `Invoke-RestMethod`. Invoke-RestMethod's -TimeoutSec does not cover the DNS
# lookup or the TLS handshake, so a probe against the tunnel can hang indefinitely - which is
# what happened: this script sat for fifteen minutes looking busy while the three services it had
# already started were up and answering. curl's --max-time is a hard ceiling on the whole call.
function Probe([string]$Label, [string]$Url, [string]$Expect) {
    # -join, because curl.exe returns an array of lines and PowerShell renders that as
    # "System.Object[]" when interpolated.
    $body = (& curl.exe -s --max-time 8 -H "ngrok-skip-browser-warning: 1" $Url 2>$null) -join ""
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($body)) {
        Write-Host ("  {0,-14} DOWN" -f $Label) -ForegroundColor Red
        return $false
    }
    # Matched against the exact health payload, not a loose substring. ngrok's own offline error
    # page is HTML containing the word "ok", so a bare -match "ok" reported a dead tunnel as
    # healthy - which is the one wrong answer a health check must never give.
    if ($body -like $Expect) {
        Write-Host ("  {0,-14} OK    {1}" -f $Label, $body.Trim()) -ForegroundColor Green
        return $true
    }
    Write-Host ("  {0,-14} BAD   {1}" -f $Label, $body.Substring(0, [Math]::Min(60, $body.Length))) -ForegroundColor Yellow
    return $false
}

$apiOk    = Probe "backend"  "http://127.0.0.1:8000/health"   '*"database":"ok"*'
$modelOk  = Probe "model"    "http://127.0.0.1:8081/health"   '*"status":"ok"*'
$tunnelOk = Probe "tunnel"   "https://$ngrokUrl/health"       '*"database":"ok"*'

Write-Host ""
if ($apiOk -and $tunnelOk) {
    Write-Host "  Participants can reach the study at https://$ngrokUrl" -ForegroundColor Green
    if (-not $modelOk) {
        Write-Host "  The model is NOT answering. Every reply will be the fallback, and" -ForegroundColor Yellow
        Write-Host "  nothing on screen says so - check logs\llama-server.err." -ForegroundColor Yellow
    }
} else {
    Write-Host "  Participants CANNOT reach the study. See logs\ for why." -ForegroundColor Red
}
Write-Host ""
Write-Host "  logs           $logDir"
Write-Host "  data           uv run python scripts\show_data.py"
Write-Host "  stop           powershell -ExecutionPolicy Bypass -File scripts\stop_stack.ps1"
Write-Host ""
