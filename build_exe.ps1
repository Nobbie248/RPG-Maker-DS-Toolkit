$ErrorActionPreference = "Stop"

$WorkspacePython = "C:\Users\Roman\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$AppName = "RPG Maker DS Toolkit"
$DistDir = Join-Path $ProjectRoot "dist"
$BuiltExe = Join-Path $DistDir "$AppName.exe"
$NativeSource = Join-Path $ProjectRoot "native\ncsf_preview"
$NativeBuild = Join-Path $NativeSource "build"
$NativeRenderer = Join-Path $NativeBuild "rpgds_ncsf_preview.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $WorkspacePython -m venv (Join-Path $ProjectRoot ".venv")
}

& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE" }

cmake -S $NativeSource -B $NativeBuild -G Ninja -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { throw "Native audio renderer configuration failed with exit code $LASTEXITCODE" }
cmake --build $NativeBuild
if ($LASTEXITCODE -ne 0) { throw "Native audio renderer build failed with exit code $LASTEXITCODE" }

& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $AppName `
    --distpath $DistDir `
    --workpath (Join-Path $ProjectRoot "build") `
    --add-binary "$NativeRenderer;." `
    (Join-Path $ProjectRoot "rpgds_gui.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$LegacyNames = @(
    "RPGDS Translator Build.exe",
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

# PyInstaller's work tree and generated spec are disposable. Keep only the
# single replacement executable in dist so repeated builds do not clutter the
# repository.
$GeneratedSpec = Join-Path $ProjectRoot "$AppName.spec"
$GeneratedBuild = Join-Path $ProjectRoot "build"
if (Test-Path -LiteralPath $GeneratedSpec) {
    Remove-Item -LiteralPath $GeneratedSpec -Force
}
if (Test-Path -LiteralPath $GeneratedBuild) {
    Remove-Item -LiteralPath $GeneratedBuild -Recurse -Force
}
foreach ($CachePath in @(
    (Join-Path $ProjectRoot "__pycache__"),
    (Join-Path $ProjectRoot "tests\__pycache__")
)) {
    if (Test-Path -LiteralPath $CachePath) {
        Remove-Item -LiteralPath $CachePath -Recurse -Force
    }
}

Write-Host "Built: $BuiltExe"
