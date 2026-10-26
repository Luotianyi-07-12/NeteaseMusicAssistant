$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Install Python 3.10+ from python.org and enable Add Python to PATH.'
}
& python -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22 LTS or newer first.' }
& node -e 'if(Number(process.versions.node.split(".")[0])<22) process.exit(1)'
if ($LASTEXITCODE -ne 0) { throw 'Node.js 22+ is required.' }
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed. Check your network.' }
Push-Location -LiteralPath 'api-server'
try {
    & npm.cmd ci --ignore-scripts --no-audit --no-fund --cache (Join-Path $PSScriptRoot '.state/npm-cache')
    if ($LASTEXITCODE -ne 0) { throw 'API dependency installation failed. Check your network.' }
} finally { Pop-Location }
Write-Host 'Installation complete. Launch app.py with pythonw.exe, or use the packaged EXE.'
