$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\Stop-Role.ps1') -Role collector
