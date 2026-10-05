$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & "$PSScriptRoot\.venv\Scripts\python.exe" -m ournotes @args
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
