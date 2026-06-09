param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8012
)

$ErrorActionPreference = "Stop"
$BackendRoot = Split-Path -Parent $PSScriptRoot
Set-Location $BackendRoot

Write-Host "[INFO] Starting backend from $BackendRoot"
python main.py
