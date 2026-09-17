$ErrorActionPreference = "Stop"
$env:PYTHONPATH = (Resolve-Path (Join-Path $PSScriptRoot ".."))
uv run uvicorn api.app.main:app --host 127.0.0.1 --port 8000