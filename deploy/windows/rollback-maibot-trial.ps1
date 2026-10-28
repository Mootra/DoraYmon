param([string]$RuntimeDirectory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'

$backupPath = (Get-Content -LiteralPath (Join-Path $RuntimeDirectory 'maibot-trial-backup-path.txt') -Encoding UTF8 -Raw).Trim()
$state = Get-Content -LiteralPath (Join-Path $backupPath 'state.json') -Encoding UTF8 -Raw | ConvertFrom-Json

function Invoke-Service([string[]]$Operations) {
    & wsl.exe -d Ubuntu -- systemctl --user @Operations
    if ($LASTEXITCODE -ne 0) { throw 'Service operation failed; keep the old backend stopped until the candidate is confirmed stopped.' }
}

$loadState = & wsl.exe -d Ubuntu -- systemctl --user show --property=LoadState --value $state.candidateService
if ($loadState -ne 'not-found') {
    Invoke-Service -Operations @('stop', $state.candidateService)
}
if ($state.oldServiceWasEnabled) {
    Invoke-Service -Operations @('enable', $state.oldService)
}
if ($state.oldServiceWasActive) {
    Invoke-Service -Operations @('start', $state.oldService)
}
if ($state.taskWasEnabled) {
    Enable-ScheduledTask -TaskName $state.taskName | Out-Null
}
Write-Output 'The old backend and its scheduled check have been restored to the saved state.'
