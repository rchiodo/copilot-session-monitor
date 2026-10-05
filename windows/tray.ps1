param([string]$MonitorUrl, [switch]$WatcherOnly, [switch]$CollectorOnly)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic
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

function Show-ConnectionDialog {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Connect to host'
    $form.Width = 480
    $form.Height = 260
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $label = New-Object System.Windows.Forms.Label
    $label.Text = 'Paste the connection string copied from the host machine:'
    $label.SetBounds(12, 10, 440, 20)
    $textBox = New-Object System.Windows.Forms.TextBox
    $textBox.Multiline = $true
    $textBox.ScrollBars = 'Vertical'
    $textBox.SetBounds(12, 35, 440, 130)
    $okButton = New-Object System.Windows.Forms.Button
    $okButton.Text = 'OK'
    $okButton.SetBounds(290, 175, 75, 28)
    $okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $cancelButton = New-Object System.Windows.Forms.Button
    $cancelButton.Text = 'Cancel'
    $cancelButton.SetBounds(375, 175, 75, 28)
    $cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.AddRange(@($label, $textBox, $okButton, $cancelButton))
    $form.AcceptButton = $okButton
    $form.CancelButton = $cancelButton
    $result = $form.ShowDialog()
    $form.Dispose()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $textBox.Text.Trim().Length -gt 0) {
        return $textBox.Text.Trim()
    }
    return $null
}

$script:tray = New-Object System.Windows.Forms.NotifyIcon
$script:tray.Icon = [System.Drawing.SystemIcons]::Information
$script:tray.Text = if ($WatcherOnly) { 'Copilot session monitor - watcher' } else { 'Copilot session monitor - collector' }
$script:tray.Visible = $true
$menu = New-Object System.Windows.Forms.ContextMenuStrip
if (-not $WatcherOnly) {
    $openItem = $menu.Items.Add('Open observed sessions')
    $testItem = $menu.Items.Add('Test notification')
    $generateItem = $menu.Items.Add('Generate connection request for a sub machine...')
    [void]$menu.Items.Add('-')
    $exitItem = $menu.Items.Add('Stop collector')
    $openItem.add_Click({ Start-Process $MonitorUrl })
    $testItem.add_Click({ Send-BridgeMessage @{ type = 'test' } })
    $generateItem.add_Click({
        try {
            $label = [Microsoft.VisualBasic.Interaction]::InputBox('Optional label for this sub machine (leave blank for a default name):', 'Generate connection request', '')
            Send-BridgeMessage @{ type = 'generate-connection'; label = $label }
        } catch {
            [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Connection request failed', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
        }
    })
    $exitItem.add_Click({ Send-BridgeMessage @{ type = 'stop' } })
} else {
    $connectItem = $menu.Items.Add('Connect to host...')
    [void]$menu.Items.Add('-')
    $exitItem = $menu.Items.Add('Stop watcher')
    $connectItem.add_Click({
        $value = Show-ConnectionDialog
        if ($null -ne $value) { Send-BridgeMessage @{ type = 'connect'; value = $value } }
    })
    $exitItem.add_Click({ Send-BridgeMessage @{ type = 'stop' } })
}
$script:tray.ContextMenuStrip = $menu
$script:queue = New-Object 'System.Collections.Generic.Queue[object]'
$script:current = $null
$script:nextBalloon = [DateTime]::MinValue
$script:nextProcesses = [DateTime]::MinValue
[MonitorInput]::Start()
$script:tray.add_DoubleClick({ if ($MonitorUrl) { Start-Process $MonitorUrl } })
$script:tray.add_BalloonTipShown({
    if ($null -ne $script:current) {
        Send-BridgeMessage @{ type = 'notification-shown'; id = $script:current.id }
    }
})
$script:tray.add_BalloonTipClicked({ if ($MonitorUrl) { Start-Process $MonitorUrl } })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.add_Tick({
    try {
        $line = $null
        while ([MonitorInput]::Lines.TryDequeue([ref]$line)) {
            $message = $line | ConvertFrom-Json
            if ($message.type -eq 'notify' -and -not $WatcherOnly) {
                $script:queue.Enqueue($message)
            } elseif ($message.type -eq 'connection-string') {
                [System.Windows.Forms.Clipboard]::SetText($message.value)
                $script:tray.BalloonTipTitle = 'Connection string copied'
                $script:tray.BalloonTipText = 'Valid for pairing one machine. Paste it on the other PC using "Connect to host...".'
                $script:tray.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
                $script:tray.ShowBalloonTip(8000)
            } elseif ($message.type -eq 'connect-result') {
                if ($message.ok) {
                    $script:tray.BalloonTipTitle = 'Paired'
                    $script:tray.BalloonTipText = "Connected to $($message.host) as $($message.label)"
                    $script:tray.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
                    $script:tray.ShowBalloonTip(8000)
                } else {
                    [System.Windows.Forms.MessageBox]::Show($message.message, 'Connection failed', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
                }
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
            if (-not $WatcherOnly) { Send-BridgeMessage (Get-WindowsAppTheme) }
            if (-not $CollectorOnly) {
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
            }
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
