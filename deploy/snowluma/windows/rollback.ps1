$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$profile = Get-Content -LiteralPath (Join-Path $root 'runtime-profile.json') -Encoding UTF8 -Raw | ConvertFrom-Json
Disable-ScheduledTask -TaskName 'DoraYmon-SnowLuma' | Out-Null
Stop-ScheduledTask -TaskName 'DoraYmon-SnowLuma'
& (Join-Path $root 'runtime.ps1') -Action Stop
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'SnowLuma did not stop; rollback was not started.' }
foreach ($arguments in @(
    @('/usr/bin/cp', $profile.backendUnitBackup, $profile.backendUnitPath),
    @('/usr/bin/cp', $profile.adapterConfigBackup, $profile.adapterConfigPath),
    @('systemctl', '--user', 'daemon-reload')
)) {
    $job = Start-Process -FilePath 'wsl.exe' -ArgumentList (@('-d', $profile.distro, '--exec') + $arguments) -WindowStyle Hidden -PassThru
    if (-not $job.WaitForExit(30000)) { $job.Kill(); throw 'Rollback operation timed out.' }
    if ($job.ExitCode -ne 0) { throw 'Rollback operation failed.' }
}
$backup = (Get-Content -LiteralPath (Join-Path $root 'backup-path.txt') -Encoding UTF8 -Raw).Trim()
Copy-Item -LiteralPath (Join-Path $backup 'start.vbs') -Destination $profile.launcherPath -Force
& (Join-Path $profile.previousRuntime 'runtime.ps1') -Action Start
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'NapCat startup failed after restoring its configuration.' }
Write-Output 'NapCat startup restored. Check its login status before relying on message delivery.'
