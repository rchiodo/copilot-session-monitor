param([Parameter(Mandatory = $true)][string]$PairingFile)
$ErrorActionPreference = 'Stop'
& node (Join-Path $PSScriptRoot 'src\configuration.mjs') import $PairingFile
if ($LASTEXITCODE -ne 0) { throw 'Pairing import failed.' }
