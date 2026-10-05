param(
    [string]$Xapk,
    [switch]$RefreshMaster,
    [switch]$ForceDump
)
$ErrorActionPreference = 'Stop'
$setupArgs = @()
if ($Xapk) { $setupArgs += @('--xapk', $Xapk) }
& powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\setup.ps1" @setupArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$prepareArgs = @()
if ($RefreshMaster) { $prepareArgs += '--refresh-master' }
if ($ForceDump) { $prepareArgs += '--force-dump' }
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\scripts\prepare.py" @prepareArgs
exit $LASTEXITCODE
