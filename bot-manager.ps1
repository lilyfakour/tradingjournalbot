#requires -Version 5.1
<#
  Trading Bot - local manager (Windows)

  Open it with bot.bat (double-click) or:
    powershell -NoProfile -ExecutionPolicy Bypass -File bot-manager.ps1

  The bot is started as a DETACHED HIDDEN process, so closing this window
  does NOT stop the bot. Files it uses (in the project folder):
    bot.pid  - PID of the running bot (recreated on every start, safe to delete)
    bot.log  - the bot's own log (bot.py writes it, utf-8)

  Non-interactive switches (for scripting/tests):
    -Status   print status and exit
    -Start    start the bot and exit
    -Stop     stop the bot and exit
#>
[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$Python     = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$BotScript  = Join-Path $ProjectDir 'bot.py'
$PidFile    = Join-Path $ProjectDir 'bot.pid'
$LogFile    = Join-Path $ProjectDir 'bot.log'

# ---------------------------------------------------------------------------
# Process detection: the pid file is the fast path; the command-line scan is
# the fallback, so a bot started manually (or a lost pid file) is still found.
# ---------------------------------------------------------------------------
function Get-BotProcessIds {
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    if (Test-Path -LiteralPath $PidFile) {
        $raw = Get-Content -LiteralPath $PidFile -TotalCount 1 -ErrorAction SilentlyContinue
        if ($raw -match '^\s*(\d+)\s*$') { [void]$ids.Add([int]$matches[1]) }
    }
    try {
        $cims = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop
        foreach ($cp in $cims) {
            if ($cp.CommandLine -and $cp.CommandLine -match 'bot\.py') {
                [void]$ids.Add([int]$cp.ProcessId)
            }
        }
    } catch { }
    # keep only living python processes (stale pids drop out here)
    $alive = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($id in $ids) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -match '^python') { [void]$alive.Add($id) }
    }
    return $alive
}

function Get-BotProcess {
    foreach ($id in Get-BotProcessIds) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p) { return $p }
    }
    return $null
}

function Format-Uptime([TimeSpan]$ts) {
    if ($ts.TotalDays -ge 1)   { return ('{0}d {1}h {2}m' -f [int]$ts.TotalDays, $ts.Hours, $ts.Minutes) }
    if ($ts.TotalHours -ge 1)  { return ('{0}h {1}m' -f [int]$ts.TotalHours, $ts.Minutes) }
    return ('{0}m {1}s' -f [int]$ts.TotalMinutes, $ts.Seconds)
}

function Show-Header {
    Clear-Host
    Write-Host ' ==============================================' -ForegroundColor DarkCyan
    Write-Host '   Trading Bot - local manager'                   -ForegroundColor Cyan
    Write-Host ' ==============================================' -ForegroundColor DarkCyan
    $proc = Get-BotProcess
    if ($proc) {
        $up  = try { Format-Uptime ((Get-Date) - $proc.StartTime) } catch { '?h' }
        $mem = '{0:N0} MB' -f ($proc.WorkingSet64 / 1MB)
        Write-Host ('   [RUNNING]  PID {0}   up {1}   mem {2}' -f $proc.Id, $up, $mem) -ForegroundColor Green
    } else {
        Write-Host '   [STOPPED]  the bot is not running' -ForegroundColor Red
        if (Test-Path -LiteralPath $PidFile) {
            Write-Host '   (stale bot.pid found - cleaned automatically on next start)' -ForegroundColor DarkYellow
        }
    }
    if (Test-Path -LiteralPath $LogFile) {
        $last = Get-Content -LiteralPath $LogFile -Tail 1 -ErrorAction SilentlyContinue
        if ($last) {
            if ($last.Length -gt 96) { $last = '...' + $last.Substring($last.Length - 96) }
            Write-Host ('   log: ' + $last) -ForegroundColor DarkGray
        }
    }
    Write-Host ''
}
# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
function Start-Bot {
    $proc = Get-BotProcess
    if ($proc) {
        Write-Host (' Bot is already running (PID {0}) - nothing to do.' -f $proc.Id) -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host ' Virtual environment not found:' -ForegroundColor Red
        Write-Host ('   ' + $Python) -ForegroundColor Red
        Write-Host ' Create it once with:' -ForegroundColor Yellow
        Write-Host '   python -m venv .venv' -ForegroundColor Yellow
        Write-Host '   .venv\Scripts\python.exe -m pip install -r requirements.txt' -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path -LiteralPath $BotScript)) {
        Write-Host (' bot.py not found at ' + $BotScript) -ForegroundColor Red
        return
    }
    Write-Host ' Starting bot (hidden, detached - it survives this window)...' -ForegroundColor Gray
    $p = Start-Process -FilePath $Python `
            -ArgumentList ('"{0}"' -f $BotScript) `
            -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $PidFile -Value $p.Id -Encoding Ascii
    # Watch the first seconds: crashes at startup (bad token, another poller
    # holding getUpdates, import errors) usually happen within this window.
    $alive = $null
    for ($i = 0; $i -lt 7; $i++) {
        Start-Sleep -Seconds 1
        $alive = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
        if (-not $alive) { break }
    }
    if ($alive) {
        Write-Host (' Bot is RUNNING (PID {0}) - it stays up after this window closes.' -f $p.Id) -ForegroundColor Green
    } else {
        Write-Host ' The bot exited immediately. Last log lines:' -ForegroundColor Red
        if (Test-Path -LiteralPath $LogFile) {
            Get-Content -LiteralPath $LogFile -Tail 12 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host ('   ' + $_) -ForegroundColor DarkGray }
        } else {
            Write-Host '   (no bot.log was created)' -ForegroundColor DarkGray
        }
        Write-Host ' Common causes:' -ForegroundColor Yellow
        Write-Host '  - TELEGRAM_BOT_TOKEN missing in .env' -ForegroundColor Yellow
        Write-Host '  - another poller on the same token (e.g. Railway) - pause one of them' -ForegroundColor Yellow
        Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    }
}

function Stop-Bot {
    $ids = @(Get-BotProcessIds)
    if ($ids.Count -eq 0) {
        Write-Host ' Bot is not running.' -ForegroundColor Yellow
        Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
        return
    }
    foreach ($id in $ids) {
        try {
            Stop-Process -Id $id -Force -ErrorAction Stop
            Write-Host (' Stopped PID {0}.' -f $id) -ForegroundColor Green
        } catch {
            Write-Host (' Could not stop PID {0}: {1}' -f $id, $_.Exception.Message) -ForegroundColor Red
        }
    }
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
}

function Show-LiveLog {
    if (-not (Test-Path -LiteralPath $LogFile)) {
        Write-Host ' No bot.log yet - start the bot first.' -ForegroundColor Yellow
        return
    }
    $cmd = 'Get-Content -LiteralPath "{0}" -Tail 40 -Wait' -f $LogFile
    Start-Process powershell.exe -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-Command', $cmd
    ) | Out-Null
    Write-Host ' Log window opened (closing it does NOT stop the bot).' -ForegroundColor Gray
}
function Clear-BotLog {
    if (-not (Test-Path -LiteralPath $LogFile)) {
        Write-Host ' No bot.log yet.' -ForegroundColor Yellow
        return
    }
    Clear-Content -LiteralPath $LogFile
    Write-Host ' bot.log cleared.' -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Menu (TUI): Up/Down + Enter, number keys 1-6, Esc quits.
# ---------------------------------------------------------------------------
function Invoke-Menu {
    $items = @(
        'Start bot',
        'Stop bot',
        'Restart bot',
        'Live log (opens a separate window)',
        'Clear log file',
        'Quit'
    )
    $sel = 0
    while ($true) {
        Show-Header
        for ($i = 0; $i -lt $items.Count; $i++) {
            if ($i -eq $sel) {
                Write-Host ('   > ' + $items[$i]) -ForegroundColor Cyan
            } else {
                Write-Host ('     ' + $items[$i]) -ForegroundColor Gray
            }
        }
        Write-Host ''
        Write-Host '   Up/Down + Enter - number keys 1-6 - Esc quits' -ForegroundColor DarkGray

        $key = [Console]::ReadKey($true)
        $action = -1
        switch ($key.Key) {
            'UpArrow'   { if ($sel -gt 0) { $sel-- } }
            'DownArrow' { if ($sel -lt $items.Count - 1) { $sel++ } }
            'Home'      { $sel = 0 }
            'End'       { $sel = $items.Count - 1 }
            'Enter'     { $action = $sel }
            'Escape'    { Clear-Host; return }
        }
        if ($action -lt 0 -and $key.KeyChar) {
            $n = 0
            if ([int]::TryParse([string]$key.KeyChar, [ref]$n) -and $n -ge 1 -and $n -le $items.Count) {
                $action = $n - 1
            } elseif ($key.KeyChar -eq 'q' -or $key.KeyChar -eq 'Q') {
                Clear-Host; return
            }
        }
        if ($action -ge 0) {
            Show-Header
            switch ($action) {
                0 { Start-Bot }
                1 { Stop-Bot }
                2 { Stop-Bot; Start-Bot }
                3 { Show-LiveLog }
                4 { Clear-BotLog }
                5 { Clear-Host; return }
            }
            Write-Host ''
            Write-Host ' Press any key to return to the menu...' -ForegroundColor DarkGray
            [void][Console]::ReadKey($true)
        }
    }
}

# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
if ($Status) { Show-Header; exit 0 }
if ($Start)  { Start-Bot;   exit 0 }
if ($Stop)   { Stop-Bot;    exit 0 }
Invoke-Menu


