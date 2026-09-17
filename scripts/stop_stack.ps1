# Stops the three processes run_stack.ps1 starts.
#
#     powershell -ExecutionPolicy Bypass -File scripts\stop_stack.ps1
#
# By listening port rather than by process name, because all three are hosted by executables with
# names shared by other things on a developer machine - `python`, `uv`, and whatever else. Killing
# every `python.exe` to stop the study backend is the sort of thing that ends a different day's
# work by accident.
#
# The database is left running. It is a Windows service, nothing in the study starts or stops it,
# and it holds the only data that cannot be rebuilt.

$ErrorActionPreference = "Continue"

$targets = @(
    @{ Port = 8000; Name = "backend" },
    @{ Port = 8081; Name = "model" },
    @{ Port = 4040; Name = "tunnel" }
)

Write-Host ""
foreach ($target in $targets) {
    $connections = Get-NetTCPConnection -LocalPort $target.Port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        Write-Host ("  {0,-10} not running" -f $target.Name)
        continue
    }
    foreach ($connection in $connections) {
        try {
            $process = Get-Process -Id $connection.OwningProcess -ErrorAction Stop
            Stop-Process -Id $process.Id -Force
            Write-Host ("  {0,-10} stopped ({1}, pid {2})" -f $target.Name, $process.ProcessName, $process.Id) -ForegroundColor Green
        } catch {
            Write-Host ("  {0,-10} could not stop: {1}" -f $target.Name, $_.Exception.Message) -ForegroundColor Red
        }
    }
}
Write-Host ""
Write-Host "  PostgreSQL left running - it is a service, and it holds the data." -ForegroundColor DarkGray
Write-Host ""
