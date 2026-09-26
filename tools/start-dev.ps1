param([string]$DockerConfig)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath 'node_modules\vite\bin\vite.js')) {
    throw 'Install dependencies first; see docs/local-start.md.'
}
if (!(Test-Path -LiteralPath '.env')) { throw 'Run node tools/init-dev.mjs first.' }

function Wait-Http([string]$Url) {
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(2)
    try {
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            try {
                $response = $client.GetAsync($Url).GetAwaiter().GetResult()
                $ok = $response.IsSuccessStatusCode
                $response.Dispose()
                if ($ok) { return }
            } catch { }
            Start-Sleep -Milliseconds 500
        }
        throw "Service did not become ready: $Url. Check .cache/dev-*.stderr.log."
    } finally {
        $client.Dispose()
    }
}

# Never terminate a process merely because it occupies our development port.
foreach ($port in @(8000, 5173)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is occupied. Stop the existing development server before restarting."
    }
}
$dockerArguments = @()
if ($DockerConfig) { $dockerArguments += @('--config', $DockerConfig) }
$dockerArguments += @('compose', 'up', '-d', '--wait', '--wait-timeout', '60')
& docker @dockerArguments
if ($LASTEXITCODE -ne 0) { throw 'Docker dependencies failed to start.' }
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed; application was not started.' }

New-Item -ItemType Directory -Path '.cache' -Force | Out-Null
$started = @()
try {
    $started += Start-Process -FilePath $python -ArgumentList '-m', 'lab_manager' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput "$projectRoot\.cache\dev-api.stdout.log" -RedirectStandardError "$projectRoot\.cache\dev-api.stderr.log" -PassThru
    Wait-Http 'http://127.0.0.1:8000/api/health/ready'
    $started += Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList '../../node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--strictPort' -WorkingDirectory "$projectRoot\apps\web" -WindowStyle Hidden -RedirectStandardOutput "$projectRoot\.cache\dev-web.stdout.log" -RedirectStandardError "$projectRoot\.cache\dev-web.stderr.log" -PassThru
    Wait-Http 'http://localhost:5173/api/health/ready'
    Write-Host 'Lab Manager is ready: http://localhost:5173/'
} catch {
    foreach ($process in $started) {
        if (!$process.HasExited) { $process.Kill() }
    }
    throw
}
