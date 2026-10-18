<#
.SYNOPSIS
  Makes Karlon (WhatsApp bridge, invoice worker, Airbnb scraper) an always-on
  part of Windows: starts at boot and at logon, survives Wi-Fi drops, sleep,
  a closed lid and crashes, and keeps its browsers and memory clean.

.DESCRIPTION
  What it sets up (everything is undone by uninstall_karlon.ps1):

  Task Scheduler, folder \Karlon\
    "Karlon Logon"  runs on your desktop when you log on (browser windows
                    visible, so a WhatsApp QR code can be scanned).
    "Karlon Boot"   runs at power-on before anyone logs on, hidden. When you
                    log on, the Logon task takes over so you can see it.
    Both: restart 1 min after any crash (999 times), no time limit, run on
    battery, start missed runs, re-check every 5 minutes, and
    start again when the network (re)connects or the PC wakes from sleep.
    Only one copy ever runs (karlon_supervisor.py holds a lock).

  Power plan (current plan, values saved first)
    Closing the lid: do nothing (plugged in and on battery).
    Sleep / hibernate when plugged in: never. On battery too with
    -KeepAwakeOnBattery. While Karlon runs it also blocks idle sleep itself.
    Wi-Fi adapter: maximum performance; Windows may not switch it off.

  Registry
    HKCU\Software\Karlon             install folder, Python, mode.
    HKCU\...\CurrentVersion\Run      "Karlon" fallback start at logon.
    HKLM ...\Session Manager\Power   HiberbootEnabled=0 (Fast Startup off:
                                     a real boot, so the Boot task runs).
    HKLM ...\WindowsUpdate\AU        NoAutoRebootWithLoggedOnUsers=1.

.PARAMETER Mode
  Both (default), Logon (only when you're logged on, visible) or Boot (only
  the hidden boot task).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install_karlon.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install_karlon.ps1 -KeepAwakeOnBattery -KarlonUrl https://davincii-code-karlcon.hf.space
#>
[CmdletBinding()]
param(
    [ValidateSet('Both', 'Logon', 'Boot')] [string]$Mode = 'Both',
    [string]$Python,
    [string]$KarlonUrl,
    [switch]$KeepAwakeOnBattery,
    [switch]$NoPowerChanges,
    [switch]$NoUpdateRebootBlock,
    [switch]$SkipPip,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

# -- run as Administrator (re-launch elevated if needed) ---------------------
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Karlon needs Administrator rights to install - asking Windows...' -ForegroundColor Yellow
    $pass = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($kv in $PSBoundParameters.GetEnumerator()) {
        if ($kv.Value -is [switch]) { if ($kv.Value) { $pass += "-$($kv.Key)" } }
        else { $pass += "-$($kv.Key)"; $pass += "`"$($kv.Value)`"" }
    }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $pass
    return
}

$Here      = $PSScriptRoot
$StateDir  = if ($env:KARLON_STATE_DIR) { $env:KARLON_STATE_DIR } else { Join-Path $HOME '.karlon' }
$TaskPath  = '\Karlon\'
$User      = "$env:USERDOMAIN\$env:USERNAME"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
Start-Transcript -Path (Join-Path $StateDir 'install.log') -Append | Out-Null

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }

try {
    if (-not (Test-Path (Join-Path $Here 'karlon_supervisor.py'))) {
        throw "karlon_supervisor.py isn't next to this script ($Here). Run it from the pc-bridge folder."
    }
    Say "`nKarlon always-on install  ($Mode)  -  folder $Here" 'Cyan'

    # -- Python --------------------------------------------------------------
    if (-not $Python) {
        try { $Python = (& py -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1) } catch { }
        if (-not $Python) {
            $cmd = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1
            if ($cmd) { $Python = $cmd.Source }
        }
    }
    if (-not $Python -or -not (Test-Path $Python)) {
        throw 'Python not found. Install Python 3.11+ from python.org (tick "Add to PATH"), or pass -Python C:\path\to\python.exe'
    }
    $Python  = (Resolve-Path $Python).Path
    $PythonW = Join-Path (Split-Path $Python) 'pythonw.exe'
    if (-not (Test-Path $PythonW)) { $PythonW = $Python }
    if ($Python -match 'WindowsApps') { Warn 'This is the Microsoft Store Python; the boot task may not be able to start it. python.org Python is recommended.' }
    Ok "Python: $Python"

    if (-not $SkipPip) {
        Say '  installing Python packages (requirements.txt)...'
        & $Python -m pip install --quiet --disable-pip-version-check -r (Join-Path $Here 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { Warn 'pip reported a problem; Karlon still runs, but check the packages.' } else { Ok 'packages installed (incl. psutil for memory limits)' }
    }
    if ($KarlonUrl) {
        [Environment]::SetEnvironmentVariable('KARLON_URL', $KarlonUrl.TrimEnd('/'), 'User')
        Ok "KARLON_URL saved for your user: $KarlonUrl"
    }

    # -- backup of everything we change (kept from the FIRST install) -------
    $BackupFile = Join-Path $StateDir 'install_backup.json'
    function Get-PowerIndex([string]$Sub, [string]$Setting) {
        $out = (powercfg /query SCHEME_CURRENT $Sub $Setting 2>$null) -join "`n"
        if ($LASTEXITCODE -ne 0) { return $null }
        $hex = @([regex]::Matches($out, '0x[0-9a-fA-F]{8}') | ForEach-Object { $_.Value })
        if ($hex.Count -lt 2) { return $null }
        return [pscustomobject]@{ AC = [Convert]::ToInt32($hex[-2], 16); DC = [Convert]::ToInt32($hex[-1], 16) }
    }
    function Get-RegValue($Path, $Name) {
        try { return (Get-ItemProperty -Path $Path -Name $Name -ErrorAction Stop).$Name } catch { return $null }
    }
    $PowerKeys = [ordered]@{
        Lid       = @('SUB_BUTTONS', 'LIDACTION')
        Standby   = @('SUB_SLEEP', 'STANDBYIDLE')
        Hibernate = @('SUB_SLEEP', 'HIBERNATEIDLE')
        Wifi      = @('19cbb8fa-5279-450e-9fac-8a3d5fedd0c1', '12bbebe6-58d6-4636-95bb-3217ef867c1a')
    }
    $HiberbootKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
    $AUKey        = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
    if (-not (Test-Path $BackupFile)) {
        $backup = [ordered]@{ Saved = (Get-Date).ToString('s'); Power = [ordered]@{}; Adapters = @() }
        foreach ($k in $PowerKeys.Keys) { $pk = $PowerKeys[$k]; $backup.Power[$k] = Get-PowerIndex $pk[0] $pk[1] }
        $backup.HiberbootEnabled = Get-RegValue $HiberbootKey 'HiberbootEnabled'
        $backup.NoAutoReboot     = Get-RegValue $AUKey 'NoAutoRebootWithLoggedOnUsers'
        try {
            $backup.Adapters = @(Get-NetAdapter -Physical -ErrorAction Stop | ForEach-Object {
                $pm = Get-NetAdapterPowerManagement -Name $_.Name -ErrorAction SilentlyContinue
                if ($pm) { [pscustomobject]@{ Name = $_.Name; AllowComputerToTurnOffDevice = [string]$pm.AllowComputerToTurnOffDevice } }
            })
        } catch { }
        $backup | ConvertTo-Json -Depth 5 | Set-Content -Path $BackupFile -Encoding UTF8
        Ok "current settings saved to $BackupFile"
    }

    # -- power: lid, sleep, Wi-Fi ---------------------------------------------
    if (-not $NoPowerChanges) {
        function Set-PowerIndex([string]$Sub, [string]$Setting, $Ac, $Dc) {
            if ($null -ne $Ac) { powercfg /setacvalueindex SCHEME_CURRENT $Sub $Setting $Ac 2>$null | Out-Null }
            if ($null -ne $Dc) { powercfg /setdcvalueindex SCHEME_CURRENT $Sub $Setting $Dc 2>$null | Out-Null }
            return ($LASTEXITCODE -eq 0)
        }
        $dcSleep = if ($KeepAwakeOnBattery) { 0 } else { $null }
        if (Set-PowerIndex 'SUB_BUTTONS' 'LIDACTION' 0 0) { Ok 'closing the lid: do nothing (plugged in and on battery)' } else { Warn 'no lid setting on this PC (desktop?) - skipped' }
        Set-PowerIndex 'SUB_SLEEP' 'STANDBYIDLE' 0 $dcSleep | Out-Null
        Set-PowerIndex 'SUB_SLEEP' 'HIBERNATEIDLE' 0 $dcSleep | Out-Null
        Ok ('sleep/hibernate: never when plugged in' + $(if ($KeepAwakeOnBattery) { ', never on battery' } else { ' (battery unchanged; use -KeepAwakeOnBattery)' }))
        if (Set-PowerIndex $PowerKeys.Wifi[0] $PowerKeys.Wifi[1] 0 0) { Ok 'Wi-Fi adapter power saving: maximum performance' }
        powercfg /setactive SCHEME_CURRENT | Out-Null
        try {
            Get-NetAdapter -Physical -ErrorAction Stop | ForEach-Object {
                Set-NetAdapterPowerManagement -Name $_.Name -AllowComputerToTurnOffDevice Disabled -ErrorAction SilentlyContinue
            }
            Ok 'network adapters: Windows may not switch them off to save power'
        } catch { Warn 'could not change network adapter power management (skipped)' }

        New-Item -Path $HiberbootKey -Force -ErrorAction SilentlyContinue | Out-Null
        Set-ItemProperty -Path $HiberbootKey -Name 'HiberbootEnabled' -Value 0 -Type DWord
        Ok 'Fast Startup off (every power-on is a real boot, so the Boot task always runs)'
    }
    if (-not $NoUpdateRebootBlock) {
        New-Item -Path $AUKey -Force | Out-Null
        Set-ItemProperty -Path $AUKey -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1 -Type DWord
        Ok 'Windows Update will not restart the PC by itself while you are logged on'
    }

    # -- registry: install record + Run fallback ------------------------------
    $RegKey = 'HKCU:\Software\Karlon'
    New-Item -Path $RegKey -Force | Out-Null
    Set-ItemProperty -Path $RegKey -Name 'InstallDir' -Value $Here
    Set-ItemProperty -Path $RegKey -Name 'Python'     -Value $Python
    Set-ItemProperty -Path $RegKey -Name 'PythonW'    -Value $PythonW
    Set-ItemProperty -Path $RegKey -Name 'Mode'       -Value $Mode
    Set-ItemProperty -Path $RegKey -Name 'StateDir'   -Value $StateDir
    Set-ItemProperty -Path $RegKey -Name 'Installed'  -Value (Get-Date).ToString('s')
    $Supervisor = Join-Path $Here 'karlon_supervisor.py'
    $RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if ($Mode -ne 'Boot') {
        # If the Task Scheduler is ever off, this still starts Karlon at logon;
        # when both start, the lock lets only one run.
        Set-ItemProperty -Path $RunKey -Name 'Karlon' -Value "`"$PythonW`" `"$Supervisor`" --service --takeover"
        Ok 'registry Run entry added (fallback start at logon)'
    } else {
        Remove-ItemProperty -Path $RunKey -Name 'Karlon' -ErrorAction SilentlyContinue
    }

    # -- scheduled tasks ------------------------------------------------------
    function New-EventTrigger([string]$Log, [string]$Filter) {
        $cls = Get-CimClass -Namespace 'ROOT\Microsoft\Windows\TaskScheduler' -ClassName 'MSFT_TaskEventTrigger'
        $t = New-CimInstance -CimClass $cls -ClientOnly
        $t.Enabled = $true
        $t.Delay = 'PT20S'                       # let the connection / resume settle
        $t.Subscription = "<QueryList><Query Id=`"0`" Path=`"$Log`"><Select Path=`"$Log`">*[System[$Filter]]</Select></Query></QueryList>"
        return $t
    }
    function New-Every5MinTrigger {
        # Daily trigger repeating every 5 min for 24 h = every 5 min forever.
        # A safety net: with a copy already running it exits in a fraction of a second.
        $t = New-ScheduledTaskTrigger -Daily -At '00:00'
        $t.Repetition = (New-ScheduledTaskTrigger -Once -At '00:00' -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
        return $t
    }
    $common = @(
        (New-Every5MinTrigger),
        (New-EventTrigger 'Microsoft-Windows-NetworkProfile/Operational' 'EventID=10000'),
        (New-EventTrigger 'System' "Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1")
    )
    # Waking a sleeping PC every 5 minutes only makes sense when it may never
    # sleep anyway: with -KeepAwakeOnBattery. Plugged in, it never sleeps.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -DontStopOnIdleEnd -WakeToRun:$KeepAwakeOnBattery -MultipleInstances IgnoreNew -Priority 5 `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

    function Register-Karlon([string]$Name, [string]$Arguments, $Triggers, $Principal, [string]$Description) {
        $action = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments -WorkingDirectory $Here
        Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Action $action -Trigger $Triggers -Settings $settings `
            -Principal $Principal -Description $Description -Force | Out-Null
        Ok "scheduled task $TaskPath$Name"
    }
    $quoted = "`"$Supervisor`""
    foreach ($old in @('Karlon Logon', 'Karlon Boot')) {
        Unregister-ScheduledTask -TaskName $old -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
    }
    if ($Mode -ne 'Boot') {
        $p = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
        Register-Karlon 'Karlon Logon' "$quoted --service --takeover" (@(New-ScheduledTaskTrigger -AtLogOn -User $User) + $common) $p `
            'Karlon on your desktop: WhatsApp bridge, invoices, Airbnb scraper. Takes over from the boot copy at logon.'
    }
    if ($Mode -ne 'Logon') {
        $p = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
        Register-Karlon 'Karlon Boot' "$quoted --service" (@(New-ScheduledTaskTrigger -AtStartup) + $common) $p `
            'Karlon from power-on, before anyone logs on (hidden). Hands over to Karlon Logon when you log on.'
    }

    # -- start now ------------------------------------------------------------
    if (-not $NoStart) {
        $first = if ($Mode -eq 'Boot') { 'Karlon Boot' } else { 'Karlon Logon' }
        Start-ScheduledTask -TaskPath $TaskPath -TaskName $first
        Start-Sleep -Seconds 8
        $beat = Join-Path $StateDir 'supervisor.json'
        if (Test-Path $beat) {
            $b = Get-Content $beat -Raw | ConvertFrom-Json
            Ok "running: supervisor pid $($b.pid), mode $($b.mode), Windows session $($b.session)"
        } else {
            Warn 'started, but no heartbeat yet - check in a minute with .\karlon_status.ps1'
        }
    }

    Say "`nDone. Karlon now starts by itself at power-on and logon, and comes back after crashes, Wi-Fi drops and sleep." 'Cyan'
    Say '  See what it is doing:   .\karlon_status.ps1          (live log: .\karlon_status.ps1 -Follow)'
    Say '  Restart its processes:  .\karlon_status.ps1 -Restart'
    Say '  Pause / resume:         .\karlon_status.ps1 -Pause   /  -Resume'
    Say '  Remove everything:      .\uninstall_karlon.ps1'
    Say '  The first time, scan the WhatsApp QR code in the browser window that opens.'
    Say "  Karlon's power request shows in:  powercfg /requests"
}
catch {
    Write-Host "`nInstall failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Details: $(Join-Path $StateDir 'install.log')" -ForegroundColor Red
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
