param([ValidateSet('Start', 'Check', 'Status', 'Stop')][string]$Action = 'Check')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$config = Get-Content -LiteralPath (Join-Path $root 'integration.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$profile = Get-Content -LiteralPath (Join-Path $root 'runtime-profile.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$appRoot = Join-Path $root 'app'
$node = Join-Path $appRoot 'node.exe'
$taskName = 'DoraYmon-SnowLuma'
$utf8 = [System.Text.UTF8Encoding]::new($false)

function Invoke-Backend([string]$Operation) {
    $job = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', 'systemctl', '--user', $Operation, $profile.backendService) -WindowStyle Hidden -PassThru
    if (-not $job.WaitForExit(30000)) { $job.Kill(); throw 'MaiBot service operation timed out.' }
    if ($job.ExitCode -ne 0) { throw 'MaiBot service operation failed.' }
}

function Connect-Panel {
    $credential = Import-Clixml -LiteralPath (Join-Path $root 'webui-credential.xml')
    $login = Invoke-RestMethod -Uri ($config.webui + '/api/login') -Method Post -ContentType 'application/json' -Body (@{password = $credential.GetNetworkCredential().Password} | ConvertTo-Json -Compress) -TimeoutSec 10
    if (-not $login.success -or $login.mustChangePassword) { throw 'SnowLuma management login failed.' }
    return @{Authorization = 'Bearer ' + $login.token}
}

function Disable-NativeStatusCommand($OneBot) {
    # MaiBot handles #sl with the original sender's administrator identity.
    if (-not $OneBot.statusCommand) {
        $OneBot | Add-Member -NotePropertyName statusCommand -NotePropertyValue ([pscustomobject]@{enabled = $false; swallow = $false; cooldownSeconds = 5; trigger = '#sl'}) -Force
        return $true
    }
    if ($OneBot.statusCommand.enabled -eq $false) { return $false }
    $OneBot.statusCommand | Add-Member -NotePropertyName enabled -NotePropertyValue $false -Force
    return $true
}

function Save-AccountConfig($Headers, $OneBot) {
    # The management API requires resolved defaults absent from older on-disk files.
    $uri = $config.webui + '/api/config/' + $config.account
    $resolved = Invoke-RestMethod -Uri $uri -Headers $Headers -TimeoutSec 15
    $resolved.config.networks = $OneBot.networks
    $resolved.config.statusCommand.enabled = $false
    $applied = Invoke-RestMethod -Uri $uri -Method Post -Headers $Headers -ContentType 'application/json' -Body ($resolved.config | ConvertTo-Json -Depth 20 -Compress) -TimeoutSec 15
    if (-not $applied.saved) { throw 'SnowLuma did not save the account configuration.' }
    return $applied
}

function Read-Processes($Headers, [switch]$All) {
    $result = Invoke-RestMethod -Uri ($config.webui + '/api/processes') -Headers $Headers -TimeoutSec 15
    if ($All) { return @($result.list) }
    # Before injection uin is 0; account.uin identifies QQ's local login.
    return @($result.list | Where-Object {
        $identity = if ($_.uin -eq '0') { $_.account.uin } else { $_.uin }
        [string]$identity -eq [string]$config.account
    })
}

function Start-LocalClient {
    $env:SNOWLUMA_HOOK_AUTOLOAD = '0'
    $env:SNOWLUMA_TELEMETRY = '0'
    $env:SNOWLUMA_ACCEPT_EULA = '1'
    $env:SNOWLUMA_ACCEPT_PRIVACY = '1'
    $client = Start-Process -FilePath $node -ArgumentList @('index.mjs') -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'client.stdout.log') -RedirectStandardError (Join-Path $root 'client.stderr.log') -PassThru
    [System.IO.File]::WriteAllText((Join-Path $root 'client.pid'), [string]$client.Id, $utf8)
    Start-Sleep -Seconds 3
}

function Write-Json([string]$Path, $Value) {
    $temporary = $Path + '.tmp'
    [System.IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 20) + "`n", $utf8)
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

if ($Action -eq 'Status') {
    Get-Content -LiteralPath (Join-Path $root 'runtime-health.json') -Encoding UTF8
    exit 0
}
if ($Action -eq 'Stop') {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Stop-ScheduledTask -TaskName $taskName
    Invoke-Backend 'disable'
    Invoke-Backend 'stop'
    $headers = Connect-Panel
    foreach ($target in @(Read-Processes $headers)) {
        Invoke-RestMethod -Uri ($config.webui + '/api/processes/' + $target.pid + '/unload') -Headers $headers -Method Post -TimeoutSec 15 | Out-Null
    }
    Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $node } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    $holderPath = Join-Path $root 'wsl-keepalive.pid'
    if (Test-Path -LiteralPath $holderPath) {
        $holderId = [int](Get-Content -LiteralPath $holderPath -Encoding UTF8 -Raw)
        Get-CimInstance Win32_Process -Filter ('ProcessId=' + $holderId) | Where-Object { $_.Name -eq 'wsl.exe' -and $_.CommandLine -like '*/usr/bin/sleep infinity*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Remove-Item -LiteralPath $holderPath
    }
    Write-Json (Join-Path $root 'runtime-health.json') ([ordered]@{checkedAt = (Get-Date).ToString('o'); provider = 'SnowLuma'; phase = 'stopped'; online = $false; oneBotAuthenticated = $false; backendService = $profile.backendService; backendActive = $false; qqMessagesSentByProbe = 0; error = ''})
    Write-Output 'SnowLuma and MaiBot stopped; automatic startup disabled.'
    exit 0
}
if ($Action -eq 'Start') { Enable-ScheduledTask -TaskName $taskName | Out-Null }

$mutex = [System.Threading.Mutex]::new($false, 'Local\DoraYmonSnowLumaRuntime')
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
try {
    $health = [ordered]@{checkedAt = (Get-Date).ToString('o'); provider = 'SnowLuma'; phase = 'starting'; online = $false; oneBotAuthenticated = $false; selfIdMatches = $false; backendService = $profile.backendService; backendActive = $false; targetProcessCount = 0; qqMessagesSentByProbe = 0; error = ''}
    try {
        $boot = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', '/bin/true') -WindowStyle Hidden -PassThru
        if (-not $boot.WaitForExit(20000)) { $boot.Kill(); throw 'WSL startup timed out.' }
        if ($boot.ExitCode -ne 0) { throw 'WSL startup failed.' }

        $holderPath = Join-Path $root 'wsl-keepalive.pid'
        $holder = $null
        if (Test-Path -LiteralPath $holderPath) {
            $holderId = [int](Get-Content -LiteralPath $holderPath -Encoding UTF8 -Raw)
            $holder = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $holderId) | Where-Object { $_.Name -eq 'wsl.exe' -and $_.CommandLine -like '*/usr/bin/sleep infinity*' }
        }
        if (-not $holder) {
            $holder = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', '/usr/bin/sleep', 'infinity') -WindowStyle Hidden -PassThru
            [System.IO.File]::WriteAllText($holderPath, [string]$holder.Id, $utf8)
        }

        $address = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like '*WSL*' -and $_.AddressState -eq 'Preferred' } | Select-Object -First 1 -ExpandProperty IPAddress
        if (-not $address) { throw 'WSL IPv4 interface is unavailable.' }
        $onebotPath = Join-Path $appRoot ('config\onebot_' + $config.account + '.json')
        $onebot = Get-Content -LiteralPath $onebotPath -Encoding UTF8 -Raw | ConvertFrom-Json
        $statusCommandChanged = Disable-NativeStatusCommand $onebot
        $servers = @($onebot.networks.wsServers | Where-Object { $_.enabled -ne $false })
        if ($servers.Count -ne 1 -or $servers[0].accessToken -ne $config.wsToken) { throw 'OneBot account configuration does not match.' }
        $networkChanged = $servers[0].host -ne $address
        if ($networkChanged) {
            $servers[0].host = $address
        }
        $endpoint = 'ws://' + $address + ':' + $servers[0].port
        if ($config.wsUrl -ne $endpoint) {
            $config.wsUrl = $endpoint
            Write-Json (Join-Path $root 'integration.json') $config
            $networkChanged = $true
        }

        $running = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $node })
        if (-not $running.Count) {
            Start-LocalClient
        }

        $headers = Connect-Panel
        if ($networkChanged -or $statusCommandChanged) {
            Save-AccountConfig $headers $onebot | Out-Null
            if ($networkChanged) { Invoke-Backend 'stop' }
        }
        $targets = @(Read-Processes $headers)
        if (-not $targets.Count -and $Action -eq 'Start') {
            $discovered = @(Read-Processes $headers -All)
            if ($discovered.Count -gt 0 -and @($discovered | Where-Object { $_.status -ne 'available' }).Count -eq 0) {
                Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $node } | ForEach-Object { Stop-Process -Id $_.ProcessId }
                Start-LocalClient
                $headers = Connect-Panel
                $targets = @(Read-Processes $headers)
            }
        }
        $health.targetProcessCount = $targets.Count
        if ($targets.Count -gt 1) { throw 'Multiple QQ processes match the bot account; resolve the duplicate before starting.' }
        if (-not $targets.Count) {
            Invoke-Backend 'stop'
            $health.phase = 'waiting_bot_login'
        } else {
            $target = $targets[0]
            if ($target.status -in @('available', 'disconnected', 'error')) {
                $loaded = Invoke-RestMethod -Uri ($config.webui + '/api/processes/' + $target.pid + '/load') -Method Post -Headers $headers -TimeoutSec 20
                if (-not $loaded.success) { throw 'SnowLuma could not attach to the bot QQ process.' }
                Start-Sleep -Seconds 2
            }
            $probeText = & $profile.windowsPython (Join-Path $root 'onebot-probe.py') --root $root
            if ($LASTEXITCODE -ne 0) { throw 'OneBot account probe failed.' }
            $probe = $probeText | ConvertFrom-Json
            $health.oneBotAuthenticated = [bool]$probe.authenticated
            $health.selfIdMatches = [bool]$probe.selfIdMatches
            $health.online = [bool]$probe.online
            if (-not $health.online -or -not $health.selfIdMatches) { throw 'The configured bot account is not online.' }
            Invoke-Backend 'enable'
            Invoke-Backend 'start'
            Invoke-Backend 'is-active'
            $health.backendActive = $true
            $health.phase = 'ready'
        }
    } catch {
        $health.phase = 'error'
        $health.backendActive = $null
        $health.error = [string]$_.Exception.Message
        if ($config.wsToken) { $health.error = $health.error.Replace($config.wsToken, '<redacted>') }
    }
    Write-Json (Join-Path $root 'runtime-health.json') $health
    $health | ConvertTo-Json -Depth 5
    if ($health.phase -eq 'error') { exit 1 }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
