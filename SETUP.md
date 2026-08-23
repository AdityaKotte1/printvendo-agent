# Setting a kiosk up

Two machines can be a kiosk: a Raspberry Pi, or a Windows PC. Both run the same
agent and behave identically. This page is the whole procedure, in order,
including the parts that happen on the backend.

Roughly ten minutes for the first one, three for the next.

---

## Before you start

You need, on the kiosk machine:

| | Raspberry Pi | Windows |
|---|---|---|
| OS | Raspberry Pi OS (Bookworm or later) | Windows 10 or 11 |
| Python | installed by the installer | **3.11+ from python.org** — see the warning below |
| Print engine | CUPS, installed by the installer | **Ghostscript**, from ghostscript.com/releases |
| Printer | already added to CUPS — see step 3 | already added to Windows |
| Network | can reach the backend over HTTPS | same |

> **Windows: do not use the Microsoft Store Python.** `python` on a fresh
> Windows PATH is a stub that opens the Store and exits. It looks like Python
> until the virtual environment fails. Install from python.org with **Add to
> PATH** ticked. The installer checks this and stops if it finds the stub.

The kiosk needs no fixed IP, no port forwarding and no inbound firewall rule.
It only ever makes outbound connections.

---

## 1. Make the kiosk on the backend

One command per physical machine. It creates the shop, prices it, stocks the
tray, and prints a one-time enrolment code.

```bash
.venv/Scripts/python -m app.cli provision-kiosk --name "Library Block A" --paper 500
```

Useful flags:

```
--type platform|sold|saas     who collects the money (default platform)
--owner-email name@shop.com   invite somebody to own it (sold/saas need one)
--location "Near the canteen"
--bw-single 2 --bw-double 3 --color-single 10 --color-double 20
--paper 500                   tray size in sheets
```

It ends with:

```
enrolment code (spend within 12h): dve_XXXXXXXX
```

That code is **one machine, one use, twelve hours**. A code left in a terminal
scrollback is worthless by morning. Need a second kiosk? Run the command again —
never reuse a code.

It also prints what is stopping the shop from selling, if anything. A PLATFORM
kiosk with paper is usually ready at once; a SOLD or SAAS one waits for an owner
who can collect money.

---

## 2. Choose the printer

The agent needs the exact printer name, as the machine spells it.

**Windows** — in PowerShell:

```powershell
Get-Printer | Select-Object Name
```

**Pi** — over SSH:

```bash
lpstat -e
```

If exactly one printer comes back, you can skip the flag in step 4: a shop with
one printer is never asked which one. With several you must say, because
guessing means somebody's dissertation coming out of the label machine.

---

## 3. If the Pi has no printer yet

CUPS has to know the printer before the agent can use it. Plug it in, switch it
on, then over SSH:

```bash
lpinfo -v                                    # find its usb:// address
lpadmin -p Shop -E -v "usb://HP/LaserJet%20M1005?serial=XXXX" -m everywhere
lpstat -e                                    # Shop should now be listed
lp -d Shop /usr/share/cups/data/testprint    # and a page should come out
```

`-m everywhere` is driverless IPP and is right for almost any printer made since
about 2015. For an older one, `lpinfo -m | grep -i <model>` lists the drivers
this Pi already has.

`-p Shop` is the name you will pass in step 4. Keep it short and without spaces.

On Windows, add the printer in Settings the ordinary way — but see the note in
step 4 about who can see it.

---

## 4. Install the agent

Copy this folder onto the machine (`scp`, a USB stick, `git clone` — whatever
suits), then run one command.

### Raspberry Pi

```bash
sudo bash install-pi.sh \
  --code dve_XXXXXXXX \
  --api https://api.printvendo.com \
  --printer Shop
```

Installs Python, CUPS and Ghostscript if missing, enrols the machine, and leaves
a systemd unit that starts at boot and restarts on failure.

### Windows

In an **Administrator** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Code dve_XXXXXXXX -Api "https://api.printvendo.com" -Printer "HP LaserJet M1005"
```

Installs into `C:\Program Files\Printvendo`, enrols the machine, and registers a
scheduled task that starts at boot as SYSTEM and restarts on failure. (A
scheduled task rather than a service: a service needs a wrapper and this needs
none.)

> **Windows: the agent runs as SYSTEM, and SYSTEM has its own printer list.** A
> printer added under one user account — which is what happens with most network
> printers — is invisible to it. The installer enumerates printers *as SYSTEM*
> and refuses to finish if the one you named is not there, rather than leaving
> you to find out from the first student. If it stops there, add the printer for
> the whole machine, or run `Add-Printer -ConnectionName` as Administrator for a
> shared one.

Both installers finish by running `check` and fail on its result. "Done. The
kiosk is printing." means the machine really did reach the backend and really
can see its printer.

---

## 5. Confirm it

```bash
# Pi
/opt/printvendo/venv/bin/printvendo-agent check
journalctl -u printvendo-agent -f
```

```powershell
# Windows
& "C:\Program Files\Printvendo\venv\Scripts\printvendo-agent.exe" check
Get-ScheduledTask PrintvendoAgent
```

`check` answers one question — can this machine print a student's job right
now — and names the fix for every reason it cannot. It never prints a traceback:
an installer at a shop counter who has to read a stack trace rings somebody, and
the shop stays shut.

Then send a real job from the app and watch the student's screen go
**Queued → Printing → Printed**. Those three words are read off the printer's own
queue, so "Printing" means the printer has the job in its hands — not that the
agent sent it.

---

## Testing on a bench, before there is a server

The backend does not have to be deployed to test the whole path. Run it on your
laptop, bound to every interface so the kiosk machines can reach it:

```bash
.venv/Scripts/python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
ipconfig            # or `ip addr` — note the laptop LAN address
```

Then pass that address to the installers instead of the production one:

```
--api http://192.168.1.7:8000          # Pi
-Api "http://192.168.1.7:8000"         # Windows
```

Plain HTTP over a LAN is fine for a bench test and **not** fine for a shop: a
device token crosses that wire on every request. Production is HTTPS.

Both machines can point at the same laptop at once. Give each its own enrolment
code and they become two separate kiosks.

---

## Day to day

| | Pi | Windows |
|---|---|---|
| Watch it | `journalctl -u printvendo-agent -f` | `Get-ScheduledTask PrintvendoAgent` |
| Stop it | `sudo systemctl stop printvendo-agent` | `Stop-ScheduledTask -TaskName PrintvendoAgent` |
| Start it | `sudo systemctl start printvendo-agent` | `Start-ScheduledTask -TaskName PrintvendoAgent` |
| Settings | `/etc/printvendo/agent.json` (`0600`) | `C:\ProgramData\Printvendo\agent.json` |

**Upgrading** the agent: `pip install` this folder into the existing virtual
environment and restart the service. Do not re-run the installer with the same
code — an enrolment code is spent.

**Changing the printer** later: edit `printer` in the settings file and restart
the service, or re-enrol with a fresh code.

The settings file holds the device token, which **is** the kiosk — nothing the
agent sends says which kiosk it belongs to. Treat it as a password. Losing the
machine means provisioning a new kiosk, not copying the file.

---

## When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| `Enrolment failed: 400` | code spent, or older than twelve hours | provision again for a fresh one |
| `Could not reach https://...` | DNS, firewall, or the wrong `--api` | `curl <api>/health` from the machine |
| `The printer ... is not there any more` | renamed, unplugged, or per-user on Windows | `printvendo-agent printers` |
| Jobs claimed, nothing prints | Ghostscript missing or not on PATH | `gswin64c --version` |
| Kiosk shows offline | agent not running | check the service, then `check` |
| Options ignored (colour, duplex) | the driver, not the agent | print the same PDF by hand with the same settings |

`printvendo-agent printers` lists what the machine can see, in the platform's own
words. On Windows, run it from the installed virtual environment — running it as
yourself shows *your* printers, which is not the list the service uses.
