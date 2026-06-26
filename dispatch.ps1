# dispatch.ps1 - run ONE codex exec job, detect completion via its unique
# --output-last-message file, then reap the child process tree.
#
# Usage:
#   .\dispatch.ps1 <read-only|workspace-write> <cwd> "<prompt>" [max_minutes]
#   CODEX_WIRE_OUTDIR overrides the output directory.
# On completion prints machine-readable STATUS/OUT/LOG headers plus any summary.

param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Sandbox,
  [Parameter(Mandatory = $true, Position = 1)][string]$CwdInput,
  [Parameter(Mandatory = $true, Position = 2)][string]$Prompt,
  [Parameter(Position = 3)][string]$MaxMinutes = "90"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Die([string]$Message) {
  [Console]::Error.WriteLine("[dispatch] ERROR: $Message")
  exit 1
}

function Usage() {
  Die "usage: .\dispatch.ps1 <read-only|workspace-write> <cwd> <prompt> [max_minutes]"
}

function Tail-Log([string]$Path) {
  if (Test-Path -LiteralPath $Path) {
    [Console]::Error.WriteLine("[dispatch] $Path tail:")
    try {
      Get-Content -LiteralPath $Path -Tail 80 -ErrorAction SilentlyContinue |
        ForEach-Object { [Console]::Error.WriteLine($_) }
    } catch {
      [Console]::Error.WriteLine("[dispatch] failed to read log tail: $($_.Exception.Message)")
    }
  } else {
    [Console]::Error.WriteLine("[dispatch] log file missing: $Path")
  }
}

function New-DispatchOutputFile([string]$Directory) {
  for ($i = 0; $i -lt 100; $i++) {
    $name = "codex_{0}.md" -f ([Guid]::NewGuid().ToString("N"))
    $path = Join-Path -Path $Directory -ChildPath $name
    try {
      $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::Read)
      $stream.Close()
      return $path
    } catch [System.IO.IOException] {
      continue
    }
  }

  Die "failed to create output file in: $Directory"
}

function Get-OutSignature([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    return $null
  }

  $item = Get-Item -LiteralPath $Path
  if ($item.Length -le 0) {
    return $null
  }

  return ("{0}:{1}" -f $item.Length, $item.LastWriteTimeUtc.Ticks)
}

function Summary-IsStable([string]$Path) {
  $sig = Get-OutSignature $Path
  if ($null -eq $sig) {
    $script:LastOutSig = ""
    $script:StableHits = 0
    return $false
  }

  if ($sig -eq $script:LastOutSig) {
    $script:StableHits += 1
  } else {
    $script:LastOutSig = $sig
    $script:StableHits = 1
  }

  return ($script:StableHits -ge 2)
}

function Wait-ForCodexChild() {
  if ($null -eq $script:ChildProcess) {
    return
  }

  if ($script:Reaped) {
    return
  }

  try {
    $script:ChildProcess.Refresh()
    if ($script:ChildProcess.HasExited) {
      $script:Status = [int]$script:ChildProcess.ExitCode
      $script:Reaped = $true
    }
  } catch {
    $script:Reaped = $true
  }
}

function Cleanup-ProcessTree() {
  if ($script:Cleaning) {
    return
  }
  $script:Cleaning = $true

  if ($null -eq $script:ChildProcess) {
    return
  }

  try {
    $script:ChildProcess.Refresh()
    if ($script:ChildProcess.HasExited) {
      Wait-ForCodexChild
      return
    }
  } catch {
    return
  }

  $pidToKill = [int]$script:ChildProcess.Id
  $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue

  if ($null -ne $taskkill) {
    $taskkillPath = Get-CommandPath $taskkill
    if (-not $taskkillPath) {
      $taskkillPath = "taskkill.exe"
    }
    $script:TerminatedTree = $true
    & $taskkillPath /F /T /PID $pidToKill > $null 2>&1
  } else {
    $script:TerminatedChild = $true
    try {
      Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
    } catch {
    }
  }

  try {
    [void]$script:ChildProcess.WaitForExit(2000)
  } catch {
  }

  Wait-ForCodexChild
}

function Print-Headers([string]$DispatchStatus) {
  Write-Output "STATUS=$DispatchStatus"
  Write-Output "OUT=$script:Out"
  Write-Output "LOG=$script:Log"
}

function Has-Summary() {
  return ((Test-Path -LiteralPath $script:Out) -and ((Get-Item -LiteralPath $script:Out).Length -gt 0))
}

function Status-IsCleanupSummary() {
  return (
    ($script:StopReason -eq "summary") -and
    $script:SummaryParentRunning -and
    ($script:TerminatedTree -or $script:TerminatedChild)
  )
}

function Get-CommandPath($CommandInfo) {
  if ($null -eq $CommandInfo) {
    return $null
  }

  if ($CommandInfo.PSObject.Properties.Name -contains "Source") {
    $source = $CommandInfo.Source
    if ($source) {
      return $source
    }
  }

  if ($CommandInfo.PSObject.Properties.Name -contains "Path") {
    $path = $CommandInfo.Path
    if ($path) {
      return $path
    }
  }

  return $null
}

function Get-CurrentPowerShellPath() {
  try {
    $path = (Get-Process -Id $PID).Path
    if ($path) {
      return $path
    }
  } catch {
  }

  $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
  if ($null -ne $pwsh) {
    $path = Get-CommandPath $pwsh
    if ($path) {
      return $path
    }
  }

  $powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
  if ($null -ne $powershell) {
    $path = Get-CommandPath $powershell
    if ($path) {
      return $path
    }
  }

  Die "PowerShell executable not found"
}

if ($args.Count -ne 0) {
  Usage
}

if (($Sandbox -ne "read-only") -and ($Sandbox -ne "workspace-write")) {
  Die "sandbox must be read-only or workspace-write"
}

$resolvedCwd = $null
try {
  $resolvedCwd = Resolve-Path -LiteralPath $CwdInput -ErrorAction Stop
} catch {
  Die "cwd is not a directory: $CwdInput"
}

$cwdItem = Get-Item -LiteralPath $resolvedCwd.Path
if (-not $cwdItem.PSIsContainer) {
  Die "cwd is not a directory: $CwdInput"
}
$Cwd = $cwdItem.FullName

$parsedMax = 0
if (-not [int]::TryParse($MaxMinutes, [ref]$parsedMax)) {
  Die "max_minutes must be a positive integer"
}
if ($parsedMax -le 0) {
  Die "max_minutes must be a positive integer"
}
if ($parsedMax -gt 10080) {
  Die "max_minutes must be between 1 and 10080"
}

$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($null -eq $codexCommand) {
  Die "codex command not found"
}
$codexBin = Get-CommandPath $codexCommand
if (-not $codexBin) {
  Die "codex command path not found"
}

$baseTemp = $env:TEMP
if (-not $baseTemp) {
  $baseTemp = $env:TMP
}
if (-not $baseTemp) {
  $baseTemp = [System.IO.Path]::GetTempPath()
}

$outDir = $env:CODEX_WIRE_OUTDIR
if (-not $outDir) {
  $outDir = Join-Path -Path $baseTemp -ChildPath "codex-wire"
}

New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$script:Out = New-DispatchOutputFile $outDir
$script:Log = "$($script:Out).log"

$script:ChildProcess = $null
$script:Reaped = $false
$script:Status = $null
$script:TimedOut = $false
$script:StopReason = ""
$script:Cleaning = $false
$script:TerminatedTree = $false
$script:TerminatedChild = $false
$script:SummaryParentRunning = $false
$script:LastOutSig = ""
$script:StableHits = 0

$childScript = @'
$ErrorActionPreference = "Continue"
& $env:CODEX_WIRE_CODEX_BIN exec -C $env:CODEX_WIRE_CWD --skip-git-repo-check -s $env:CODEX_WIRE_SANDBOX -c approval_policy=never --output-last-message $env:CODEX_WIRE_OUT $env:CODEX_WIRE_PROMPT *> $env:CODEX_WIRE_LOG
if ($null -ne $global:LASTEXITCODE) {
  exit $global:LASTEXITCODE
}
if ($?) {
  exit 0
}
exit 1
'@

$encodedChildScript = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childScript))
$powerShellPath = Get-CurrentPowerShellPath

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $powerShellPath
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encodedChildScript"
$psi.EnvironmentVariables["CODEX_WIRE_CODEX_BIN"] = $codexBin
$psi.EnvironmentVariables["CODEX_WIRE_CWD"] = $Cwd
$psi.EnvironmentVariables["CODEX_WIRE_SANDBOX"] = $Sandbox
$psi.EnvironmentVariables["CODEX_WIRE_OUT"] = $script:Out
$psi.EnvironmentVariables["CODEX_WIRE_LOG"] = $script:Log
$psi.EnvironmentVariables["CODEX_WIRE_PROMPT"] = $Prompt

try {
  $script:ChildProcess = [System.Diagnostics.Process]::Start($psi)
} catch {
  Die "failed to start codex child process: $($_.Exception.Message)"
}

try {
  $deadline = (Get-Date).AddMinutes($parsedMax)

  while ($true) {
    $script:ChildProcess.Refresh()

    if ($script:ChildProcess.HasExited) {
      $script:StopReason = "exit"
      Wait-ForCodexChild
      break
    }

    if ((Get-Date) -ge $deadline) {
      $script:StopReason = "timeout"
      $script:TimedOut = $true
      [Console]::Error.WriteLine("[dispatch] TIMEOUT ${parsedMax}m - reaping process tree $($script:ChildProcess.Id)")
      break
    }

    if (Summary-IsStable $script:Out) {
      $script:StopReason = "summary"
      break
    }

    Start-Sleep -Seconds 2
  }

  Start-Sleep -Seconds 1

  switch ($script:StopReason) {
    "summary" {
      $script:ChildProcess.Refresh()
      if ($script:ChildProcess.HasExited) {
        Wait-ForCodexChild
      } else {
        $script:SummaryParentRunning = $true
      }
      Cleanup-ProcessTree
      Wait-ForCodexChild
    }
    "timeout" {
      Cleanup-ProcessTree
      Wait-ForCodexChild
    }
    "exit" {
      Cleanup-ProcessTree
    }
    default {
      Cleanup-ProcessTree
      Wait-ForCodexChild
    }
  }
} catch {
  Cleanup-ProcessTree
  [Console]::Error.WriteLine("[dispatch] ERROR: $($_.Exception.Message)")
  exit 1
}

if ($script:TimedOut) {
  Print-Headers "timeout"
  if (Has-Summary) {
    Write-Output "----- codex partial/late summary -----"
    Get-Content -LiteralPath $script:Out
  } else {
    [Console]::Error.WriteLine("[dispatch] no summary after timeout - see $script:Log")
    Tail-Log $script:Log
  }
  exit 124
}

if (($null -ne $script:Status) -and ($script:Status -ne 0) -and (-not (Status-IsCleanupSummary))) {
  Print-Headers "error"
  if (Has-Summary) {
    Write-Output "----- codex partial/late summary -----"
    Get-Content -LiteralPath $script:Out
  } else {
    [Console]::Error.WriteLine("[dispatch] codex exited nonzero ($script:Status) without summary - see $script:Log")
    Tail-Log $script:Log
  }
  exit $script:Status
}

if (Has-Summary) {
  Print-Headers "ok"
  Write-Output "----- codex summary -----"
  Get-Content -LiteralPath $script:Out
  exit 0
}

Print-Headers "error"
if (($null -ne $script:Status) -and ($script:Status -ne 0)) {
  [Console]::Error.WriteLine("[dispatch] codex exited nonzero ($script:Status) without summary - see $script:Log")
} else {
  [Console]::Error.WriteLine("[dispatch] no summary - see $script:Log")
}
Tail-Log $script:Log
exit 1
