param([switch]$Open, [switch]$Restart)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$configPath = Join-Path $root 'integration.json'
$config = Get-Content -LiteralPath $configPath -Encoding UTF8 -Raw | ConvertFrom-Json
$profile = Get-Content -LiteralPath (Join-Path $root 'runtime-profile.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$runtime = Join-Path $root 'NapCat'
$node = Join-Path $runtime 'node.exe'

# Starting the configured distro also creates its current virtual network.
$boot = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', '/bin/true') -WindowStyle Hidden -PassThru
if (-not $boot.WaitForExit(20000)) { $boot.Kill(); throw 'WSL startup timed out.' }
if ($boot.ExitCode -ne 0) { throw 'WSL startup failed.' }
$utf8 = [System.Text.UTF8Encoding]::new($false)
# systemd services alone do not keep a WSL instance alive.
$holderPath = Join-Path $root 'wsl-keepalive.pid'
$holder = $null
if (Test-Path -LiteralPath $holderPath) {
    $holderId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $holderPath -Encoding UTF8 -Raw).Trim(), [ref]$holderId)) {
        $holder = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $holderId) | Where-Object {
            $_.Name -eq 'wsl.exe' -and $_.CommandLine -like '*/usr/bin/sleep infinity*'
        }
    }
}
if (-not $holder) {
    $holder = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', '/usr/bin/sleep', 'infinity') -WindowStyle Hidden -PassThru
    [System.IO.File]::WriteAllText($holderPath, [string]$holder.Id, $utf8)
}
$address = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -like '*WSL*' -and $_.AddressState -eq 'Preferred'
} | Select-Object -First 1 -ExpandProperty IPAddress
if (-not $address) { throw 'WSL IPv4 network is unavailable.' }

$onebotPath = Join-Path $runtime ('napcat\config\onebot11_' + $config.account + '.json')
$onebot = Get-Content -LiteralPath $onebotPath -Encoding UTF8 -Raw | ConvertFrom-Json
$server = @($onebot.network.websocketServers | Where-Object { $_.enable })
if ($server.Count -ne 1) { throw 'Expected one enabled OneBot WebSocket server.' }
if (-not $server[0].token -or $server[0].token -ne $config.wsToken) { throw 'OneBot authentication configuration does not match.' }
$networkChanged = $server[0].host -ne $address
$url = 'ws://' + $address + ':' + $server[0].port
if ($networkChanged) {
    $server[0].host = $address
    [System.IO.File]::WriteAllText($onebotPath, ($onebot | ConvertTo-Json -Depth 30) + "`n", $utf8)
}
if ($config.wsUrl -ne $url) {
    $config.wsUrl = $url
    [System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 20) + "`n", $utf8)
}
$running = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $node })
if (($Restart -or $networkChanged) -and $running.Count) {
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $running = @()
}
if (-not $running.Count) {
    $client = Start-Process -FilePath $node -ArgumentList @('index.js', '-q', $config.account) -WorkingDirectory $runtime -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'client.stdout.log') -RedirectStandardError (Join-Path $root 'client.stderr.log') -PassThru
    [System.IO.File]::WriteAllText((Join-Path $root 'client.pid'), [string]$client.Id, $utf8)
    Write-Output ('NapCat started. PID: ' + $client.Id)
} else {
    Write-Output 'NapCat is already running.'
}
if ($networkChanged) {
    # The manual launcher must also refresh an already-running backend's URL.
    $reload = Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $profile.distro, '--exec', 'systemctl', '--user', 'try-restart', 'doraymon-qq.service') -WindowStyle Hidden -PassThru
    if (-not $reload.WaitForExit(20000)) { $reload.Kill(); throw 'Backend network reload timed out.' }
    if ($reload.ExitCode -ne 0) { throw 'Backend network reload failed.' }
}
if ($Open) { Start-Process ($config.webui + '/webui?token=' + $config.webuiToken) }
