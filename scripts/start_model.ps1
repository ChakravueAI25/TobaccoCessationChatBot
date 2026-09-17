# Starts llama-server for the study stack.
#
# Why this is a script and not a command in tasks.json: VS Code refused to launch the exe
# directly ("Path to shell executable ... does not exist") even though the file existed and ran
# fine from a shell, because the path it built mixed separators - LOCALAPPDATA contributes
# backslashes and the task contributed forward slashes. Resolving the path in PowerShell, and
# letting VS Code run a plain `powershell -File`, sidesteps VS Code's own path validation
# entirely. That is the same shape as the Health check task, which has always launched.
#
# ASCII only - PowerShell 5.1 reads a BOM-less file as CP1252 and an em dash's UTF-8 bytes end
# in 0x94, a closing double quote, which terminates a string early.
#
# ---------------------------------------------------------------------------------------------
# THE BINARY LIVES IN THIS REPO, NOT IN %LOCALAPPDATA%.
#
# That is the fix for three rounds of "the llama-server binary is not there" reported about a
# file that was demonstrably on disk, 9,216 bytes, timestamp unchanged, and which ran when
# executed directly seconds earlier.
#
# The cause, found with Get-Item -Force on the folder: %LOCALAPPDATA%\quitsmoke-llamahost
# resolved to
#
#     ...\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\quitsmoke-llamahost
#
# It had been built by an agent session running inside the Claude desktop app's MSIX container,
# where writes to LocalAppData are redirected into that package's own private cache. Inside the
# container the path resolves and the exe runs; from a VS Code task outside it, the same path is
# not reachable. That is why every existence check disagreed with reality, and why the launch
# itself finally failed with CommandNotFoundException. The directory ACL confirmed it - an
# S-1-15-3-... AppContainer package SID.
#
# It was never antivirus and never Controlled Folder Access. Defender reported
# EnableControlledFolderAccess = 0 the whole time, and an earlier version of this comment
# blaming antivirus was wrong.
#
# So the binary now lives under bin\llamahost in this repo, on the same drive as the weights,
# which every context reads without redirection. LOCALAPPDATA is still tried as a fallback so an
# existing machine keeps working.
#
# The probe below only WARNS and never aborts, which is a separate lesson from the same episode:
# a check that cannot see the file it is checking has no business deciding whether to stop. Only
# the launch decides, because the operating system is the one authority that is never wrong about
# whether an executable runs.
# ---------------------------------------------------------------------------------------------

[CmdletBinding()]
param(
    [int]$Port = 8081,
    [int]$ContextLength = 2048,
    # Layers to offload to the GPU. 99 means "all of them"; measured 2,827 of 4,096 MiB on the
    # RTX 3050. Pass a lower number if something else is holding video memory.
    [int]$GpuLayers = 99
)

$ErrorActionPreference = 'Stop'

# Advisory only. Never exits. See the header for why.
function Warn-IfUnseen {
    param([string]$Path, [string]$What, [string]$Fix)

    $seen = $false
    try { $seen = [System.IO.File]::Exists($Path) } catch { }
    if ($seen) { return }

    Write-Host "Note: cannot see the $What from this process." -ForegroundColor DarkYellow
    Write-Host "      Starting anyway; if it really is absent the launch below will say so." -ForegroundColor DarkYellow
    Write-Host "      If the launch fails for real: $Fix" -ForegroundColor DarkYellow
    Write-Host ""
}

$RepoRoot  = Resolve-Path (Join-Path $PSScriptRoot '..')
$ModelPath = Join-Path $RepoRoot 'models\Qwen3-4B-Q4_K_M.gguf'

# In-repo first, for the reason in the header.
$InRepoExe = Join-Path $RepoRoot 'bin\llamahost\llama-server.exe'
$LocalExe  = Join-Path $env:LOCALAPPDATA 'quitsmoke-llamahost\llama-server.exe'

$Exe   = $InRepoExe
$Where = 'in repo'
if (-not [System.IO.File]::Exists($InRepoExe)) {
    $Exe   = $LocalExe
    $Where = 'LOCALAPPDATA fallback - see the header, this one can be container-private'
}

$BuildScript    = '..\TobaccoCessationChatbot\scripts\promptlab\build_host_llama.ps1'
$DownloadScript = '..\TobaccoCessationChatbot\scripts\download_model.ps1'

Write-Host "llama-server" -ForegroundColor Cyan
Write-Host "  binary : $Exe"
Write-Host "           ($Where)"
Write-Host "  model  : $ModelPath"
Write-Host "  port   : $Port   context: $ContextLength   gpu layers: $GpuLayers"
Write-Host ""

Warn-IfUnseen $Exe       "llama-server binary" "powershell -File $BuildScript"
Warn-IfUnseen $ModelPath "model weights"       "powershell -File $DownloadScript"

# The port check IS reliable and does gate, because it asks the network stack rather than the
# filesystem. A port already in use is the other common cause of "the model is broken": the task
# starts, fails to bind, exits, and the API keeps answering 503.
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    $owner = (Get-Process -Id $inUse[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Host "Port $Port is already listening (process: $owner, PID $($inUse[0].OwningProcess))." -ForegroundColor Yellow
    if ($owner -eq 'llama-server') {
        # Exit 0, not 1. A model that is already up is the task having nothing to do, not a
        # failure - and reporting it as one is what makes somebody go looking for a fault.
        Write-Host "That is already a llama-server, so the model is up and nothing needs doing." -ForegroundColor Green
        Write-Host "Check it with:  curl http://127.0.0.1:$Port/health"
        exit 0
    }
    Write-Host "Stop whatever owns that port first, then re-run this task." -ForegroundColor Yellow
    exit 1
}

Write-Host "Loading the model (first start reads 2.5 GB from disk, allow 30-60s)..." -ForegroundColor Cyan
Write-Host "Leave this terminal open - the server runs for as long as this task does." -ForegroundColor Cyan
Write-Host ""

# The only check that counts.
try {
    & $Exe -m $ModelPath -c $ContextLength -ngl $GpuLayers --host 127.0.0.1 --port $Port
    $code = $LASTEXITCODE
} catch [System.Management.Automation.CommandNotFoundException] {
    Write-Host ""
    Write-Host "No runnable llama-server at either location:" -ForegroundColor Red
    Write-Host "  $InRepoExe"
    Write-Host "  $LocalExe"
    Write-Host ""
    Write-Host "Build it, then copy the WHOLE output folder to bin\llamahost in this repo:" -ForegroundColor Red
    Write-Host "  powershell -File $BuildScript"
    Write-Host ""
    Write-Host "In-repo matters. A build that lands in an app-container LocalAppData is invisible" -ForegroundColor Red
    Write-Host "to VS Code tasks while working perfectly from the shell that built it, which is" -ForegroundColor Red
    Write-Host "exactly how this failed three times." -ForegroundColor Red
    exit 1
} catch {
    Write-Host ""
    Write-Host "llama-server could not start: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# Reaching here means the server exited. It is supposed to run until the task is stopped, so a
# quiet exit is worth naming rather than letting the terminal just close.
if ($code -ne 0) {
    Write-Host ""
    Write-Host "llama-server exited with code $code." -ForegroundColor Red
    exit $code
}

Write-Host ""
Write-Host "llama-server exited normally (the task was stopped, or the port was closed)." -ForegroundColor Yellow
