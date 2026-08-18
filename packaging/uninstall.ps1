[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\HarborVoice'),
    [string]$StartMenuRoot = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Harbor Voice'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'HarborVoice'),
    [switch]$RemoveUserData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-SafeRemovalTarget {
    param([Parameter(Mandatory = $true)][string]$Path, [string]$Label)
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    $protected = @(
        $root,
        [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\'),
        [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\'),
        [IO.Path]::GetFullPath($env:APPDATA).TrimEnd('\')
    )
    if (-not $full -or $protected -contains $full) {
        throw "Refusing broad $Label removal target: $full"
    }
    return $full
}

function Assert-OwnedRemovalTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Marker,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $markerPath = Join-Path $Path $Marker
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Refusing $Label removal because its Harbor Voice ownership marker is missing: $Path"
    }
}

$installPath = Resolve-SafeRemovalTarget -Path $InstallRoot -Label 'installation'
$menuPath = Resolve-SafeRemovalTarget -Path $StartMenuRoot -Label 'Start Menu'
$dataPath = Resolve-SafeRemovalTarget -Path $DataRoot -Label 'user data'

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$expectedRunValue = '"' + (Join-Path $installPath 'HarborVoice.exe') + '"'
$configuredRunValue = $null
$runValues = Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue
if ($null -ne $runValues) {
    $runProperty = $runValues.PSObject.Properties['HarborVoice']
    if ($null -ne $runProperty) {
        $configuredRunValue = $runProperty.Value
    }
}
if ($configuredRunValue -eq $expectedRunValue) {
    Remove-ItemProperty -Path $runKey -Name 'HarborVoice'
}
if (Test-Path -LiteralPath $menuPath) {
    Assert-OwnedRemovalTarget -Path $menuPath -Marker '.harbor-voice-start-menu' `
        -Label 'Start Menu'
    Remove-Item -LiteralPath $menuPath -Recurse -Force
}
if (Test-Path -LiteralPath $installPath) {
    Assert-OwnedRemovalTarget -Path $installPath -Marker '.harbor-voice-installation' `
        -Label 'installation'
    Remove-Item -LiteralPath $installPath -Recurse -Force
}
if ($RemoveUserData -and (Test-Path -LiteralPath $dataPath)) {
    Assert-OwnedRemovalTarget -Path $dataPath -Marker '.harbor-voice-data' `
        -Label 'user data'
    Remove-Item -LiteralPath $dataPath -Recurse -Force
}

Write-Output 'Harbor Voice uninstalled.'
if (-not $RemoveUserData) {
    Write-Output "User data preserved at $dataPath"
}
