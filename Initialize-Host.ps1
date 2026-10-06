param([string]$BindAddress = '127.0.0.1', [int]$IngestPort = 43188, [switch]$Reconfigure)
$ErrorActionPreference = 'Stop'
$arguments = @('-m', 'pymonitor.cli', 'config', 'initialize', $BindAddress, "$IngestPort")
if ($Reconfigure) { $arguments += 'replace' }
& python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Collector initialization failed.' }
