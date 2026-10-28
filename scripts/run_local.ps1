$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @("python")
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @("py", "-3")
    }

    throw "Python was not found. Please install Python 3.10+ and add it to PATH."
}

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Step "Preparing DoraYmon in $ProjectRoot"

$PythonCommand = Resolve-Python
$PythonExe = $PythonCommand[0]
$PythonArgs = @()
if ($PythonCommand.Length -gt 1) {
    $PythonArgs = $PythonCommand[1..($PythonCommand.Length - 1)]
}

if (-not (Test-Path ".venv")) {
    Write-Step "Creating virtual environment"
    & $PythonExe @PythonArgs -m venv .venv
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment Python was not found at $VenvPython"
}

$DependencyStamp = Join-Path $ProjectRoot ".venv\.requirements.stamp"
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$ShouldInstallDependencies = -not (Test-Path $DependencyStamp)

if ((Test-Path $DependencyStamp) -and (Test-Path $RequirementsFile)) {
    $RequirementsUpdatedAt = (Get-Item $RequirementsFile).LastWriteTimeUtc
    $DependencyStampUpdatedAt = (Get-Item $DependencyStamp).LastWriteTimeUtc
    $ShouldInstallDependencies = $RequirementsUpdatedAt -gt $DependencyStampUpdatedAt
}

if ($ShouldInstallDependencies) {
    Write-Step "Installing dependencies"
    & $VenvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    New-Item -ItemType File -Path $DependencyStamp -Force | Out-Null
} else {
    Write-Step "Dependencies are up to date"
}

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Write-Step "Creating .env from .env.example"
    Copy-Item ".env.example" ".env"
    Write-Host "Please fill QQBOT_APPID and QQBOT_SECRET in .env before connecting to QQ Bot." -ForegroundColor Yellow
}

Write-Step "Starting DoraYmon"
& $VenvPython main.py
exit $LASTEXITCODE
