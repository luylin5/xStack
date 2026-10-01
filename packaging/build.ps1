param(
    # Leave empty to use the first interpreter that has the xStack dependencies.
    [string]$Python = '',
    [string]$Compiler = '',
    [switch]$SkipTests
)
# Native tools (unittest, PyInstaller, py) write progress to stderr, which Windows
# PowerShell 5.1 turns into errors under 'Stop'; each step checks $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$projectDir = Split-Path -Parent $PSScriptRoot
$check = 'import numpy, matplotlib, PyQt6.QtWidgets, scipy'

function Test-XStackPython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path $Candidate)) { return $false }
    $env:PYTHONPATH = ''
    & $Candidate -c $check *> $null
    return $LASTEXITCODE -eq 0
}

function Find-XStackPython {
    $candidates = @()
    if ($env:XSTACK_PYTHON) { $candidates += $env:XSTACK_PYTHON }
    $candidates += Join-Path $projectDir '.venv\Scripts\python.exe'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in '3.14', '3.13', '3.12', '3.11') {
            $exe = & py "-$version" -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += $exe.Trim() }
        }
    }
    $candidates += @(Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object Source)
    foreach ($candidate in $candidates) {
        if (Test-XStackPython $candidate) { return $candidate }
    }
    throw 'No Python with numpy, matplotlib, PyQt6 and scipy was found. Pass -Python or set XSTACK_PYTHON.'
}

Push-Location $projectDir
$previousPythonPath = $env:PYTHONPATH
$previousPath = $env:PATH
try {
    if (-not $Python) { $Python = Find-XStackPython }
    elseif (-not (Test-XStackPython $Python)) { throw "$Python is missing xStack dependencies." }
    Write-Host "Using Python: $Python"
    $version = [regex]::Match((Get-Content xstack_version.py -Raw), '__version__ = "([^"]+)"').Groups[1].Value
    if (-not $version) { throw 'Could not read __version__ from xstack_version.py' }
    Write-Host "Building xStack $version"
    if (-not $Compiler) { $Compiler = Join-Path $projectDir '.build-tools\inno\ISCC.exe' }

    if (-not $SkipTests) {
        $env:PYTHONPATH = ''
        $env:QT_QPA_PLATFORM = 'offscreen'
        & $Python -m unittest test_ui test_data_safety test_file_open test_live_normalization test_performance test_richtext
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; not building.' }
        Remove-Item Env:QT_QPA_PLATFORM
    }

    $env:PYTHONPATH = Join-Path $projectDir '.build-tools'
    $pythonDir = Split-Path -Parent $Python
    $env:PATH = "$pythonDir;$pythonDir\Scripts;$env:SystemRoot\System32;$env:SystemRoot"
    # Intermediates stay outside OneDrive: they are large, regenerated every build,
    # and OneDrive sync locks would make PyInstaller's cleanup fail.
    $workPath = Join-Path (Join-Path $env:LOCALAPPDATA 'xStack-build') "release-$version"
    & $Python -m PyInstaller --noconfirm --distpath "release/$version" --workpath $workPath packaging/xstack.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $Compiler "/DAppVer=$version" packaging/xstack.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
    Get-FileHash "release/$version/xStack-$version-Setup.exe", "release/$version/xStack-$version-Portable.exe" -Algorithm SHA256
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PATH = $previousPath
    Pop-Location
}
