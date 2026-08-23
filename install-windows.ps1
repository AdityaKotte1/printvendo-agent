# Set a Windows machine up as a Printvendo kiosk, in one command.
#
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Code dve_xxxx
#
# Run it in an Administrator PowerShell: it installs a service.
param(
    [Parameter(Mandatory = $true)][string]$Code,
    [string]$Api = "https://api.printvendo.com",
    [string]$Printer = ""
)

$ErrorActionPreference = "Stop"

function Need($what, $fix) {
    Write-Host "  missing: $what" -ForegroundColor Yellow
    Write-Host "           $fix"
}

Write-Host "==> Checking what is installed"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Need "Python" "Install Python 3.11+ from python.org, ticking 'Add to PATH'."
    exit 1
}

# Printing goes through Ghostscript. It takes every option -- colour, duplex,
# copies, page range -- which the Windows print verb does not.
$gs = Get-Command gswin64c, gswin32c -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $gs) {
    Need "Ghostscript" "Install it from ghostscript.com/releases, then reopen PowerShell."
    exit 1
}

Write-Host "==> Installing the agent"
$root = "$env:ProgramFiles\Printvendo"
New-Item -ItemType Directory -Force -Path $root | Out-Null
python -m venv "$root\venv"
& "$root\venv\Scripts\pip.exe" install --quiet --upgrade pip
& "$root\venv\Scripts\pip.exe" install --quiet "$PSScriptRoot[windows]"

Write-Host "==> Enrolling this machine"
$enrol = @("enrol", "--code", $Code, "--api", $Api)
if ($Printer) { $enrol += @("--printer", $Printer) }
& "$root\venv\Scripts\printvendo-agent.exe" @enrol
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Installing the service"
# A scheduled task rather than a Windows service: a service needs a wrapper
# (NSSM or pywin32's service host) and this needs neither, starts at boot
# without anybody logging in, and restarts on failure.
$action = New-ScheduledTaskAction -Execute "$root\venv\Scripts\printvendo-agent.exe" -Argument "run"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "PrintvendoAgent" -Action $action -Trigger $trigger `
    -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "PrintvendoAgent"

Write-Host ""
Write-Host "Done. The kiosk is printing." -ForegroundColor Green
Write-Host "  Check it:  & '$root\venv\Scripts\printvendo-agent.exe' check"
Write-Host "  Stop it:   Stop-ScheduledTask -TaskName PrintvendoAgent"
