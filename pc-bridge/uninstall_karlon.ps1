<#
.SYNOPSIS
  Removes the always-on Karlon setup made by install_karlon.ps1: stops it
  cleanly, deletes the scheduled tasks and registry entries, and puts the
  power settings back the way they were before the first install.
  The WhatsApp login (~\.karlon\wa_session) and your files are kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\uninstall_karlon.ps1
  powershell -ExecutionPolicy Bypass -File .\uninstall_karlon.ps1 -KeepPowerSettings
#>
[CmdletBinding()]
param([switch]$KeepPowerSettings)

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $pass = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($KeepPowerSettings) { $pass += '-KeepPowerSettings' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $pass
    return
}

$TaskPath = '\Karlon\'
$Reg      = Get-ItemProperty -Path 'HKCU:\Software\Karlon' -ErrorAction SilentlyContinue
$StateDir = if ($env:KARLON_STATE_DIR) { $env:KARLON_STATE_DIR } elseif ($Reg.StateDir) { $Reg.StateDir } else { Join-Path $HOME '.karlon' }
function Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }

Write-Host "`nRemoving Karlon always-on setup" -ForegroundColor Cyan

# 1. no more starts, then a clean stop (WhatsApp closes its browser itself)
$tasks = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
$tasks | Disable-ScheduledTask -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType File -Force -Path (Join-Path $StateDir 'stop.request') | Out-Null
$beatFile = Join-Path $StateDir 'supervisor.json'
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    try { $b = Get-Content $beatFile -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $b = $null }
    if (-not $b -or $b.state -eq 'stopped' -or ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$b.updated) -gt 60) { break }
    Start-Sleep -Seconds 2
}
$tasks | Stop-ScheduledTask -ErrorAction SilentlyContinue
Ok 'Karlon stopped'

# 2. anything left over: the supervisor, its scripts, Playwright browsers
$left = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.CommandLine -match 'karlon_supervisor|wa_bridge\.py|invoice_worker\.py|airbnb_parallel_system\.py') -or
    ($_.Name -match 'chrom' -and $_.CommandLine -notmatch '\\\.crumb\\' -and
        ($_.ExecutablePath -match 'ms-playwright' -or $_.CommandLine -match '\\\.karlon\\wa_session'))
}
foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if ($left) { Ok "closed $(@($left).Count) leftover process(es)" }

# 3. tasks and registry
foreach ($t in $tasks) { Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $TaskPath -Confirm:$false }
try {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $svc.GetFolder('\').DeleteFolder('Karlon', 0)
} catch { }
Ok 'scheduled tasks removed'
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Karlon' -ErrorAction SilentlyContinue
Remove-Item -Path 'HKCU:\Software\Karlon' -Recurse -ErrorAction SilentlyContinue
Remove-Item (Join-Path $StateDir 'stop.request') -ErrorAction SilentlyContinue
Ok 'registry entries removed'

# 4. power and Windows settings back as they were (not while Crumb's bridge needs them)
$BackupFile = Join-Path $StateDir 'install_backup.json'
$CrumbHere = Test-Path 'HKCU:\Software\Crumb'
if ($CrumbHere) {
    Ok 'the Crumb bridge is installed too: power, sign-in and Windows Update settings left as they are'
} elseif (-not $KeepPowerSettings -and (Test-Path $BackupFile)) {
    $bk = Get-Content $BackupFile -Raw | ConvertFrom-Json
    $keys = @{
        Lid       = @('SUB_BUTTONS', 'LIDACTION')
        Standby   = @('SUB_SLEEP', 'STANDBYIDLE')
        Hibernate = @('SUB_SLEEP', 'HIBERNATEIDLE')
        Wifi      = @('19cbb8fa-5279-450e-9fac-8a3d5fedd0c1', '12bbebe6-58d6-4636-95bb-3217ef867c1a')
    }
    foreach ($k in $keys.Keys) {
        $v = $bk.Power.$k
        if ($v) {
            powercfg /setacvalueindex SCHEME_CURRENT $keys[$k][0] $keys[$k][1] $v.AC 2>$null | Out-Null
            powercfg /setdcvalueindex SCHEME_CURRENT $keys[$k][0] $keys[$k][1] $v.DC 2>$null | Out-Null
        }
    }
    powercfg /setactive SCHEME_CURRENT | Out-Null
    foreach ($a in @($bk.Adapters)) {
        if ($a.Name -and $a.AllowComputerToTurnOffDevice -in @('Enabled', 'Disabled')) {
            Set-NetAdapterPowerManagement -Name $a.Name -AllowComputerToTurnOffDevice $a.AllowComputerToTurnOffDevice -ErrorAction SilentlyContinue
        }
    }
    $hb = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
    if ($null -ne $bk.HiberbootEnabled) { Set-ItemProperty -Path $hb -Name 'HiberbootEnabled' -Value ([int]$bk.HiberbootEnabled) -Type DWord }
    else { Remove-ItemProperty -Path $hb -Name 'HiberbootEnabled' -ErrorAction SilentlyContinue }
    $au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
    if ($null -ne $bk.NoAutoReboot) { Set-ItemProperty -Path $au -Name 'NoAutoRebootWithLoggedOnUsers' -Value ([int]$bk.NoAutoReboot) -Type DWord }
    else { Remove-ItemProperty -Path $au -Name 'NoAutoRebootWithLoggedOnUsers' -ErrorAction SilentlyContinue }
    Ok 'power plan, Fast Startup and Windows Update settings restored'
} elseif ($KeepPowerSettings) {
    Ok 'power settings left as they are (-KeepPowerSettings)'
}

# 5. automatic sign-in off again (whatever -KeepPowerSettings says; kept while the other bridge needs it)
. (Join-Path $PSScriptRoot 'karlon_lsa.ps1')
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$bk2 = if (Test-Path $BackupFile) { Get-Content $BackupFile -Raw | ConvertFrom-Json } else { $null }
if (-not $CrumbHere -and ($Reg.AutoLogon -eq 1 -or ((Get-ItemProperty -Path $wl -ErrorAction SilentlyContinue).AutoAdminLogon -eq '1' -and $bk2 -and $bk2.AutoAdminLogon -ne '1'))) {
    try { [KarlonLsa]::Set('DefaultPassword', $null) } catch { }
    $was = if ($bk2 -and $bk2.AutoAdminLogon) { [string]$bk2.AutoAdminLogon } else { '0' }
    Set-ItemProperty -Path $wl -Name 'AutoAdminLogon' -Value $was
    if ($bk2 -and $null -ne $bk2.PasswordLess) {
        Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' `
            -Name 'DevicePasswordLessBuildVersion' -Value ([int]$bk2.PasswordLess) -Type DWord -ErrorAction SilentlyContinue
    }
    Ok 'automatic sign-in turned off, stored password deleted'
}

Remove-Item $BackupFile -ErrorAction SilentlyContinue
Write-Host "`nDone. Your WhatsApp login and files are kept; run .\install_karlon.ps1 to set it up again." -ForegroundColor Cyan
