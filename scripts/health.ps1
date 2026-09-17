# Is the study reachable right now?
#
#     powershell -ExecutionPolicy Bypass -File scripts\health.ps1
#
# Answers the only question that matters to a participant - can they use it - and separates the
# three ways the answer can be no, because the app behaves differently for each.
#
# `curl.exe` rather than `Invoke-RestMethod`: the latter's -TimeoutSec does not cover DNS or the
# TLS handshake, so a probe against a dead tunnel hangs indefinitely. curl's --max-time is a hard
# ceiling on the whole call.

$ngrokUrl = "favorably-immature-fountain.ngrok-free.dev"

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

$api    = Probe "backend"  "http://127.0.0.1:8000/health"   '*"database":"ok"*'
$model  = Probe "model"    "http://127.0.0.1:8081/health"   '*"status":"ok"*'
$tunnel = Probe "tunnel"   "https://$ngrokUrl/health"       '*"database":"ok"*'
Write-Host ""

if ($api -and $tunnel) {
    Write-Host "  Participants can use the app." -ForegroundColor Green
    if (-not $model) {
        # The important one to say out loud. The fallback is good enough that a dead model looks
        # like a working conversation - it hid for a whole day once.
        Write-Host "  The model is NOT answering, so every reply will be the deterministic" -ForegroundColor Yellow
        Write-Host "  fallback. Nothing on screen says so." -ForegroundColor Yellow
    }
} elseif ($api -and -not $tunnel) {
    Write-Host "  Reachable on this network only - the tunnel is down, so a phone on mobile" -ForegroundColor Yellow
    Write-Host "  data cannot sign in or sync. The app still works offline." -ForegroundColor Yellow
} else {
    Write-Host "  Participants CANNOT sign in or sync. The app still works offline and" -ForegroundColor Red
    Write-Host "  nothing they do is lost - it queues on the phone until this is back." -ForegroundColor Red
}
Write-Host ""
