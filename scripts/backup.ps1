param([string]$OutputDirectory = "backups")
$ErrorActionPreference = "Stop"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$output = Join-Path $OutputDirectory "quit_smoke_research-$timestamp.dump"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
pg_dump --format=custom --file=$output --dbname=$env:DATABASE_URL
Write-Output "Backup written to $output"