param()

$ErrorActionPreference = "Stop"

function Get-ScriptRoot {
    if ($PSScriptRoot) {
        return $PSScriptRoot
    }
    return Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Test-CommandVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Arguments = @("--version")
    )

    try {
        $output = & $Path @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            return ($output | Select-Object -First 1)
        }
    } catch {
        return $null
    }

    return $null
}

function Get-WhereExe {
    $systemWhere = Join-Path $env:SystemRoot "System32\where.exe"
    if (Test-Path -LiteralPath $systemWhere) {
        return $systemWhere
    }
    return "where.exe"
}

$Root = Get-ScriptRoot
$EnvExample = Join-Path $Root ".env.example"
$EnvFile = Join-Path $Root ".env"

Write-Host "CODEX WIRE Windows install"
Write-Host "Root: $Root"
Write-Host ""

if (Test-Path -LiteralPath $EnvFile) {
    Write-Host "Keeping existing .env"
} elseif (Test-Path -LiteralPath $EnvExample) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host "Created .env from .env.example"
} else {
    Write-Warning ".env.example not found; skipped .env creation"
}

$CodexDir = Join-Path $HOME ".codex"
$DispatchSource = Join-Path $Root "dispatch.ps1"
$DispatchTarget = Join-Path $CodexDir "dispatch.ps1"
if (Test-Path -LiteralPath $DispatchSource) {
    New-Item -ItemType Directory -Force -Path $CodexDir | Out-Null
    Copy-Item -LiteralPath $DispatchSource -Destination $DispatchTarget -Force
    Write-Host "Installed dispatch wrapper at $DispatchTarget"
} else {
    Write-Warning "dispatch.ps1 not found; skipped dispatch wrapper install"
}

$ClaudeCommandSource = Join-Path $Root "examples\codex.md"
$ClaudeCommandDir = Join-Path $HOME ".claude\commands"
$ClaudeCommandTarget = Join-Path $ClaudeCommandDir "codex.md"
if (Test-Path -LiteralPath $ClaudeCommandTarget) {
    Write-Host "Keeping existing $ClaudeCommandTarget"
} elseif (Test-Path -LiteralPath $ClaudeCommandSource) {
    New-Item -ItemType Directory -Force -Path $ClaudeCommandDir | Out-Null
    Copy-Item -LiteralPath $ClaudeCommandSource -Destination $ClaudeCommandTarget
    Write-Host "Installed /codex skill at $ClaudeCommandTarget"
} else {
    Write-Warning "examples\codex.md not found; skipped /codex skill install"
}

Write-Host ""
Write-Host "Checking Python..."
$PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
$PyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
$PythonOk = $false

if ($PythonCommand) {
    $version = Test-CommandVersion -Path $PythonCommand.Source
    if ($version) {
        $PythonOk = $true
        Write-Host "  OK python: $($PythonCommand.Source) ($version)"
    } else {
        Write-Warning "  Found python.exe but it did not run successfully: $($PythonCommand.Source)"
    }
} else {
    Write-Warning "  python.exe not found on PATH"
}

if ($PyCommand) {
    $version = Test-CommandVersion -Path $PyCommand.Source -Arguments @("-3", "--version")
    if ($version) {
        $PythonOk = $true
        Write-Host "  OK py -3: $($PyCommand.Source) ($version)"
    } else {
        Write-Warning "  Found py.exe but 'py -3 --version' did not run successfully: $($PyCommand.Source)"
    }
} else {
    Write-Warning "  py.exe not found on PATH"
}

if (-not $PythonOk) {
    Write-Warning "Python 3 was not detected. Install Python 3 and add python.exe or py.exe to PATH."
}

Write-Host ""
Write-Host "Checking Codex CLI..."
$WhereExe = Get-WhereExe
try {
    $CodexPaths = @(& $WhereExe codex 2>$null)
    if ($LASTEXITCODE -eq 0 -and $CodexPaths.Count -gt 0) {
        Write-Host "  OK codex found via where.exe:"
        foreach ($path in $CodexPaths) {
            Write-Host "    $path"
        }
    } else {
        Write-Warning "  codex was not found by where.exe. Install Codex CLI and make sure it is on PATH."
    }
} catch {
    Write-Warning "  Could not run where.exe codex: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit $EnvFile"
Write-Host "  2. Run the monitor in the foreground:"
Write-Host "       powershell -ExecutionPolicy Bypass -File `"$Root\run.ps1`""
Write-Host "  3. Or run it in the background:"
Write-Host "       powershell -ExecutionPolicy Bypass -File `"$Root\run.ps1`" -Background"
Write-Host "  4. Open: http://localhost:8787"
Write-Host "  5. In Claude Code, start orchestrating: /codex"
