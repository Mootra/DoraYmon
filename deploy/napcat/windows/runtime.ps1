param([ValidateSet('Check', 'Start', 'Stop', 'Status')][string]$Action = 'Check')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$taskName = 'DoraYmon-NapCat'
$healthPath = Join-Path $root 'runtime-health.json'
$profile = Get-Content -LiteralPath (Join-Path $root 'runtime-profile.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$backendService = [string]$profile.backendService
if (-not $backendService) { throw 'Backend service is not configured in runtime-profile.json.' }
$config = Get-Content -LiteralPath (Join-Path $root 'integration.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$python = $profile.windowsPython
$client = Join-Path $root 'account-client.py'

function Invoke-Backend([string]$Operation) {
    $process = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', 'systemctl', '--user', $Operation, $backendService) -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(20000)) { $process.Kill(); throw 'WSL backend service operation timed out.' }
    if ($process.ExitCode -ne 0) { throw ('WSL backend service ' + $Operation + ' failed.') }
}

function Read-Client([string]$Command) {
    $result = & $python $client $Command 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($result -join "`n") }
    return ($result -join "`n" | ConvertFrom-Json)
}

if ($Action -eq 'Stop') {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Stop-ScheduledTask -TaskName $taskName
    Invoke-Backend 'disable'
    Invoke-Backend 'stop'
    $node = Join-Path $root 'NapCat\node.exe'
    Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $node } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    $holderPath = Join-Path $root 'wsl-keepalive.pid'
    if (Test-Path -LiteralPath $holderPath) {
        $holderId = 0
        if ([int]::TryParse((Get-Content -LiteralPath $holderPath -Encoding UTF8 -Raw).Trim(), [ref]$holderId)) {
            Get-CimInstance Win32_Process -Filter ("ProcessId=" + $holderId) | Where-Object {
                $_.Name -eq 'wsl.exe' -and $_.CommandLine -like '*/usr/bin/sleep infinity*'
            } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        }
        Remove-Item -LiteralPath $holderPath
    }
    if (Test-Path -LiteralPath $healthPath) { Remove-Item -LiteralPath $healthPath }
    Write-Output 'Runtime stopped; automatic startup is disabled.'
    exit 0
}
if ($Action -eq 'Status') {
    Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State | Format-Table -AutoSize
    if (Test-Path -LiteralPath $healthPath) { Get-Content -LiteralPath $healthPath -Encoding UTF8 }
    Read-Client 'status' | ConvertTo-Json
    exit 0
}
if ($Action -eq 'Start') { Enable-ScheduledTask -TaskName $taskName | Out-Null }

$mutex = [System.Threading.Mutex]::new($false, 'Local\DoraYmonNapCatRuntime')
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
try {
    $previous = $null
    if (Test-Path -LiteralPath $healthPath) {
        $previous = Get-Content -LiteralPath $healthPath -Encoding UTF8 -Raw | ConvertFrom-Json
    }
    $health = [ordered]@{
        checkedAt = (Get-Date).ToString('o'); phase = 'starting'; online = $false
        backendService = $backendService
        oneBotAuthenticated = $false; backendActive = $false; failureStreak = 0
        lastRecoveryAt = if ($previous) { $previous.lastRecoveryAt } else { $null }
        error = ''; qqMessagesSentByProbe = 0
    }
    $stage = 'startup'
    try {
        & (Join-Path $root 'start-client.ps1') | Out-Null
        if ($Action -eq 'Start') { Invoke-Backend 'enable' }
        Invoke-Backend 'start'
        $health.backendActive = $true
        $stage = 'webui'
        $login = Read-Client 'status'
        # A quick login can briefly expose the QR phase while the core starts.
        for ($attempt = 0; $attempt -lt 5 -and (-not $login.isLogin -or -not $login.coreReady); $attempt++) {
            Start-Sleep -Seconds 2
            $login = Read-Client 'status'
        }
        if (-not $login.isLogin -or -not $login.coreReady) {
            $health.phase = if ($login.loginPhase) { $login.loginPhase } else { 'waiting_login' }
        } else {
            $stage = 'onebot'
            $probe = Read-Client 'probe'
            $health.online = [bool]$probe.online
            $health.oneBotAuthenticated = [bool]$probe.authenticated
            $health.phase = 'ready'
        }
        $stage = 'backend'
        Invoke-Backend 'is-active'
    } catch {
        $health.phase = $stage + '_error'
        if ($stage -eq 'backend') { $health.backendActive = $false }
        $health.error = [string]$_.Exception.Message
        foreach ($secret in @($config.webuiToken, $config.wsToken)) {
            if ($secret) { $health.error = $health.error.Replace($secret, '<redacted>') }
        }
    }
    $failed = $health.phase -in @('webui_error', 'onebot_error', 'offline', 'reconnecting')
    if ($failed) {
        $health.failureStreak = if ($previous -and $previous.phase -eq $health.phase) { [int]$previous.failureStreak + 1 } else { 1 }
        $recoveryDue = -not $health.lastRecoveryAt -or ((Get-Date) - [datetime]$health.lastRecoveryAt).TotalMinutes -ge 10
        if ($health.failureStreak -ge 3 -and $recoveryDue) {
            try {
                & (Join-Path $root 'start-client.ps1') -Restart | Out-Null
                Invoke-Backend 'restart'
                $health.lastRecoveryAt = (Get-Date).ToString('o')
                $health.phase = 'recovering'
                $health.online = $false
                $health.oneBotAuthenticated = $false
                $health.failureStreak = 0
            } catch {
                $health.error = 'Automatic recovery failed; inspect the runtime and service.'
            }
        }
    }
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $temporary = $healthPath + '.tmp'
    [System.IO.File]::WriteAllText($temporary, ($health | ConvertTo-Json -Depth 5) + "`n", $utf8)
    Move-Item -LiteralPath $temporary -Destination $healthPath -Force

    # Waiting for a QR scan never triggers a restart.
    if ($health.phase -ne 'ready' -and (-not $previous -or $previous.phase -ne $health.phase)) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $notice = [System.Windows.Forms.NotifyIcon]::new()
            $notice.Icon = [System.Drawing.SystemIcons]::Warning
            $notice.Visible = $true
            $text = if ($health.phase -match 'qrcode|login|confirm') { 'QQ login requires attention. Open NapCat to continue.' } else { 'QQ runtime is unavailable. Check runtime-health.json.' }
            $notice.ShowBalloonTip(5000, 'DoraYmon NapCat', $text, [System.Windows.Forms.ToolTipIcon]::Warning)
            Start-Sleep -Seconds 3
            $notice.Dispose()
        } catch { Write-Warning 'Desktop notification unavailable; health state was saved.' }
    }
    $health | ConvertTo-Json -Depth 5
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
