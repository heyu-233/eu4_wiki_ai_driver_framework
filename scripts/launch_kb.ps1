$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$HostName = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$Port = if ($env:PORT) { [int]$env:PORT } else { 8765 }
$Url = "http://${HostName}:${Port}/"
$LogDir = Join-Path $Root "logs"
$OutLog = Join-Path $LogDir "kb_server.out.log"
$ErrLog = Join-Path $LogDir "kb_server.err.log"
$LauncherLog = Join-Path $LogDir "launcher.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $env:LLM_USE_PROXY) {
    Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:ALL_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
    Remove-Item Env:https_proxy -ErrorAction SilentlyContinue
    Remove-Item Env:all_proxy -ErrorAction SilentlyContinue
}

function Test-Server {
    try {
        $response = Invoke-WebRequest -Uri "${Url}api/stats" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-PythonExe {
    $candidates = @(
        (Join-Path $Root ".venv\Scripts\python.exe"),
        "D:\miniconda3\python.exe",
        "C:\Users\21101\AppData\Local\Programs\Python\Python312\python.exe",
        "C:\Users\21101\AppData\Local\Programs\Python\Python311\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

function Stop-StaleKbListener {
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction Stop
            $cmd = [string]$proc.CommandLine
            $cwd = [string]$proc.ExecutablePath
            if ($cmd -like "*app\server.py*" -or $cmd -like "*app/server.py*" -or $cmd -like "*eu41.37*") {
                Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
                Add-Content -Path $LauncherLog -Value "$(Get-Date -Format s) Stopped stale KB listener PID $($listener.OwningProcess): $cmd"
            } else {
                Add-Content -Path $LauncherLog -Value "$(Get-Date -Format s) Port $Port is used by another process PID $($listener.OwningProcess): $cmd"
            }
        } catch {
            Add-Content -Path $LauncherLog -Value "$(Get-Date -Format s) Could not inspect listener PID $($listener.OwningProcess): $_"
        }
    }
}

function Start-KbServer {
    $python = Get-PythonExe
    if (-not $python) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("Python was not found. Please install Python or add it to PATH.", "EU4 Wiki KB")
        exit 1
    }

    Add-Content -Path $LauncherLog -Value "$(Get-Date -Format s) Starting KB with $python"
    Start-Process `
        -FilePath $python `
        -ArgumentList @("-u", (Join-Path $Root "app\server.py")) `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog
}

if (-not (Test-Server)) {
    Stop-StaleKbListener
    Start-Sleep -Milliseconds 300
    Start-KbServer
}

for ($i = 0; $i -lt 30; $i++) {
    if (Test-Server) {
        Start-Process $Url
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

$message = "EU4 Wiki KB did not respond at $Url. Check logs\kb_server.err.log."
Add-Content -Path $LauncherLog -Value "$(Get-Date -Format s) $message"
Start-Process $Url
