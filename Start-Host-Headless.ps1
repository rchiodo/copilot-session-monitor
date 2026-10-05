param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\Start-Role.ps1') -Role collector -NoBrowser:$NoBrowser
