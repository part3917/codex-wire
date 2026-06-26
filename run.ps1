param(
    [switch]$Background,
    [switch]$Foreground,
    [string]$HostName = $(if ($env:CODEX_MONITOR_HOST) { $env:CODEX_MONITOR_HOST } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:CODEX_MONITOR_PORT) { [int]$env:CODEX_MONITOR_PORT } else { 8787 })
)

$ErrorActionPreference = "Stop"

function Get-ScriptRoot {
    if ($PSScriptRoot) {
        return $PSScriptRoot
    }
    return Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Resolve-Python {
    param([switch]$Windowless)

    if ($Windowless) {
        $pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($pythonw) {
            return $pythonw.Source
        }

        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($python) {
            $candidate = Join-Path (Split-Path -Parent $python.Source) "pythonw.exe"
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
    }

    $pythonExe = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonExe) {
        return $pythonExe.Source
    }

    throw "python.exe was not found on PATH. Install Python 3 or add python.exe to PATH."
}

function Join-ProcessArguments {
    param([string[]]$Values)

    $quoted = @()
    foreach ($value in $Values) {
        if ($value -notmatch '[\s"]') {
            $quoted += $value
        } else {
            $quoted += '"' + ($value -replace '"', '\"') + '"'
        }
    }
    return ($quoted -join " ")
}

if ($Background -and $Foreground) {
    throw "Use either -Background or -Foreground, not both."
}

$Root = Get-ScriptRoot
$Monitor = Join-Path $Root "codex_monitor.py"
if (-not (Test-Path -LiteralPath $Monitor)) {
    throw "codex_monitor.py not found at $Monitor"
}

$Url = "http://localhost:$Port"
$Arguments = @($Monitor, "--host", $HostName, "--port", "$Port")

if ($Background) {
    $PythonExe = Resolve-Python -Windowless
    $LogDir = Join-Path $env:TEMP "codex-wire"
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $StdOutLog = Join-Path $LogDir "monitor.out.log"
    $StdErrLog = Join-Path $LogDir "monitor.err.log"

    $startInfo = @{
        FilePath = $PythonExe
        ArgumentList = (Join-ProcessArguments -Values $Arguments)
        WorkingDirectory = $Root
        PassThru = $true
        WindowStyle = "Hidden"
        RedirectStandardOutput = $StdOutLog
        RedirectStandardError = $StdErrLog
    }
    $process = Start-Process @startInfo

    Write-Host "Started CODEX WIRE monitor in the background."
    Write-Host "PID: $($process.Id)"
    Write-Host "Open: $Url"
    Write-Host "Logs:"
    Write-Host "  stdout: $StdOutLog"
    Write-Host "  stderr: $StdErrLog"
} else {
    $PythonExe = Resolve-Python
    Write-Host "Starting CODEX WIRE monitor in the foreground."
    Write-Host "Open: $Url"
    Write-Host "Press Ctrl+C to stop."
    & $PythonExe @Arguments
}
