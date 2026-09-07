# Set a Windows machine up as a Printvendo kiosk, in one command.
#
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Code dve_xxxx
#
# Run it in an Administrator PowerShell: it installs a service.
#
# Add -Printer "<name>" when the machine has more than one printer. The names
# are what `Get-Printer | Select-Object Name` prints.
param(
    [Parameter(Mandatory = $true)][string]$Code,
    [string]$Api = "https://api.printvendo.com",
    [string]$Printer = "",
    # A xerox counter runs several machines off one agent. Repeatable, and the
    # order is the order they are preferred in when both are idle.
    #   -Bw 'Mono-1','Mono-2' -Colour 'Colour-1'
    [string[]]$Bw = @(),
    [string[]]$Colour = @(),
    # The shop screen. Passing a PIN sets one up: a desktop shortcut that opens
    # the locked display, and the PIN that leaves it. Without this nothing about
    # the screen is installed and the kiosk prints exactly as before.
    [string]$Pin = ""
)

$ErrorActionPreference = "Stop"
# GitHub refuses anything below TLS 1.2, and Windows PowerShell 5.1 on an
# older build still negotiates 1.0 by default -- which arrives as a connection
# error and reads as "this shop has no internet".
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$root = "$env:ProgramFiles\Printvendo"
$exe = "$root\venv\Scripts\printvendo-agent.exe"

function Need($what, $fix) {
    Write-Host "  missing: $what" -ForegroundColor Yellow
    Write-Host "           $fix"
}

# The agent runs as SYSTEM, so what SYSTEM can see is the only list that
# matters. A printer added under one user account is invisible to it, and the
# failure that produces -- an enrolled kiosk that claims jobs and cannot print
# them -- is silent, which is why this is checked at install rather than
# discovered by a student.
function Invoke-AsSystem($arguments) {
    # Written under C:\Windows\Temp, not the installing admin's temp folder:
    # SYSTEM has to be able to write it.
    $out = Join-Path $env:SystemRoot "Temp\printvendo-probe.txt"
    Remove-Item $out -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"`"$exe`" $arguments > `"$out`" 2>&1 & echo EXIT:%ERRORLEVEL% >> `"$out`"`""
    Register-ScheduledTask -TaskName "PrintvendoProbe" -Action $action `
        -User "SYSTEM" -RunLevel Highest -Force | Out-Null
    try {
        Start-ScheduledTask -TaskName "PrintvendoProbe"
        # Waited on by the marker the command itself writes, not by the task's
        # State: a task polled the instant after Start is still "Ready", and a
        # wait that ends there reads a file that does not exist yet.
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            if ((Test-Path $out) -and (Select-String -Path $out -Pattern "EXIT:" -Quiet)) { break }
            Start-Sleep -Milliseconds 300
        }
    } finally {
        Unregister-ScheduledTask -TaskName "PrintvendoProbe" -Confirm:$false
    }
    if (-not (Test-Path $out)) { return @{ Lines = @(); Code = 1 } }
    $lines = @(Get-Content $out)
    $code = 1
    $marker = $lines | Where-Object { $_ -like "EXIT:*" } | Select-Object -Last 1
    if ($marker) { $code = [int]($marker -replace "EXIT:", "") }
    return @{ Lines = @($lines | Where-Object { $_ -notlike "EXIT:*" }); Code = $code }
}

# Run a native command whose *failure is an answer*, not an error.
#
# Windows PowerShell 5.1 turns a native command's stderr into ErrorRecords the
# moment it is redirected, and `$ErrorActionPreference = "Stop"` above makes
# those terminating. So a probe written as `& python -c ... 2>$null` does not
# return "there is no python" when there is none -- it kills the script.
#
# It did. The Microsoft Store's python.exe stub writes "Python was not found"
# to stderr and exits 9, so this installer died on the very line meant to
# discover that and install Python -- on a shop PC, with a message pointing at
# a line of PowerShell rather than at anything an installer could act on.
# Reproduced on 5.1.22621 before this was written, and again after.
function Invoke-Native {
    param([scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command 2>$null } finally { $ErrorActionPreference = $previous }
}

# Refresh PATH from the registry, so something installed a moment ago in this
# same script is visible without reopening PowerShell. A process inherits its
# environment at start and never hears about a change -- which is why every set
# of install instructions ends with "close and reopen the terminal", and why
# somebody who does not gets a confusing failure instead of a working agent.
function Sync-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user | Where-Object { $_ }) -join ";"
}

function Have-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { return $null }
    # Anything under WindowsApps is unusable here, for two separate reasons: it
    # is either the Microsoft Store's stub, which is not Python at all and does
    # nothing but open the Store, or a Store install, which is per-user and so
    # invisible to the SYSTEM account the agent runs as. Neither is worth
    # probing -- and probing the stub is what used to end this script.
    if ($python.Source -like "*\WindowsApps\*") { return $null }
    $v = Invoke-Native { & python -c "import sys; print('%s.%s' % sys.version_info[:2])" }
    if (-not $v) { return $null }
    if ([version]$v -lt [version]"3.11") { return $null }
    return $v
}

function Have-Ghostscript {
    $onPath = Get-Command gswin64c, gswin32c -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    # Its installer does not add itself to PATH, so a perfectly good install is
    # invisible to Get-Command. The agent looks here too, for the same reason.
    $found = Get-ChildItem "$env:ProgramFiles\gs", "${env:ProgramFiles(x86)}\gs" `
        -Filter gswin*c.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

# Ghostscript is not on winget, and has not been since Artifex's package was
# dropped from the community repo: `winget install --id ArtifexSoftware.GhostScript`
# answers "No package found matching input criteria" -- on every Windows kiosk,
# at the step that was meant to be the automatic one. Checked on 2026-09-05;
# the only thing left under that publisher is ArtifexSoftware.mutool.
#
# So it comes from Artifex's own releases. The newest is asked for rather than
# pinned, because a pinned version is exactly what rotted here before -- and
# the pin below is only the fallback for a machine that cannot reach the API.
$GhostscriptFallback = "https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10071/gs10071w64.exe"

function Install-Ghostscript {
    $url = $GhostscriptFallback
    try {
        $latest = Invoke-RestMethod -UseBasicParsing `
            -Uri "https://api.github.com/repos/ArtifexSoftware/ghostpdl-downloads/releases/latest"
        $asset = $latest.assets | Where-Object { $_.name -match '^gs\d+w64\.exe$' } | Select-Object -First 1
        if ($asset) { $url = $asset.browser_download_url }
    } catch {
        Write-Host "    (could not ask which is newest; using $(Split-Path $GhostscriptFallback -Leaf))"
    }

    $setup = Join-Path $env:TEMP "printvendo-ghostscript.exe"
    # 65 MB. PowerShell 5.1 renders a progress bar per chunk, which costs more
    # time than the download on a shop connection -- an order of magnitude, and
    # it is the difference between a minute and a quarter of an hour.
    $progress = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing
    } finally {
        $ProgressPreference = $progress
    }

    # NSIS: /S is silent. No /D, deliberately -- the default C:\Program Files\gs
    # is where Have-Ghostscript looks and where the agent looks, and a custom
    # directory would have to be taught to both.
    $run = Start-Process -FilePath $setup -ArgumentList "/S" -Wait -PassThru
    Remove-Item $setup -ErrorAction SilentlyContinue
    return $run.ExitCode
}

# ── is this actually an Administrator shell? ────────────────────────────────
#
# Checked first, because a run without it does not fail cleanly: it installs
# the agent, enrols the kiosk, registers the task, and *then* throws a raw
# .NET UnauthorizedAccessException at the registry write -- so the operator has
# a half-configured shop and a stack trace. It happened.
#
# Worse, a re-install can get further than a first install: `C:\Program Files\
# Printvendo` already exists from the elevated run, so the unprivileged one
# writes into it happily and looks like it worked.
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This needs an Administrator PowerShell." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Start, type powershell, right-click Windows PowerShell,"
    Write-Host "  then Run as administrator -- and run this again."
    Write-Host ""
    Write-Host "Without it the agent installs, the kiosk enrols, and the setup"
    Write-Host "fails at the end with the shop half configured."
    exit 1
}

Write-Host "==> Checking what is installed"
Sync-Path

$winget = Get-Command winget -ErrorAction SilentlyContinue

$version = Have-Python
if (-not $version) {
    if (-not $winget) {
        Need "Python 3.11+" "winget is not on this machine. Install Python from python.org, ticking 'Add python.exe to PATH'."
        exit 1
    }
    Write-Host "    installing Python (this takes a few minutes)"
    # --scope machine so the service account can see it: this runs as SYSTEM,
    # and a per-user Python is invisible to it.
    # Wrapped for the same reason as the probe above: winget writes notices to
    # stderr -- a source agreement, a pending upgrade -- and under `Stop` one of
    # those ends the install rather than being the sentence it is.
    Invoke-Native { & winget install --id Python.Python.3.12 --scope machine --silent `
        --accept-package-agreements --accept-source-agreements } | Out-Null
    Sync-Path
    $version = Have-Python
    if (-not $version) {
        Need "Python 3.11+" "winget ran but Python is still not usable. Install it from python.org, ticking 'Add python.exe to PATH', then run this again."
        exit 1
    }
}

# Printing goes through Ghostscript. It takes every option -- colour, duplex,
# copies, page range -- which the Windows print verb does not.
$gsPath = Have-Ghostscript
if (-not $gsPath) {
    Write-Host "    installing Ghostscript (65 MB, a few minutes)"
    try {
        $code = Install-Ghostscript
    } catch {
        Need "Ghostscript" "could not fetch it: $($_.Exception.Message). Install it by hand from https://ghostscript.com/releases/gsdnld.html, then run this again."
        exit 1
    }
    Sync-Path
    $gsPath = Have-Ghostscript
    if (-not $gsPath) {
        Need "Ghostscript" "the installer ran (exit $code) but Ghostscript is still not there. Install it by hand from https://ghostscript.com/releases/gsdnld.html, then run this again."
        exit 1
    }
}

Write-Host "    python $version, $(Split-Path $gsPath -Leaf)"

Write-Host "==> Installing the agent"
New-Item -ItemType Directory -Force -Path $root | Out-Null
& python -m venv "$root\venv"
# Unchecked, a failed venv arrives two lines later as "pip.exe is not
# recognised", which reads as a missing file rather than as the thing that was
# never made.
if ($LASTEXITCODE -ne 0) {
    Need "a usable Python" "python -m venv failed. Install Python 3.12 from python.org, ticking 'Add python.exe to PATH', then run this again."
    exit 1
}
# Through the venv's python, never pip.exe: on Windows pip refuses to replace
# its own running executable and answers "ERROR: To modify pip, please run the
# following command". Nothing checked that line's exit code, so the install
# carried on correctly -- while printing ERROR at somebody standing at a shop
# counter, which is indistinguishable from the install having failed.
#
# --disable-pip-version-check for the same reason: a "[notice] A new release of
# pip is available" is true, irrelevant here, and reads as a problem.
& "$root\venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip
# $($PSScriptRoot) in braces: "$PSScriptRoot[windows]" is parsed by PowerShell
# as an index into the path string, which installs nothing and says nothing.
& "$root\venv\Scripts\pip.exe" install --quiet --disable-pip-version-check "$($PSScriptRoot)[windows]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Checking the printer"
$probe = Invoke-AsSystem "printers"
$visible = $probe.Lines
if ($probe.Code -ne 0) {
    Write-Host "  The service account cannot see any printer." -ForegroundColor Yellow
    Write-Host "  Printers added for one user only are invisible to it. Add the printer"
    Write-Host "  for the whole machine (Settings > Bluetooth & devices > Printers), or"
    Write-Host "  for a shared one run, as Administrator:"
    Write-Host "      Add-Printer -ConnectionName '\server\printer'"
    exit 1
}
$visible | ForEach-Object { Write-Host "    $_" }

if ($Printer -and ($visible -notcontains $Printer)) {
    Write-Host "  '$Printer' is not in that list." -ForegroundColor Yellow
    Write-Host "  Pass one of the names above with -Printer, exactly as printed."
    exit 1
}
if (-not $Printer -and $visible.Count -gt 1) {
    # Guessing between two printers means somebody's dissertation on the label
    # machine.
    Write-Host "  This machine has more than one printer. Run again with -Printer '<name>'," -ForegroundColor Yellow
    Write-Host "  or split them: -Bw 'Mono-1','Mono-2' -Colour 'Colour-1'"
    exit 1
}

Write-Host "==> Enrolling this machine"
$enrol = @("enrol", "--code", $Code, "--api", $Api)
if ($Printer) { $enrol += @("--printer", $Printer) }
foreach ($name in $Bw) { $enrol += @("--bw", $name) }
foreach ($name in $Colour) { $enrol += @("--colour", $name) }
& $exe @enrol
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Installing the service"
# A scheduled task rather than a Windows service: a service needs a wrapper
# (NSSM or pywin32's service host) and this needs neither, starts at boot
# without anybody logging in, and restarts on failure.
# The executable directly, never through `cmd.exe`.
#
# The log used to be captured by `cmd /c "$exe" run >> agent.log`, which
# attaches the agent to whichever console started the task -- so a Ctrl+C in the
# installer's own PowerShell window, or just closing it, killed the shop's agent
# with STATUS_CONTROL_C_EXIT. That happened on a live kiosk. The agent writes
# its own file now (`_start_logging`), so there is nothing to redirect and no
# console to inherit.
$action = New-ScheduledTaskAction -Execute $exe -Argument "run"

# At logon rather than at startup, and as the logged-in user rather than SYSTEM.
#
# Ghostscript's mswinpr2 device needs an interactive window station: as SYSTEM
# in session 0 it blocks for ever without ever reaching the spooler, so a kiosk
# claimed jobs, downloaded them and printed nothing. Until printing goes through
# PCL and the raw spooler, the agent has to live in a real session.
#
# The consequence is stated rather than hidden: this machine must log in
# automatically, or the shop does not come back after a power cut.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Hidden

$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "PrintvendoAgent" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

# Stop before start, because Start-ScheduledTask does nothing to a task that is
# already running -- and enrolling above has just rotated this kiosk's token, so
# an already-running agent is holding one the server invalidated a moment ago.
# It would go on polling with it and get 401 on every call for ever, while
# `check` below reads the *file*, finds the new token, and reports the kiosk
# healthy. That is exactly how two shops stopped printing on the Pi side.
Stop-ScheduledTask -TaskName "PrintvendoAgent" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "PrintvendoAgent"

Write-Host "==> Checking it end to end"
$ready = Invoke-AsSystem "check"
$ready.Lines | ForEach-Object { Write-Host "    $_" }
if ($ready.Code -ne 0) {
    Write-Host "The kiosk is not ready. Fix the above and run: `"$exe`" check" -ForegroundColor Yellow
    exit 1
}

# ── the screen above the counter ───────────────────────────────────────────
#
# Optional, and last: a shop with no second monitor is a working kiosk, and
# nothing here may be able to stop one printing.
if ($Pin) {
    Write-Host "==> Setting up the shop screen"

    $display = "$root\venv\Scripts\printvendo-display.exe"
    if (-not (Test-Path $display)) {
        Need "the display" "the agent installed but printvendo-display.exe is missing. Re-run this installer."
    }
    else {
        & $display --set-pin $Pin | Out-Null

        # A shortcut rather than a startup entry, deliberately. A display that
        # launched itself at boot on a PC with one monitor would cover the
        # shop's own desktop, and the person who needs it gone is the person
        # who has just lost their screen.
        $link = Join-Path ([Environment]::GetFolderPath("CommonDesktopDirectory")) "Printvendo screen.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $display
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = "The locked shop screen. Ctrl+Alt+U and the PIN to leave it."
        $shortcut.Save()

        # Closes the useful half of the Ctrl+Alt+Del door. The other half --
        # Ctrl+Alt+Del itself -- is reserved by Windows and cannot be blocked
        # from user space by anything, which is stated here rather than
        # discovered at a counter.
        # Closes the useful half of the Ctrl+Alt+Del door, and is the one step
        # here the shop can live without -- so it reports rather than throws.
        # Ctrl+Alt+Del itself is reserved by Windows and cannot be blocked from
        # user space by anything.
        $policy = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
        try {
            if (-not (Test-Path $policy)) { New-Item -Path $policy -Force | Out-Null }
            Set-ItemProperty -Path $policy -Name DisableTaskMgr -Value 1 -Type DWord -ErrorAction Stop
        }
        catch {
            Write-Host "    could not disable Task Manager: $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "    the screen still locks; a determined student has one more way out"
        }

        Write-Host "    shortcut on the desktop: Printvendo screen"
        Write-Host "    Ctrl+Alt+U and the PIN leaves it"
    }
}

Write-Host ""
Write-Host "Done. The kiosk is printing." -ForegroundColor Green
Write-Host "  Check it:  & '$exe' check"
Write-Host "  Watch it:  Get-ScheduledTask PrintvendoAgent"
Write-Host "  Stop it:   Stop-ScheduledTask -TaskName PrintvendoAgent"
