param([Parameter(Mandatory = $true)][string]$MonitorUrl)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Threading;
public static class MonitorInput {
    public static readonly ConcurrentQueue<string> Lines = new ConcurrentQueue<string>();
    public static readonly ConcurrentQueue<string> PowerEvents = new ConcurrentQueue<string>();
    public static volatile bool Closed;
    public static void Start() {
        Microsoft.Win32.SystemEvents.PowerModeChanged += (sender, args) => {
            PowerEvents.Enqueue(args.Mode.ToString());
        };
        var reader = new Thread(() => {
            try {
                string line;
                while ((line = Console.In.ReadLine()) != null) Lines.Enqueue(line);
            } finally { Closed = true; }
        });
        reader.IsBackground = true;
        reader.Start();
    }
}
'@
[System.Windows.Forms.Application]::EnableVisualStyles()

function Send-BridgeMessage($Message) {
    [Console]::Out.WriteLine(($Message | ConvertTo-Json -Compress -Depth 6))
    [Console]::Out.Flush()
}

function Get-WindowsAppTheme {
    $key = $null
    try {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Themes\Personalize', $false)
        if ($null -eq $key) { throw 'Personalize registry key unavailable' }
        $value = $key.GetValue('AppsUseLightTheme', $null)
        if ($value -ne 0 -and $value -ne 1) { throw 'AppsUseLightTheme is missing or unsupported' }
        return @{ type = 'theme'; mode = $(if ($value -eq 1) { 'light' } else { 'dark' }) }
    } catch {
        return @{ type = 'theme'; mode = $null; reason = "Windows app preference unavailable: $($_.Exception.Message)" }
    } finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

$script:tray = New-Object System.Windows.Forms.NotifyIcon
$script:tray.Icon = [System.Drawing.SystemIcons]::Information
$script:tray.Text = 'Copilot session monitor - this machine only'
$script:tray.Visible = $true
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $menu.Items.Add('Open observed sessions')
$testItem = $menu.Items.Add('Test notification')
[void]$menu.Items.Add('-')
$exitItem = $menu.Items.Add('Stop monitor')
$openItem.add_Click({ Start-Process $MonitorUrl })
$script:tray.add_DoubleClick({ Start-Process $MonitorUrl })
$testItem.add_Click({ Send-BridgeMessage @{ type = 'test' } })
$exitItem.add_Click({ Send-BridgeMessage @{ type = 'stop' } })
$script:tray.ContextMenuStrip = $menu
$script:queue = New-Object 'System.Collections.Generic.Queue[object]'
$script:current = $null
$script:nextBalloon = [DateTime]::MinValue
$script:nextProcesses = [DateTime]::MinValue
[MonitorInput]::Start()
$script:tray.add_BalloonTipShown({
    if ($null -ne $script:current) {
        Send-BridgeMessage @{ type = 'notification-shown'; id = $script:current.id }
    }
})
$script:tray.add_BalloonTipClicked({ Start-Process $MonitorUrl })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.add_Tick({
    try {
        $line = $null
        while ([MonitorInput]::Lines.TryDequeue([ref]$line)) {
            $message = $line | ConvertFrom-Json
            if ($message.type -eq 'notify') {
                $script:queue.Enqueue($message)
            } elseif ($message.type -eq 'stop') {
                [System.Windows.Forms.Application]::Exit()
                return
            }
        }
        if ([MonitorInput]::Closed) {
            [System.Windows.Forms.Application]::Exit()
            return
        }
        $power = $null
        while ([MonitorInput]::PowerEvents.TryDequeue([ref]$power)) {
            if ($power -eq 'Suspend' -or $power -eq 'Resume') {
                Send-BridgeMessage @{ type = 'power'; mode = $power }
            }
        }
        if ([DateTime]::UtcNow -ge $script:nextProcesses) {
            Send-BridgeMessage (Get-WindowsAppTheme)
            $processes = @(Get-CimInstance Win32_Process -Filter "Name='copilot.exe' OR Name='github.exe'" |
                ForEach-Object {
                    @{
                        pid = [int]$_.ProcessId
                        parentPid = [int]$_.ParentProcessId
                        name = $_.Name.ToLowerInvariant()
                        startedAt = $_.CreationDate.ToUniversalTime().ToString('o')
                    }
                })
            Send-BridgeMessage @{ type = 'processes'; processes = $processes }
            $script:nextProcesses = [DateTime]::UtcNow.AddSeconds(2)
        }
        if ($script:queue.Count -gt 0 -and [DateTime]::UtcNow -ge $script:nextBalloon) {
            $script:current = $script:queue.Dequeue()
            $script:tray.BalloonTipTitle = $script:current.title
            $script:tray.BalloonTipText = $script:current.message
            $script:tray.BalloonTipIcon = if ($script:current.kind -eq 'error') {
                [System.Windows.Forms.ToolTipIcon]::Error
            } elseif ($script:current.kind -eq 'finished' -or $script:current.kind -eq 'test') {
                [System.Windows.Forms.ToolTipIcon]::Info
            } else {
                [System.Windows.Forms.ToolTipIcon]::Warning
            }
            $script:tray.ShowBalloonTip(8000)
            Send-BridgeMessage @{ type = 'notification-submitted'; id = $script:current.id }
            $script:nextBalloon = [DateTime]::UtcNow.AddSeconds(10)
        }
    } catch {
        Send-BridgeMessage @{ type = 'error'; message = $_.Exception.Message }
        [System.Windows.Forms.Application]::Exit()
    }
})

try {
    Send-BridgeMessage @{ type = 'ready' }
    $timer.Start()
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $timer.Dispose()
    $script:tray.Visible = $false
    $script:tray.Dispose()
    $menu.Dispose()
}
