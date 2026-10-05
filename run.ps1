$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run .\setup.ps1 first.'
}
Push-Location $PSScriptRoot
try {
    if ($args.Count -eq 0) {
        & $python -m nnnotes --config nnnotes.toml browse --host 127.0.0.1 --port 8000
    } elseif ($args[0] -in @('browse-apk', 'catalog-apk')) {
        $command = if ($args[0] -eq 'browse-apk') { 'browse' } else { 'catalog' }
        $rest = @($args | Select-Object -Skip 1)
        & $python scripts/offline.py $command @rest
    } else {
        & $python -m nnnotes --config nnnotes.toml @args
    }
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
