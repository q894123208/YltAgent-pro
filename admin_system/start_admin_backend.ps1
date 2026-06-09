param(
    [string]$Python = "python",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8022,
    [string]$AdminUsername = "<ADMIN_USERNAME>",
    [string]$AdminPassword = "<ADMIN_PASSWORD>"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:MEDIX_ADMIN_USERNAME = $AdminUsername
$env:MEDIX_ADMIN_PASSWORD = $AdminPassword

& $Python -m uvicorn admin_system.backend.main:app --host $HostName --port $Port --reload
