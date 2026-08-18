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
    Remove-Item -LiteralPath $menuPath -Recurse -Force
}
if (Test-Path -LiteralPath $installPath) {
    Remove-Item -LiteralPath $installPath -Recurse -Force
}
if ($RemoveUserData -and (Test-Path -LiteralPath $dataPath)) {
    Remove-Item -LiteralPath $dataPath -Recurse -Force
}

Write-Output 'Harbor Voice uninstalled.'
if (-not $RemoveUserData) {
    Write-Output "User data preserved at $dataPath"
}
