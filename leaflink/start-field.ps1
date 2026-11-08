param([switch]$Experts)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 24 or newer, then run again.' }
if ([int](& node -p 'process.versions.node.split(".")[0]') -lt 24) { throw 'Node.js 24 or newer is required.' }
if (-not (Test-Path -LiteralPath 'node_modules/express')) {
    & npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' }
}
& npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'App build failed.' }
$configureArgs = @('server/configure-field.js','--show-token')
if ($Experts) { $configureArgs += '--experts' }
& node @configureArgs
if ($LASTEXITCODE -ne 0) { throw 'Could not create field configuration.' }
Write-Host 'Model inference requires PyTorch, torchvision and the configured model weights.'
$configFile = if ($Experts) { '.env.experts' } else { '.env.field' }
& node "--env-file=$configFile" server/index.js
