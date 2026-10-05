$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'work\browser.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'No recorded background browser. Stop a foreground browser with Ctrl+C.'
    exit 0
}
$browserPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$browserPattern = '(scripts[/\\]offline\.py.*browse|-m nnnotes\s+.*\bbrowse\b)'
$browserProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $browserPid"
if ($null -ne $browserProcess) {
    $expectedPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if ($browserProcess.ExecutablePath -ne $expectedPython -or $browserProcess.CommandLine -notmatch $browserPattern) {
        throw 'Recorded PID no longer belongs to this workspace browser; process was not stopped.'
    }
    # Windows venv's python.exe forwards execution to a child Python process.
    Get-CimInstance Win32_Process -Filter "ParentProcessId = $browserPid" |
        Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match $browserPattern } |
        ForEach-Object { Stop-Process -Id $_.ProcessId }
    Stop-Process -Id $browserPid -ErrorAction SilentlyContinue
    Write-Output 'Background APK browser stopped.'
}
Remove-Item -LiteralPath $pidFile
