param(
    [string]$ServiceName = "postgresql-x64-18",
    [string]$PsqlPath = "<YOUR_POSTGRESQL_BIN_DIR>\psql.exe",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 5432,
    [string]$Database = "<POSTGRES_DATABASE>",
    [string]$User = "<POSTGRES_USER>"
)

$ErrorActionPreference = "Stop"

Write-Host "[INFO] Checking PostgreSQL service: $ServiceName"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if (-not $service) {
    throw "PostgreSQL service '$ServiceName' was not found. Please check the service name in Windows Services."
}

if ($service.Status -ne "Running") {
    Write-Host "[INFO] Starting PostgreSQL service..."
    Start-Service -Name $ServiceName
} else {
    Write-Host "[INFO] PostgreSQL service is already running."
}

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $service.Refresh()
} while ($service.Status -ne "Running" -and (Get-Date) -lt $deadline)

if ($service.Status -ne "Running") {
    throw "PostgreSQL service did not enter Running state in time."
}

Write-Host "[INFO] PostgreSQL service status: $($service.Status)"

if (-not (Test-Path $PsqlPath)) {
    Write-Host "[WARN] psql.exe not found at $PsqlPath, skipped connection test."
    exit 0
}

if (-not $env:PGPASSWORD) {
    throw "Please set environment variable PGPASSWORD before running this script."
}

Write-Host "[INFO] Testing connection: $User@$HostName`:$Port/$Database"
& $PsqlPath -h $HostName -p $Port -U $User -d $Database -c "select current_database(), current_user;"

if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL service is running, but database connection test failed."
}

Write-Host "[OK] PostgreSQL is ready."
