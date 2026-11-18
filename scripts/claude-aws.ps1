# Use the existing claude-aws function without copying its credentials.
$ErrorActionPreference = 'Stop'
$script:agentBusClaudeArguments = @($args)
$agentBusUtf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $agentBusUtf8
[Console]::OutputEncoding = $agentBusUtf8
$OutputEncoding = $agentBusUtf8

$agentBusProfile = Join-Path $env:USERPROFILE 'Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1'
$agentBusTokens = $null
$agentBusParseErrors = $null
$agentBusAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $agentBusProfile, [ref]$agentBusTokens, [ref]$agentBusParseErrors)
if ($agentBusParseErrors.Count) {
    throw 'Cannot parse the PowerShell profile containing claude-aws.'
}
$agentBusFunction = $agentBusAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'claude-aws'
}, $true) | Select-Object -First 1
if (-not $agentBusFunction) {
    throw 'The claude-aws function was not found in the Windows PowerShell profile.'
}

$script:agentBusClaudeExe = Join-Path $env:APPDATA 'npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe'
if (-not (Test-Path -LiteralPath $script:agentBusClaudeExe -PathType Leaf)) {
    throw 'The native Claude Code executable was not found.'
}

# AgentBus sends prompts through stdin. Forward them through the profile function.
$script:agentBusHasInput = [Console]::IsInputRedirected
$script:agentBusInput = if ($script:agentBusHasInput) { [Console]::In.ReadToEnd() } else { $null }
. ([scriptblock]::Create($agentBusFunction.Extent.Text))
function claude {
    if ($script:agentBusHasInput) {
        $script:agentBusInput | & $script:agentBusClaudeExe @args
    } else {
        & $script:agentBusClaudeExe @args
    }
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Remove-Item Env:CLAUDECODE -ErrorAction SilentlyContinue
claude-aws @script:agentBusClaudeArguments
exit $LASTEXITCODE
