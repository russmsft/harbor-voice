[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    $bundled = Join-Path $projectRoot '.bootstrap\Scripts\uv.exe'
    if (-not (Test-Path -LiteralPath $bundled -PathType Leaf)) {
        throw 'uv was not found. Install uv or create the project bootstrap environment.'
    }
    $uvPath = $bundled
} else {
    $uvPath = $uv.Source
}
& $uvPath run pyinstaller --clean --noconfirm packaging\harbor-voice.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
$distRoot = Join-Path $projectRoot 'dist\HarborVoice'
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $distRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $distRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $distRoot -Force
Write-Output $distRoot
