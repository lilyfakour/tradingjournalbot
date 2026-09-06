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
    -Check    run the environment pre-flight checks and exit (code 0 = ok)
    -Install  run pip install -r requirements.txt and exit
#>
[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Check,
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$Python     = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$BotScript  = Join-Path $ProjectDir 'bot.py'
$PidFile    = Join-Path $ProjectDir 'bot.pid'
$LogFile    = Join-Path $ProjectDir 'bot.log'
$EnvFile    = Join-Path $ProjectDir '.env'

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
    if (-not (Test-Path -LiteralPath $BotScript)) {
        Write-Host (' bot.py not found at ' + $BotScript) -ForegroundColor Red
        return
    }
    # Pre-flight: catch a broken environment HERE with the exact reason,
    # instead of letting the bot die in 4s and guessing from hints.
    $envOk = Test-Environment
    if (-not $envOk) { return }
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
        Write-Host '  - TELEGRAM_BOT_TOKEN missing/wrong in .env (git clones do NOT ship .env)' -ForegroundColor Yellow
        Write-Host '  - another poller on the same token (Railway / another PC) - pause one' -ForegroundColor Yellow
        Write-Host '  - dependencies missing (menu: Install / repair dependencies)' -ForegroundColor Yellow
        Write-Host '  - api.telegram.org unreachable (VPN needed on some networks)' -ForegroundColor Yellow
        Write-Host ' Menu > "Environment check" pinpoints the exact cause.' -ForegroundColor Cyan
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
# Pre-flight environment checks: find the REAL reason a fresh laptop fails.
# The start-watchdog hints are generic; these checks pinpoint the problem.
# ---------------------------------------------------------------------------
function Test-Environment {
    $fail = 0
    Write-Host ''
    Write-Host ' --- Environment check ---' -ForegroundColor Cyan

    # 1) venv + dependencies ----------------------------------------------
    if (Test-Path -LiteralPath $Python) {
        Write-Host ' [OK] .venv\Scripts\python.exe found' -ForegroundColor Green
        $out = & $Python -c "import telegram, openpyxl; print('ptb', telegram.__version__, '/ openpyxl', openpyxl.__version__)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host (' [OK] dependencies importable (' + ($out -join ' ') + ')') -ForegroundColor Green
        } else {
            Write-Host ' [X] python-telegram-bot / openpyxl are NOT installed in this .venv.' -ForegroundColor Red
            Write-Host '     Fix: menu item "Install / repair dependencies" (internet needed).' -ForegroundColor Yellow
            $fail++
        }
    } else {
        Write-Host ' [X] .venv NOT FOUND (no .venv\Scripts\python.exe).' -ForegroundColor Red
        Write-Host '     Fix, once:  python -m venv .venv' -ForegroundColor Yellow
        Write-Host '     (needs Python 3.10+ from python.org, "Add python.exe to PATH"),' -ForegroundColor Yellow
        Write-Host '     then menu item "Install / repair dependencies".' -ForegroundColor Yellow
        $fail++
    }

    # 2) .env + token -------------------------------------------------------
    $token = ''
    if (Test-Path -LiteralPath $EnvFile) {
        Write-Host ' [OK] .env found next to bot.py' -ForegroundColor Green
        foreach ($line in Get-Content -LiteralPath $EnvFile -ErrorAction SilentlyContinue) {
            if ($line -match '^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.+)\s*$') {
                $token = $matches[1].Trim().Trim("'", '"')
                break
            }
        }
        if (-not $token) {
            Write-Host ' [X] .env has no TELEGRAM_BOT_TOKEN line.' -ForegroundColor Red
            Write-Host '     Add:  TELEGRAM_BOT_TOKEN=1234567:AA...your-real-token' -ForegroundColor Yellow
            $fail++
        } elseif ($token -like '*AAAA-your-token-here*') {
            Write-Host ' [X] .env still contains the EXAMPLE placeholder - paste the REAL token' -ForegroundColor Red
            Write-Host '     from @BotFather (copy .env from the working laptop).' -ForegroundColor Yellow
            $fail++
        } elseif ($token -notmatch '^\d{5,}:[A-Za-z0-9_-]{25,}$') {
            Write-Host ' [!] Token shape looks unusual (expected 1234567:AA...). Verify it.' -ForegroundColor Yellow
        }
    } else {
        Write-Host ' [X] .env NOT FOUND next to bot.py. A git clone does NOT include it' -ForegroundColor Red
        Write-Host '     (.env is gitignored). Create .env there with the single line:' -ForegroundColor Yellow
        Write-Host '       TELEGRAM_BOT_TOKEN=1234567:AA...your-real-token' -ForegroundColor Yellow
        $fail++
    }
    # 3) live token + connectivity test (getMe never conflicts with polling) -
    if ($token) {
        try {
            $r = Invoke-RestMethod -Uri ('https://api.telegram.org/bot' + $token + '/getMe') -TimeoutSec 12
            Write-Host (' [OK] Telegram API reachable, token valid (@' + $r.result.username + ')') -ForegroundColor Green
        } catch {
            $code = $null
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            if ($code -eq 401) {
                Write-Host ' [X] Telegram says 401 Unauthorized - the token is WRONG/revoked.' -ForegroundColor Red
                $fail++
            } else {
                Write-Host ' [!] Could NOT reach api.telegram.org. On many networks (e.g. IRAN)' -ForegroundColor Yellow
                Write-Host '     Telegram needs a VPN/proxy. If Start then dies with NetworkError' -ForegroundColor Yellow
                Write-Host '     in the log, THIS is the cause - not a second poller.' -ForegroundColor Yellow
            }
        }
    }

    # 4) evidence of a second poller ---------------------------------------
    if (Test-Path -LiteralPath $LogFile) {
        $tail = @(Get-Content -LiteralPath $LogFile -Tail 40 -ErrorAction SilentlyContinue)
        if ($tail -match 'Conflict: terminated by other getUpdates') {
            Write-Host ' [!] bot.log shows "terminated by other getUpdates" - ANOTHER poller' -ForegroundColor Yellow
            Write-Host '     (Railway / another PC) is using this token. Pause one of them.' -ForegroundColor Yellow
        }
    }
    try {
        $cims = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop
        $others = @($cims | Where-Object { $_.CommandLine -match 'bot\.py' })
        if ($others.Count -gt 0) {
            Write-Host (' [!] ' + $others.Count + ' local bot.py process(es) already running (PID ' +
                (($others | ForEach-Object { $_.ProcessId }) -join ', ') + ').') -ForegroundColor Yellow
        }
    } catch { }

    Write-Host ''
    if ($fail -eq 0) {
        Write-Host ' Result: no blocking problems found.' -ForegroundColor Green
        return $true
    }
    Write-Host (' Result: ' + $fail + ' blocking problem(s) [X] above - fix them, then Start again.') -ForegroundColor Red
    return $false
}

function Invoke-PipInstall {
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host ' [X] .venv not found. Create it once with:' -ForegroundColor Red
        Write-Host '       python -m venv .venv' -ForegroundColor Yellow
        Write-Host '     (needs Python 3.10+ from python.org - tick "Add python.exe to PATH")' -ForegroundColor Yellow
        return
    }
    Write-Host ' Installing requirements.txt into the venv (needs internet)...' -ForegroundColor Cyan
    & $Python -m pip install -r (Join-Path $ProjectDir 'requirements.txt')
    if ($LASTEXITCODE -eq 0) {
        Write-Host ' Dependencies installed.' -ForegroundColor Green
    } else {
        Write-Host ' pip FAILED - check internet / proxy and try again.' -ForegroundColor Red
    }
}

# ---------------------------------------------------------------------------
# Menu (TUI): Up/Down + Enter, number keys 1-8, Esc quits.
# ---------------------------------------------------------------------------
function Invoke-Menu {
    $items = @(
        'Start bot',
        'Stop bot',
        'Restart bot',
        'Live log (opens a separate window)',
        'Clear log file',
        'Environment check (.env / deps / Telegram)',
        'Install / repair dependencies (pip)',
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
        Write-Host '   Up/Down + Enter - number keys 1-8 - Esc quits' -ForegroundColor DarkGray

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
                5 { $null = Test-Environment }
                6 { Invoke-PipInstall }
                7 { Clear-Host; return }
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
if ($Check)  { if (Test-Environment) { exit 0 } else { exit 1 } }
if ($Install){ Invoke-PipInstall; exit 0 }
Invoke-Menu


