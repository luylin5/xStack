param(
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
    [string]$Compiler = ''
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Push-Location $projectDir
$previousPythonPath = $env:PYTHONPATH
$previousPath = $env:PATH
try {
    $env:PYTHONPATH = Join-Path $projectDir '.build-tools'
    $pythonDir = Split-Path -Parent $Python
    $env:PATH = "$pythonDir;$pythonDir\Scripts;$env:SystemRoot\System32;$env:SystemRoot"
    if (-not $Compiler) { $Compiler = Join-Path $projectDir '.build-tools\inno\ISCC.exe' }
    & $Python -m PyInstaller --noconfirm --distpath release/1.5 --workpath build/release-1.5 packaging/xstack.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $Compiler packaging/xstack.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
    Get-FileHash release/1.5/xStack-1.5-Setup.exe, release/1.5/xStack-1.5-Portable.exe -Algorithm SHA256
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PATH = $previousPath
    Pop-Location
}
