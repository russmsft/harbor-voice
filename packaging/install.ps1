[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\HarborVoice'),
    [string]$StartMenuRoot = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Harbor Voice'),
    [switch]$EnableLaunchAtLogin = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sourcePath = [IO.Path]::GetFullPath($Source)
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$menuPath = [IO.Path]::GetFullPath($StartMenuRoot)
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'HarborVoice.exe') -PathType Leaf)) {
    throw "HarborVoice.exe was not found under $sourcePath"
}

New-Item -ItemType Directory -Force -Path $installPath | Out-Null
Get-ChildItem -LiteralPath $sourcePath -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $installPath -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $menuPath | Out-Null
$shortcutPath = Join-Path $menuPath 'Harbor Voice.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $installPath 'HarborVoice.exe'
$shortcut.WorkingDirectory = $installPath
$shortcut.Description = 'Harbor Voice personal assistant'
$shortcut.Save()

if ($EnableLaunchAtLogin) {
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-ItemProperty -Path $runKey -Name 'HarborVoice' `
        -Value ('"' + (Join-Path $installPath 'HarborVoice.exe') + '"') `
        -PropertyType String -Force | Out-Null
}

Write-Output "Harbor Voice installed to $installPath"

