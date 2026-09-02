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
    [string[]]$Colour = @()
)

$ErrorActionPreference = "Stop"
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

Write-Host "==> Checking what is installed"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Need "Python" "Install Python 3.11+ from python.org, ticking 'Add to PATH'."
    exit 1
}
# The Microsoft Store stub is on PATH as `python` and does nothing but open the
# Store. It looks exactly like Python until the venv fails.
$version = & python -c "import sys; print('%s.%s' % sys.version_info[:2])" 2>$null
if (-not $version) {
    Need "a working Python" "`python` on PATH is the Microsoft Store stub. Install Python 3.11+ from python.org."
    exit 1
}
if ([version]$version -lt [version]"3.11") {
    Need "Python 3.11+" "Found $version. Install a newer one from python.org."
    exit 1
}

# Printing goes through Ghostscript. It takes every option -- colour, duplex,
# copies, page range -- which the Windows print verb does not.
$gs = Get-Command gswin64c, gswin32c -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $gs) {
    Need "Ghostscript" "Install it from ghostscript.com/releases, then reopen PowerShell."
    exit 1
}
Write-Host "    python $version, $($gs.Name)"

Write-Host "==> Installing the agent"
New-Item -ItemType Directory -Force -Path $root | Out-Null
& python -m venv "$root\venv"
& "$root\venv\Scripts\pip.exe" install --quiet --upgrade pip
# $($PSScriptRoot) in braces: "$PSScriptRoot[windows]" is parsed by PowerShell
# as an index into the path string, which installs nothing and says nothing.
& "$root\venv\Scripts\pip.exe" install --quiet "$($PSScriptRoot)[windows]"
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
$action = New-ScheduledTaskAction -Execute $exe -Argument "run"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "PrintvendoAgent" -Action $action -Trigger $trigger `
    -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null

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

Write-Host ""
Write-Host "Done. The kiosk is printing." -ForegroundColor Green
Write-Host "  Check it:  & '$exe' check"
Write-Host "  Watch it:  Get-ScheduledTask PrintvendoAgent"
Write-Host "  Stop it:   Stop-ScheduledTask -TaskName PrintvendoAgent"
