# CreditLens desktop launcher.
# Starts the Flask server hidden (if not already running) and opens the
# system in a dedicated Edge app window (no browser chrome).

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$url = 'http://127.0.0.1:5000'

function Test-Server {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "$url/healthz" -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch { return $false }
}

if (-not (Test-Server)) {
    Start-Process -FilePath $python -ArgumentList 'main.py' -WorkingDirectory $root -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(45)
    while (-not (Test-Server) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
}

$edge = "$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }

if (Test-Path $edge) {
    Start-Process -FilePath $edge -ArgumentList "--app=$url/", '--window-size=1480,940'
} else {
    Start-Process $url  # Fallback: default browser
}
