param([switch]$Experts,[switch]$SetupOnly)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot
try {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'This launcher supports x64 Windows 10/11.' }
    $taskSmi=Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $taskSmi) { throw 'Install an NVIDIA graphics driver first: https://www.nvidia.com/Download/index.aspx' }
    & $taskSmi.Source --query-gpu=name --format=csv,noheader
    if ($LASTEXITCODE -ne 0) { throw 'The NVIDIA driver is not working. Update it and restart Windows.' }
    $taskVc=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
    if (-not $taskVc -or $taskVc.Installed -ne 1) {
        Write-Host 'Installing the Microsoft Visual C++ x64 runtime...'
        New-Item -ItemType Directory -Force -Path '.leaflink/tools' | Out-Null
        $taskRedist=Join-Path $PSScriptRoot '.leaflink/tools/vc_redist.x64.exe'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $taskRedist
        $taskSignature=Get-AuthenticodeSignature -LiteralPath $taskRedist
        if ($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'The Microsoft runtime signature could not be verified.' }
        $taskInstall=Start-Process -FilePath $taskRedist -ArgumentList '/install /quiet /norestart' -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        if ($taskInstall.ExitCode -eq 3010) { throw 'Microsoft runtime installation needs a Windows restart. Restart and open this launcher again.' }
        if ($taskInstall.ExitCode -ne 0 -and $taskInstall.ExitCode -ne 1638) { throw "Microsoft runtime installation failed ($($taskInstall.ExitCode))." }
    }
    $taskNode=Get-Command node -ErrorAction SilentlyContinue
    $taskNodePath=$null
    if ($taskNode) {
        $taskMajor=& $taskNode.Source -p 'process.versions.node.split(".")[0]'
        if ($LASTEXITCODE -eq 0 -and [int]$taskMajor -ge 24) { $taskNodePath=$taskNode.Source }
    }
    if (-not $taskNodePath) {
        $taskTools=Join-Path $PSScriptRoot '.leaflink/tools'
        New-Item -ItemType Directory -Force -Path $taskTools | Out-Null
        $taskCached=Get-ChildItem -LiteralPath $taskTools -Directory -Filter 'node-v24*-win-x64' | Sort-Object Name -Descending | Select-Object -First 1
        if ($taskCached -and (Test-Path -LiteralPath (Join-Path $taskCached.FullName 'node.exe'))) { $taskNodePath=Join-Path $taskCached.FullName 'node.exe' }
        else {
            Write-Host 'Installing project-local Node.js 24...'
            $taskSums=(Invoke-WebRequest -UseBasicParsing -Uri 'https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt').Content
            $taskMatch=[regex]::Match($taskSums,'(?m)^([a-f0-9]{64})\s+(node-(v24\.[0-9]+\.[0-9]+)-win-x64\.zip)\s*$')
            if (-not $taskMatch.Success) { throw 'Could not resolve the official Node 24 checksum.' }
            $taskFilename=$taskMatch.Groups[2].Value
            $taskArchive=Join-Path $taskTools $taskFilename
            Invoke-WebRequest -UseBasicParsing -Uri ('https://nodejs.org/dist/'+$taskMatch.Groups[3].Value+'/'+$taskFilename) -OutFile $taskArchive
            if ((Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskMatch.Groups[1].Value) { throw 'Node download checksum mismatch. Run the launcher again.' }
            Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskTools -Force
            $taskNodePath=Join-Path (Join-Path $taskTools ($taskFilename -replace '\.zip$','')) 'node.exe'
        }
    }
    $env:PATH=(Split-Path -Parent $taskNodePath)+';'+$env:PATH
    $taskArgs=@('setup.js')
    if (-not $SetupOnly) { $taskArgs+='--start' }
    if ($Experts) { $taskArgs+='--experts' }
    & $taskNodePath @taskArgs
    exit $LASTEXITCODE
} catch {
    Write-Host ('LeafLink could not start: '+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
