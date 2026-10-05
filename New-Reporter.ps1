param([Parameter(Mandatory = $true)][string]$Name)
$ErrorActionPreference = 'Stop'
& node (Join-Path $PSScriptRoot 'src\configuration.mjs') pair $Name
if ($LASTEXITCODE -ne 0) { throw 'Pairing failed.' }
