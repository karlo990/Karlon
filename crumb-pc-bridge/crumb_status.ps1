<#
.SYNOPSIS
  What the always-on Crumb is doing, and simple controls.

.EXAMPLE
  .\crumb_status.ps1             # tasks, processes, memory, network, power, last log lines
  .\crumb_status.ps1 -Follow     # live log (Ctrl+C to leave; Crumb keeps running)
  .\crumb_status.ps1 -Restart    # fresh start of the WhatsApp bridge
  .\crumb_status.ps1 -Pause      # stop it and keep it stopped, restarts included
  .\crumb_status.ps1 -Kill       # the same, without waiting: Crumb's processes end at once
  .\crumb_status.ps1 -Resume     # start again (and at every sign-in, as before)
#>
[CmdletBinding()]
param(
    [switch]$Follow,
    [switch]$Restart,
    [switch]$Pause,
    [switch]$Kill,
    [switch]$Resume,
    [int]$Lines = 25
)

$TaskPath = '\Crumb\'
$Reg      = Get-ItemProperty -Path 'HKCU:\Software\Crumb' -ErrorAction SilentlyContinue
$StateDir = if ($env:CRUMB_STATE_DIR) { $env:CRUMB_STATE_DIR } elseif ($Reg.StateDir) { $Reg.StateDir } else { Join-Path $HOME '.crumb' }
$Beat     = Join-Path $StateDir 'supervisor.json'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

function Read-Beat {
    if (-not (Test-Path $Beat)) { return $null }
    try { return Get-Content $Beat -Raw | ConvertFrom-Json } catch { return $null }
}
function Beat-Age($b) {
    if (-not $b) { return [double]::PositiveInfinity }
    return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$b.updated
}
function Need-Admin {
    $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'This needs an Administrator PowerShell (right-click > Run as administrator).' -ForegroundColor Yellow
        exit 1
    }
}
function Wait-Stopped([int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $b = Read-Beat
        if (-not $b -or $b.state -eq 'stopped' -or (Beat-Age $b) -gt 60) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

if ($Restart) {
    New-Item -ItemType File -Force -Path (Join-Path $StateDir 'restart.request') | Out-Null
    Write-Host 'Restart requested - each process closes its browser and starts fresh within ~1 minute.' -ForegroundColor Cyan
    return
}
function Stop-CrumbProcesses {
    # Only Crumb's: Karlon's bridge and browsers on the same PC are left alone.
    $left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.CommandLine -match 'crumb_supervisor\.py|crumb_whatsapp\.py') -or
        ($_.Name -match 'chrom' -and $_.CommandLine -match '\\\.crumb\\wa_session')
    })
    foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    return $left.Count
}

if ($Pause -or $Kill) {
    Need-Admin
    Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null
    # the sign-in fallback would start it again after a restart: park it until -Resume
    $run = (Get-ItemProperty -Path $RunKey -Name 'Crumb' -ErrorAction SilentlyContinue).Crumb
    if ($run) {
        # (New-Item -Force on an existing key would wipe Crumb's install settings)
        if (-not (Test-Path 'HKCU:\Software\Crumb')) { New-Item -Path 'HKCU:\Software\Crumb' | Out-Null }
        Set-ItemProperty -Path 'HKCU:\Software\Crumb' -Name 'PausedRun' -Value $run
        Remove-ItemProperty -Path $RunKey -Name 'Crumb' -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $StateDir 'stop.request') | Out-Null
    if ($Kill) {
        Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
        $n = Stop-CrumbProcesses
        Write-Host "Killed: $n Crumb process(es) ended, tasks disabled. Start again with: .\crumb_status.ps1 -Resume" -ForegroundColor Green
        return
    }
    Write-Host 'Pausing: tasks disabled, asking Crumb to stop (WhatsApp closes its browser cleanly)...' -ForegroundColor Cyan
    if (-not (Wait-Stopped)) {
        Write-Host 'Still running after 90 s; ending it.' -ForegroundColor Yellow
        Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
    }
    $n = Stop-CrumbProcesses
    if ($n) { Write-Host "  closed $n leftover process(es)" }
    Write-Host 'Stopped, and stays stopped after a restart. Start again with: .\crumb_status.ps1 -Resume' -ForegroundColor Green
    return
}
if ($Resume) {
    Need-Admin
    Remove-Item (Join-Path $StateDir 'stop.request') -ErrorAction SilentlyContinue
    $parked = (Get-ItemProperty -Path 'HKCU:\Software\Crumb' -Name 'PausedRun' -ErrorAction SilentlyContinue).PausedRun
    if ($parked) {
        Set-ItemProperty -Path $RunKey -Name 'Crumb' -Value $parked
        Remove-ItemProperty -Path 'HKCU:\Software\Crumb' -Name 'PausedRun' -ErrorAction SilentlyContinue
    }
    $tasks = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
    if (-not $tasks) { Write-Host 'Crumb is not installed. Run .\install_crumb.ps1' -ForegroundColor Yellow; return }
    $tasks | Enable-ScheduledTask | Out-Null
    $first = $tasks | Where-Object TaskName -eq 'Crumb' | Select-Object -First 1
    if (-not $first) { $first = $tasks[0] }
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $first.TaskName
    Write-Host "Resumed ($($first.TaskName))." -ForegroundColor Green
    return
}

# -- status ------------------------------------------------------------------
Write-Host "`nCRUMB" -ForegroundColor Cyan
$tasks = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
if (-not $tasks) { Write-Host '  scheduled tasks: not installed (run .\install_crumb.ps1)' -ForegroundColor Yellow }
foreach ($t in $tasks) {
    $i = $t | Get-ScheduledTaskInfo
    $result = if ($i.LastTaskResult -eq 0) { 'ok' } elseif ($i.LastTaskResult -eq 267009) { 'running' } else { '0x{0:X}' -f $i.LastTaskResult }
    Write-Host ('  task {0,-13} {1,-9} last run {2}  result {3}' -f $t.TaskName, $t.State, $i.LastRunTime, $result)
}

$b = Read-Beat
$age = Beat-Age $b
if (-not $b) {
    Write-Host '  supervisor: never started on this PC' -ForegroundColor Yellow
} elseif ($b.state -eq 'stopped' -or $age -gt 180) {
    Write-Host ("  supervisor: NOT RUNNING (last heartbeat {0:N0} s ago)" -f $age) -ForegroundColor Red
} else {
    $where = if ($b.session -eq 0) { 'NOT SIGNED IN (session 0) - browsers wait for sign-in' } else { "in your session ($($b.session))" }
    $since = [DateTimeOffset]::FromUnixTimeSeconds([long]$b.started).LocalDateTime
    Write-Host ("  supervisor: RUNNING pid {0}, {1}, since {2:g}, heartbeat {3:N0} s ago" -f $b.pid, $where, $since, $age) -ForegroundColor Green
    $net = if ($b.network.ok) { 'ok' } else { 'DOWN (restarts wait for it)' }
    Write-Host ("  network: {0}    PC memory used: {1}%" -f $net, $b.system_memory_pct)
    foreach ($p in $b.children.PSObject.Properties) {
        $c = $p.Value
        $state = if ($c.running) { 'up' } elseif ($c.waiting_for_desktop) { 'wait' } else { 'down' }
        $color = if ($c.running) { 'Gray' } else { 'Yellow' }
        $how = if ($c.headless -eq $true) { 'headless' } elseif ($c.headless -eq $false) { 'window' } else { '' }
        Write-Host ('    {0,-8} {1,-5} {2,-9} pid {3,-6} up {4,5} min  {5,7} MB  restarts {6}' -f $p.Name, $state, $how, $c.pid,
            [math]::Floor([double]$c.uptime_s / 60), $c.memory_mb, $c.restarts) -ForegroundColor $color
        if ($c.waiting_for_desktop) { Write-Host "             waits for you to sign in to Windows (its saved login only opens in your session)" -ForegroundColor Yellow }
    }
}

$lid = (powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION 2>$null) -join "`n"
$hex = @([regex]::Matches($lid, '0x[0-9a-fA-F]{8}') | ForEach-Object { [Convert]::ToInt32($_.Value, 16) })
if ($hex.Count -ge 2) {
    $name = @{ 0 = 'do nothing'; 1 = 'sleep'; 2 = 'hibernate'; 3 = 'shut down' }
    Write-Host ("  lid closed: plugged in = {0}, battery = {1}" -f $name[$hex[-2]], $name[$hex[-1]])
}
$req = (powercfg /requests 2>$null) -join "`n"
if ($req -match 'python') { Write-Host '  keep-awake: active (shows in powercfg /requests)' } else { Write-Host '  keep-awake: not active (needs an Administrator PowerShell to see, or Crumb is not running)' }

$log = if ($b -and $b.log) { $b.log } elseif ($Reg.InstallDir) { Join-Path $Reg.InstallDir 'logs\crumb_supervisor.log' } else { $null }
if ($log -and (Test-Path $log)) {
    Write-Host "`n  log: $log" -ForegroundColor Cyan
    if ($Follow) { Get-Content $log -Tail $Lines -Wait }
    else { Get-Content $log -Tail $Lines | ForEach-Object { "  $_" } }
}
