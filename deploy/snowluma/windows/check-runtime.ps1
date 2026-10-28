$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'runtime.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors.Message -join '; ') }
$selection = $tree.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Read-Processes'}, $true)
Invoke-Expression $selection.Extent.Text

function Invoke-RestMethod($Uri, $Headers, $TimeoutSec) {
    return @{list = $script:fixture}
}

$config = @{webui = 'http://127.0.0.1:5099'; account = '10001'}
$cases = @(
    @{Name = 'unloaded bot'; Process = @{pid = 101; uin = '0'; account = @{uin = '10001'}}; Expected = 1},
    @{Name = 'unloaded other account'; Process = @{pid = 102; uin = '0'; account = @{uin = '10002'}}; Expected = 0},
    @{Name = 'online bot'; Process = @{pid = 103; uin = '10001'; account = @{uin = '10001'}}; Expected = 1},
    @{Name = 'stale local identity'; Process = @{pid = 104; uin = '10002'; account = @{uin = '10001'}}; Expected = 0},
    @{Name = 'unknown login'; Process = @{pid = 105; uin = '0'; account = $null}; Expected = 0}
)
foreach ($case in $cases) {
    $script:fixture = @([pscustomobject]$case.Process)
    $matched = @(Read-Processes @{})
    if ($matched.Count -ne $case.Expected) { throw ('Account isolation failed: ' + $case.Name) }
}
Write-Output 'Five account selection cases passed; no network or QQ messages were used.'

$selection = $tree.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Disable-NativeStatusCommand'}, $true)
Invoke-Expression $selection.Extent.Text
$cases = @(
    @{Name = 'default status'; Config = '{}'; Changed = $true},
    @{Name = 'enabled status'; Config = '{"statusCommand":{"enabled":true,"trigger":"#custom","swallow":true,"cooldownSeconds":10},"networks":{"wsServers":[{"host":"127.0.0.1"}]}}'; Changed = $true},
    @{Name = 'missing enabled'; Config = '{"statusCommand":{"trigger":"#custom"}}'; Changed = $true},
    @{Name = 'already protected'; Config = '{"statusCommand":{"enabled":false,"trigger":"#sl"}}'; Changed = $false}
)
foreach ($case in $cases) {
    $fixture = $case.Config | ConvertFrom-Json
    $before = $case.Config | ConvertFrom-Json
    $changed = Disable-NativeStatusCommand $fixture
    if ($changed -ne $case.Changed -or $fixture.statusCommand.enabled -ne $false) { throw ('Status command access failed: ' + $case.Name) }
    if ($before.statusCommand) {
        foreach ($name in @('trigger', 'swallow', 'cooldownSeconds')) {
            if ($before.statusCommand.$name -ne $fixture.statusCommand.$name) { throw ('Unrelated status setting changed: ' + $case.Name) }
        }
        if (($before.networks | ConvertTo-Json -Depth 20 -Compress) -ne ($fixture.networks | ConvertTo-Json -Depth 20 -Compress)) { throw ('Network configuration changed: ' + $case.Name) }
    }
    if (Disable-NativeStatusCommand $fixture) { throw ('Status command policy is not idempotent: ' + $case.Name) }
}
Write-Output 'Four status command policy cases passed; unrelated settings were preserved.'

$selection = $tree.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Save-AccountConfig'}, $true)
Invoke-Expression $selection.Extent.Text
$script:managementRequests = @()
$script:resolved = '{"networks":{"wsServers":[{"name":"synthetic","host":"127.0.0.1"}]},"statusCommand":{"enabled":true,"swallow":false,"cooldownSeconds":5,"trigger":"#sl"},"historySync":{"enabled":true},"notifications":{"channelIds":[]}}' | ConvertFrom-Json
$nextConfig = '{"networks":{"wsServers":[{"name":"synthetic","host":"127.0.0.2"}]}}' | ConvertFrom-Json
function Invoke-RestMethod($Uri, $Headers, $TimeoutSec, $Method, $ContentType, $Body) {
    $script:managementRequests += @{Uri = $Uri; Method = $Method; Body = $Body}
    if ($Method -eq 'Post') { return @{saved = $true; applied = $true} }
    return @{config = $script:resolved}
}
$result = Save-AccountConfig @{} $nextConfig
if (-not $result.applied -or $script:managementRequests.Count -ne 2) { throw 'Resolved account configuration was not applied.' }
if ($script:managementRequests[0].Uri -ne $script:managementRequests[1].Uri -or $script:managementRequests[1].Uri -notlike '*/10001') { throw 'Account configuration target changed.' }
if ($script:managementRequests[1].Body -ne ($script:resolved | ConvertTo-Json -Depth 20 -Compress)) { throw 'Resolved account configuration was not preserved.' }
if ($script:resolved.statusCommand.enabled -ne $false -or $script:resolved.networks.wsServers[0].host -ne '127.0.0.2' -or $script:resolved.historySync.enabled -ne $true) { throw 'Account config access policy or resolved defaults changed.' }
Write-Output 'Management config read/save contract passed with synthetic resolved defaults.'

$selection = $tree.Find({param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$servers'}, $true)
$cases = @(
    @{Server = '{"enabled":true}'; Expected = 1},
    @{Server = '{}'; Expected = 1},
    @{Server = '{"enabled":false}'; Expected = 0}
)
foreach ($case in $cases) {
    $onebot = [pscustomobject]@{networks = [pscustomobject]@{wsServers = @(($case.Server | ConvertFrom-Json))}}
    Invoke-Expression $selection.Extent.Text
    if ($servers.Count -ne $case.Expected) { throw 'OneBot server enabled default was not respected.' }
}
Write-Output 'Three server enabled/default cases passed.'
