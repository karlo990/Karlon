<#
.SYNOPSIS
  What the always-on Karlon is doing, and simple controls.

.EXAMPLE
  .\karlon_status.ps1             # tasks, processes, memory, network, power, last log lines
  .\karlon_status.ps1 -Follow     # live log (Ctrl+C to leave; Karlon keeps running)
  .\karlon_status.ps1 -Restart    # fresh start of the WhatsApp bridge, invoices and scraper
  .\karlon_status.ps1 -Pause      # stop it and keep it stopped (tasks disabled)
  .\karlon_status.ps1 -Resume     # enable the tasks again and start
#>
[CmdletBinding()]
param(
    [switch]$Follow,
    [switch]$Restart,
    [switch]$Pause,
    [switch]$Resume,
    [int]$Lines = 25
)

$TaskPath = '\Karlon\'
$Reg      = Get-ItemProperty -Path 'HKCU:\Software\Karlon' -ErrorAction SilentlyContinue
$StateDir = if ($env:KARLON_STATE_DIR) { $env:KARLON_STATE_DIR } elseif ($Reg.StateDir) { $Reg.StateDir } else { Join-Path $HOME '.karlon' }
$Beat     = Join-Path $StateDir 'supervisor.json'

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
if ($Pause) {
    Need-Admin
    Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $StateDir 'stop.request') | Out-Null
    Write-Host 'Pausing: tasks disabled, asking Karlon to stop (WhatsApp closes its browser cleanly)...' -ForegroundColor Cyan
    if (Wait-Stopped) { Write-Host 'Stopped. Start again with: .\karlon_status.ps1 -Resume' -ForegroundColor Green }
    else { Write-Host 'Still running after 90 s; ending the tasks.' -ForegroundColor Yellow; Get-ScheduledTask -TaskPath $TaskPath | Stop-ScheduledTask }
    return
}
if ($Resume) {
    Need-Admin
    Remove-Item (Join-Path $StateDir 'stop.request') -ErrorAction SilentlyContinue
    $tasks = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
    if (-not $tasks) { Write-Host 'Karlon is not installed. Run .\install_karlon.ps1' -ForegroundColor Yellow; return }
    $tasks | Enable-ScheduledTask | Out-Null
    $first = $tasks | Where-Object TaskName -eq 'Karlon' | Select-Object -First 1
    if (-not $first) { $first = $tasks[0] }
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $first.TaskName
    Write-Host "Resumed ($($first.TaskName))." -ForegroundColor Green
    return
}

# -- status ------------------------------------------------------------------
Write-Host "`nKARLON" -ForegroundColor Cyan
$tasks = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
if (-not $tasks) { Write-Host '  scheduled tasks: not installed (run .\install_karlon.ps1)' -ForegroundColor Yellow }
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
    $where = if ($b.session -eq 0) { 'NO DESKTOP - browsers wait for sign-in' } else { "on the desktop (session $($b.session))" }
    $since = [DateTimeOffset]::FromUnixTimeSeconds([long]$b.started).LocalDateTime
    Write-Host ("  supervisor: RUNNING pid {0}, {1}, since {2:g}, heartbeat {3:N0} s ago" -f $b.pid, $where, $since, $age) -ForegroundColor Green
    $net = if ($b.network.ok) { 'ok' } else { 'DOWN (restarts wait for it)' }
    Write-Host ("  network: {0}    PC memory used: {1}%" -f $net, $b.system_memory_pct)
    foreach ($p in $b.children.PSObject.Properties) {
        $c = $p.Value
        $state = if ($c.running) { 'up' } else { 'down' }
        $color = if ($c.running) { 'Gray' } else { 'Yellow' }
        Write-Host ('    {0,-8} {1,-5} pid {2,-6} up {3,5} min  {4,7} MB  restarts {5}' -f $p.Name, $state, $c.pid,
            [math]::Floor([double]$c.uptime_s / 60), $c.memory_mb, $c.restarts) -ForegroundColor $color
    }
}

$lid = (powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION 2>$null) -join "`n"
$hex = @([regex]::Matches($lid, '0x[0-9a-fA-F]{8}') | ForEach-Object { [Convert]::ToInt32($_.Value, 16) })
if ($hex.Count -ge 2) {
    $name = @{ 0 = 'do nothing'; 1 = 'sleep'; 2 = 'hibernate'; 3 = 'shut down' }
    Write-Host ("  lid closed: plugged in = {0}, battery = {1}" -f $name[$hex[-2]], $name[$hex[-1]])
}
$req = (powercfg /requests 2>$null) -join "`n"
if ($req -match 'python') { Write-Host '  keep-awake: active (shows in powercfg /requests)' } else { Write-Host '  keep-awake: not active (needs an Administrator PowerShell to see, or Karlon is not running)' }

$log = if ($b -and $b.log) { $b.log } elseif ($Reg.InstallDir) { Join-Path $Reg.InstallDir 'logs\karlon_supervisor.log' } else { $null }
if ($log -and (Test-Path $log)) {
    Write-Host "`n  log: $log" -ForegroundColor Cyan
    if ($Follow) { Get-Content $log -Tail $Lines -Wait }
    else { Get-Content $log -Tail $Lines | ForEach-Object { "  $_" } }
}
