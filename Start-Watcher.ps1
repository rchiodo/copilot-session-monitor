$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\Start-Role.ps1') -Role watcher -NoBrowser
