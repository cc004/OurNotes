$ErrorActionPreference = 'Stop'
& py -3.11 "$PSScriptRoot\scripts\setup.py" @args
exit $LASTEXITCODE
