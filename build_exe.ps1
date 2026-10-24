$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Installing build requirements failed."
}

python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --noconfirm `
    --name GPUCheck `
    --distpath dist `
    --workpath build `
    --specpath build `
    gpucheck.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Host "Built: $ProjectRoot\dist\GPUCheck.exe"
