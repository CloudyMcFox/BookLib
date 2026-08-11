<#
.SYNOPSIS
    Copy the BookLib source tree to the server without touching live data.

.DESCRIPTION
    Wraps robocopy with a fixed exclusion list. The database and .env live in the
    same folders as the code, so a plain "copy everything" overwrites the running
    library and its secrets. Everything listed in $ExcludedFiles / $ExcludedDirs
    is never copied and never deleted, so whatever is on the server stays put.

    Both images build from source (backend installs requirements.txt, frontend
    runs npm ci + npm run build), so node_modules and dist are not copied either.

.PARAMETER Destination
    Where the server's booklib folder is reachable from this machine: a UNC path
    (\\server\share\booklib) or a mapped/mounted drive.

.PARAMETER Mirror
    Also delete files on the server that no longer exist here. Excluded files are
    still left alone. Off by default: an additive copy cannot destroy anything.

.PARAMETER DryRun
    List what would be copied and deleted, without writing anything.

.EXAMPLE
    .\deploy.ps1 -Destination \\bookserver\srv\booklib -DryRun
    .\deploy.ps1 -Destination \\bookserver\srv\booklib

    Then on the server:
    docker compose up -d --build
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,

    [switch]$Mirror,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot

# Live data and secrets. These are the whole reason this script exists.
$ExcludedFiles = @(
    'books.db', 'books.db-journal', 'books.db-wal', 'books.db-shm',
    '*.sqlite', '*.sqlite3',
    '.env',
    'deploy.ps1',
    '*.pyc', 'Thumbs.db', 'desktop.ini', '.DS_Store'
)

# Build artefacts and local tooling: rebuilt on the server, and node_modules
# would carry Windows binaries onto a Linux host.
$ExcludedDirs = @(
    'node_modules', 'dist', '.vite',
    '__pycache__', '.venv', 'venv',
    '.git', '.vscode', '.idea'
)

if (-not (Test-Path -LiteralPath $Destination)) {
    throw "Destination not reachable: $Destination"
}

Write-Host "Source      : $source"
Write-Host "Destination : $Destination"
Write-Host "Mode        : $(if ($DryRun) { 'DRY RUN (nothing written)' } elseif ($Mirror) { 'mirror (deletes removed files)' } else { 'copy (additive)' })"
Write-Host "Never touched: $($ExcludedFiles -join ', ')"
Write-Host ""

$args = @($source, $Destination, '/E', '/R:2', '/W:2', '/NP', '/FFT')
if ($Mirror) { $args += '/PURGE' }   # with /E this is /MIR, but exclusions still win
if ($DryRun) { $args += '/L' }
$args += '/XF'; $args += $ExcludedFiles
$args += '/XD'; $args += $ExcludedDirs

robocopy @args
$code = $LASTEXITCODE

# Robocopy: 0-7 are success (0 = nothing to do, 1 = files copied, 2 = extras,
# 4 = mismatches). 8 and above are real failures.
if ($code -ge 8) {
    Write-Error "robocopy failed with exit code $code"
    exit $code
}

Write-Host ""
Write-Host "Done (robocopy code $code)." -ForegroundColor Green
if (-not $DryRun) {
    Write-Host "The server's books.db and .env were left untouched."
    Write-Host "Now restart the stack on the server: docker compose up -d --build"
}
exit 0
