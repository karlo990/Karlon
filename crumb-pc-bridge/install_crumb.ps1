<#
.SYNOPSIS
  Makes the Crumb WhatsApp bridge an always-on part of Windows: starts by
  itself, survives Wi-Fi drops, sleep, a closed lid and crashes, and keeps its
  browser and memory clean. Crumb answers the WhatsApp messages; this bridge
  carries them between WhatsApp Web and the Sunday Crumb Space.

.DESCRIPTION
  WhatsApp Web needs a person once: scan the QR with Crumb's phone. Do that
  with  python crumb_login.py  (a visible window, press Enter when done; the
  login is saved in ~/.crumb). After that it runs HEADLESS - no window, in
  the background - inside your signed-in Windows session.

  Runs next to Karlon's bridge on the same PC: its own task folder (\Crumb\),
  its own folder (~/.crumb), its own browser profile and settings. Installing
  or removing one never stops the other.

  The Space only talks to a bridge that sends its token: give the same value
  as the Space's CRUMB_BRIDGE_TOKEN secret with -BridgeToken (saved for your
  user as CRUMB_BRIDGE_TOKEN), or type it when asked.

  What it sets up (everything is undone by uninstall_crumb.ps1):

  Task Scheduler, folder \Crumb\
    "Crumb"       starts at sign-in, in your session. Restarted 1 min after
                   any crash (999 times), no time limit, runs on battery,
                   re-checked every 5 minutes, started again when the network
                   (re)connects or the PC wakes. Only one copy ever runs.
    "Crumb Lock"  with -AutoLogon: locks the screen after an automatic
                   sign-in at power-on.
    (An old "Crumb Boot" task from an earlier version is removed.)

  Power plan (current plan, values saved first)
    Closing the lid: do nothing (plugged in and on battery).
    Sleep / hibernate when plugged in: never. On battery too with
    -KeepAwakeOnBattery. While Crumb runs it also blocks idle sleep itself.
    Wi-Fi adapter: maximum performance; Windows may not switch it off.

  Registry
    HKCU\Software\Crumb             install folder, Python.
    HKCU\...\CurrentVersion\Run      "Crumb" fallback start at sign-in.
    HKLM ...\Session Manager\Power   HiberbootEnabled=0 (Fast Startup off).
    HKLM ...\WindowsUpdate\AU        NoAutoRebootWithLoggedOnUsers=1.
    HKLM ...\Winlogon                with -AutoLogon: automatic sign-in after
                                     a restart; the password is kept encrypted
                                     by Windows (LSA secret), never in a file.

.EXAMPLE
  python crumb_login.py
  powershell -ExecutionPolicy Bypass -File .\install_crumb.ps1 -BridgeToken "the-same-token-as-the-Space"
#>
[CmdletBinding()]
param(
    [switch]$AutoLogon,
    [switch]$NoLockAfterAutoLogon,
    [string]$Python,
    [string]$CrumbUrl,
    [string]$BridgeToken,
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
    Write-Host 'Crumb needs Administrator rights to install - asking Windows...' -ForegroundColor Yellow
    $pass = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($kv in $PSBoundParameters.GetEnumerator()) {
        if ($kv.Value -is [switch]) { if ($kv.Value) { $pass += "-$($kv.Key)" } }
        else { $pass += "-$($kv.Key)"; $pass += "`"$($kv.Value)`"" }
    }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $pass
    return
}

$Here      = $PSScriptRoot
$StateDir  = if ($env:CRUMB_STATE_DIR) { $env:CRUMB_STATE_DIR } else { Join-Path $HOME '.crumb' }
$TaskPath  = '\Crumb\'
$User      = "$env:USERDOMAIN\$env:USERNAME"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
Start-Transcript -Path (Join-Path $StateDir 'install.log') -Append | Out-Null

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }

. (Join-Path $PSScriptRoot 'crumb_lsa.ps1')     # [CrumbLsa]::Set - the auto sign-in password store

try {
    if (-not (Test-Path (Join-Path $Here 'crumb_supervisor.py'))) {
        throw "crumb_supervisor.py isn't next to this script ($Here). Run it from the pc-bridge folder."
    }
    Say "`nCrumb always-on install  -  folder $Here" 'Cyan'

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
    if ($Python -match 'WindowsApps') { Warn 'This is the Microsoft Store Python; python.org Python is recommended for scheduled tasks.' }
    Ok "Python: $Python"

    if (-not $SkipPip) {
        Say '  installing Python packages (requirements.txt)...'
        & $Python -m pip install --quiet --disable-pip-version-check -r (Join-Path $Here 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { Warn 'pip reported a problem; Crumb still runs, but check the packages.' } else { Ok 'packages installed (incl. psutil for memory limits)' }
    }
    if (-not $BridgeToken -and -not [Environment]::GetEnvironmentVariable('CRUMB_BRIDGE_TOKEN', 'User')) {
        # read hidden, so the token stays out of the screen and install.log
        $sec = Read-Host 'Bridge token (the CRUMB_BRIDGE_TOKEN secret on the Space)' -AsSecureString
        $BridgeToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
    if ($BridgeToken) {
        [Environment]::SetEnvironmentVariable('CRUMB_BRIDGE_TOKEN', $BridgeToken.Trim(), 'User')
        $env:CRUMB_BRIDGE_TOKEN = $BridgeToken.Trim()
        Ok 'CRUMB_BRIDGE_TOKEN saved for your user'
    } elseif (-not [Environment]::GetEnvironmentVariable('CRUMB_BRIDGE_TOKEN', 'User')) {
        Warn 'no bridge token: the Space will refuse the bridge until CRUMB_BRIDGE_TOKEN is set'
    }
    if ($CrumbUrl) {
        [Environment]::SetEnvironmentVariable('CRUMB_URL', $CrumbUrl.TrimEnd('/'), 'User')
        Ok "CRUMB_URL saved for your user: $CrumbUrl"
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
    $WinlogonKey  = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    $PwLessKey    = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
    if (-not (Test-Path $BackupFile)) {
        $backup = [ordered]@{ Saved = (Get-Date).ToString('s'); Power = [ordered]@{}; Adapters = @() }
        foreach ($k in $PowerKeys.Keys) { $pk = $PowerKeys[$k]; $backup.Power[$k] = Get-PowerIndex $pk[0] $pk[1] }
        $backup.HiberbootEnabled = Get-RegValue $HiberbootKey 'HiberbootEnabled'
        $backup.NoAutoReboot     = Get-RegValue $AUKey 'NoAutoRebootWithLoggedOnUsers'
        $backup.AutoAdminLogon   = Get-RegValue $WinlogonKey 'AutoAdminLogon'
        $backup.DefaultUserName  = Get-RegValue $WinlogonKey 'DefaultUserName'
        $backup.DefaultDomainName = Get-RegValue $WinlogonKey 'DefaultDomainName'
        $backup.PasswordLess     = Get-RegValue $PwLessKey 'DevicePasswordLessBuildVersion'
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
        Ok 'Fast Startup off (every power-on is a clean start)'
    }
    if (-not $NoUpdateRebootBlock) {
        New-Item -Path $AUKey -Force | Out-Null
        Set-ItemProperty -Path $AUKey -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1 -Type DWord
        Ok 'Windows Update will not restart the PC by itself while you are signed in'
    }

    # -- automatic sign-in: the desktop the browsers need, after any restart --
    if ($AutoLogon) {
        Say "  Automatic sign-in: enter the password you use to sign in to Windows as $env:USERNAME"
        Say '  (for a Microsoft account, that account''s password - not the PIN).'
        $cred = Get-Credential -UserName $User -Message 'Windows password for automatic sign-in (stored encrypted by Windows)'
        if (-not $cred) { throw 'automatic sign-in cancelled - run again with -AutoLogon to set it up' }
        [CrumbLsa]::Set('DefaultPassword', $cred.GetNetworkCredential().Password)
        Set-ItemProperty -Path $WinlogonKey -Name 'AutoAdminLogon' -Value '1'
        Set-ItemProperty -Path $WinlogonKey -Name 'DefaultUserName' -Value $env:USERNAME
        Set-ItemProperty -Path $WinlogonKey -Name 'DefaultDomainName' -Value $env:USERDOMAIN
        Remove-ItemProperty -Path $WinlogonKey -Name 'DefaultPassword' -ErrorAction SilentlyContinue   # never in plain text
        Remove-ItemProperty -Path $WinlogonKey -Name 'AutoLogonCount' -ErrorAction SilentlyContinue    # not "N times only"
        if (Test-Path $PwLessKey) {
            # "Only allow Windows Hello sign-in" blocks password sign-in, so automatic sign-in too.
            Set-ItemProperty -Path $PwLessKey -Name 'DevicePasswordLessBuildVersion' -Value 0 -Type DWord
        }
        $cred = $null
        Ok 'automatic sign-in after power-on/restart (password kept encrypted by Windows)'
    }

    # -- registry: install record + Run fallback ------------------------------
    $RegKey = 'HKCU:\Software\Crumb'
    New-Item -Path $RegKey -Force | Out-Null
    Set-ItemProperty -Path $RegKey -Name 'InstallDir' -Value $Here
    Set-ItemProperty -Path $RegKey -Name 'Python'     -Value $Python
    Set-ItemProperty -Path $RegKey -Name 'PythonW'    -Value $PythonW
    Set-ItemProperty -Path $RegKey -Name 'StateDir'   -Value $StateDir
    Set-ItemProperty -Path $RegKey -Name 'AutoLogon'  -Value ([int][bool]$AutoLogon)
    Set-ItemProperty -Path $RegKey -Name 'Installed'  -Value (Get-Date).ToString('s')
    $Supervisor = Join-Path $Here 'crumb_supervisor.py'
    # If the Task Scheduler is ever off, this still starts Crumb at sign-in;
    # when both start, the lock lets only one run.
    Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Crumb' `
        -Value "`"$PythonW`" `"$Supervisor`" --service --takeover"
    Ok 'registry Run entry added (fallback start at sign-in)'

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
    # Waking a sleeping PC every 5 minutes only makes sense when it may never
    # sleep anyway: with -KeepAwakeOnBattery. Plugged in, it never sleeps.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -DontStopOnIdleEnd -WakeToRun:$KeepAwakeOnBattery -MultipleInstances IgnoreNew -Priority 5 `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    # Interactive = the signed-in desktop, where a login window can show.
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

    # A copy started by an older task (e.g. "Crumb Boot", before sign-in) must
    # not keep running: stop every Crumb task first (its browsers die with it).
    Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
    foreach ($old in @('Crumb', 'Crumb Logon', 'Crumb Boot', 'Crumb Lock')) {
        Unregister-ScheduledTask -TaskName $old -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
    }
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $User),
        (New-Every5MinTrigger),
        (New-EventTrigger 'Microsoft-Windows-NetworkProfile/Operational' 'EventID=10000'),
        (New-EventTrigger 'System' "Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1")
    )
    $action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Supervisor`" --service --takeover" -WorkingDirectory $Here
    Register-ScheduledTask -TaskName 'Crumb' -TaskPath $TaskPath -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Force `
        -Description 'Crumb WhatsApp bridge: carries WhatsApp messages to the Sunday Crumb Space and sends Crumb''s replies.' | Out-Null
    Ok "scheduled task $($TaskPath)Crumb"

    if ($AutoLogon -and -not $NoLockAfterAutoLogon) {
        # Lock only a sign-in that happened by itself right after power-on
        # (within 10 min of boot); signing in yourself later is left alone.
        $lockCmd = '$up = (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime; ' +
                   'if ($up.TotalMinutes -lt 10) { & rundll32.exe ''user32.dll,LockWorkStation'' }'
        $lockAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$lockCmd`""
        $lockTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
        $lockTrigger.Delay = 'PT40S'           # the browsers open first
        $lockSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
        Register-ScheduledTask -TaskName 'Crumb Lock' -TaskPath $TaskPath -Action $lockAction -Trigger $lockTrigger `
            -Settings $lockSettings -Principal (New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive) -Force `
            -Description 'Locks the screen after an automatic sign-in at power-on; Crumb keeps running behind the lock.' | Out-Null
        Ok "scheduled task $($TaskPath)Crumb Lock (screen locks 40 s after an automatic sign-in)"
    }

    # -- start now ------------------------------------------------------------
    if (-not $NoStart) {
        Start-ScheduledTask -TaskPath $TaskPath -TaskName 'Crumb'
        Start-Sleep -Seconds 8
        $beat = Join-Path $StateDir 'supervisor.json'
        if (Test-Path $beat) {
            $b = Get-Content $beat -Raw | ConvertFrom-Json
            Ok "running: supervisor pid $($b.pid), Windows session $($b.session)"
        } else {
            Warn 'started, but no heartbeat yet - check in a minute with .\crumb_status.ps1'
        }
    }

    if ($AutoLogon) {
        Say "`nDone. After a restart Windows signs in by itself, locks the screen, and Crumb runs headless behind it." 'Cyan'
    } else {
        Say "`nDone. Crumb runs headless whenever you are signed in." 'Cyan'
        Warn 'After a restart it waits for you to sign in. To have it start by itself, run again with -AutoLogon.'
    }
    $waOk = Test-Path (Join-Path $HOME '.crumb\wa_session\crumb_logged_in')
    if (-not $waOk) { Warn 'WhatsApp login not saved yet - run  python crumb_login.py  once so it runs headless.' }
    Say '  It comes back by itself after crashes, Wi-Fi drops, sleep and with the lid closed.'
    Say '  See what it is doing:   .\crumb_status.ps1          (live log: .\crumb_status.ps1 -Follow)'
    Say '  Restart its processes:  .\crumb_status.ps1 -Restart'
    Say '  Pause / resume:         .\crumb_status.ps1 -Pause   /  -Resume'
    Say '  Remove everything:      .\uninstall_crumb.ps1'
    Say '  Log in again (WhatsApp QR):  python crumb_login.py'
}
catch {
    Write-Host "`nInstall failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Details: $(Join-Path $StateDir 'install.log')" -ForegroundColor Red
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
