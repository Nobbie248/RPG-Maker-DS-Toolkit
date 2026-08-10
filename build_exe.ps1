$ErrorActionPreference = "Stop"

$WorkspacePython = "C:\Users\Roman\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$AppName = "RPGDS Translator Build"
$DistDir = Join-Path $ProjectRoot "dist"
$BuiltExe = Join-Path $DistDir "$AppName.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $WorkspacePython -m venv (Join-Path $ProjectRoot ".venv")
}

& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE" }
& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $AppName `
    --distpath $DistDir `
    --workpath (Join-Path $ProjectRoot "build") `
    (Join-Path $ProjectRoot "rpgds_gui.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$LegacyNames = @(
    "RPGDS_Translator_Dual.exe",
    "RPGDS_Translator_Dual_SafeImages.exe"
)
foreach ($LegacyName in $LegacyNames) {
    $LegacyPath = Join-Path $DistDir $LegacyName
    if ((Test-Path -LiteralPath $LegacyPath) -and $LegacyPath -ne $BuiltExe) {
        try {
            Remove-Item -LiteralPath $LegacyPath -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not remove the old build '$LegacyPath'. Close the old app before the next build."
        }
    }
}

Write-Host "Built: $BuiltExe"
