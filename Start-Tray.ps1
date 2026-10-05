param(
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Arguments,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
# Single entry point for the tray-based UX: `Start-Tray.ps1` (no args) runs child/watcher mode;
# `Start-Tray.ps1 /host` runs host/collector mode. Each mode is the unchanged existing role,
# with its own always-visible tray icon offering role-appropriate clipboard-pairing menu items.
$hostMode = $Arguments -contains '/host'
if ($hostMode) {
    & (Join-Path $PSScriptRoot 'Start-Host-Headless.ps1') -NoBrowser:$NoBrowser
} else {
    & (Join-Path $PSScriptRoot 'Start-Client-Headless.ps1')
}
