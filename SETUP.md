# Setting a kiosk up, from nothing

Assumes you know nothing about this system. Every command says which machine to
run it on and which folder to be in. Follow it top to bottom.

There are **two machines** in play, and they do different jobs:

| | What it is | What runs on it |
|---|---|---|
| **The server** | your laptop, for now. Later, a real server | the backend (`printvendo-backend`) |
| **The kiosk** | the machine with the printer plugged into it | the agent (`printvendo-agent`) |

They can be **the same laptop** — a laptop with a printer attached can run both
at once, and that is the quickest way to see the whole thing work. Part D says
what changes when they are separate machines.

Where the code already is on your laptop:

```
C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\
├── printvendo-backend\      the server
├── printvendo-agent\        the agent  ← this folder
└── printvendo-web\          the student app
```

Nothing is downloaded from the internet except Python and Ghostscript. The agent
is the folder you are reading this in.

---

# Part A — Install two things on the kiosk machine

Skip anything already installed. Check by opening PowerShell and typing the
check command; if it prints a version, you have it.

### 1. Python 3.11 or newer

Check: `python --version`

> If it prints nothing, or opens the Microsoft Store, you do **not** have it.
> The `python` that ships on Windows PATH is a stub that opens the Store. The
> installer detects this and stops.

Get it: <https://www.python.org/downloads/> → the big yellow button.
**Tick "Add python.exe to PATH"** on the first screen of the installer. That tick
box is the whole difference between this working and not.

Close and reopen PowerShell afterwards, then check again.

### 2. Ghostscript

This is what actually sends the PDF to the printer with the right colour, duplex
and page range. Without it nothing prints.

Check: `gswin64c --version`

Get it: <https://ghostscript.com/releases/gsdnld.html> → **Ghostscript AGPL
Release**, the 64-bit Windows installer. Accept the defaults.

Close and reopen PowerShell, then check again. If it still says the command is
not found, it installed to `C:\Program Files\gs\gs10.xx\bin` and did not add
itself to PATH — add that folder to PATH, or reinstall ticking the PATH option.

**On a Raspberry Pi you install nothing by hand.** The Pi installer does it.

---

# Part B — Start the server (on your laptop)

Open PowerShell. Everything in this part happens in the **backend** folder:

```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\printvendo-backend"
```

### B1. Find your laptop's address on the network

```powershell
ipconfig
```

Look for **IPv4 Address** under your Wi-Fi adapter. Something like
`192.168.1.7`. Write it down — it appears in several commands below. This guide
calls it `<LAN-IP>`.

Skip this if the kiosk *is* this laptop: use `127.0.0.1` everywhere instead.

### B2. Tell the backend its own address

Open `printvendo-backend\.env` in Notepad and add one line (or change it if it
is already there):

```
PUBLIC_BASE_URL=http://<LAN-IP>:8000
```

This is the address the kiosk will be told to call. Left as the default it says
`https://api.printvendo.com`, which is not running yet.

### B3. Let the kiosk through the Windows firewall

Only needed if the kiosk is a **different** machine. Windows blocks incoming
connections by default, so the Pi would simply never reach your laptop.

Open PowerShell **as Administrator** (right-click → Run as administrator):

```powershell
New-NetFirewallRule -DisplayName "Printvendo dev" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

Remove it when you are finished testing:

```powershell
Remove-NetFirewallRule -DisplayName "Printvendo dev"
```

### B4. Start the backend

```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\printvendo-backend"
.venv\Scripts\python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` is what makes it reachable from other machines. Without it, only
the laptop itself can talk to it.

**Leave this window open.** It is the server; closing it stops everything.

Check it from a browser: `http://<LAN-IP>:8000/health` should say
`{"status":"ok"}`.

### B5. Make the kiosk, and get its enrolment code

Open a **second** PowerShell window (the first is busy running the server):

```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\printvendo-backend"
.venv\Scripts\python -m app.cli provision-kiosk --name "Test Shop" --paper 500
```

It prints something like:

```
kiosk    Test Shop  ksk_aht70px25kb15b2r  (live)

selling: students can send jobs to it now.

enrolment code (spend within 12h): dve_HrZzapHvWhc-4M7kIJo-ayR5J9D10VaD

  on the machine itself, from the printvendo-agent folder:
    sudo bash install-pi.sh --code dve_Hr... --api http://192.168.1.7:8000
    powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Code dve_Hr... -Api http://192.168.1.7:8000
```

**Copy the enrolment code.** It is good for **one machine, one use, twelve
hours**. Two kiosks means running this command twice — never reuse a code.

`--paper 500` says the tray holds 500 sheets. `--name` is what students see.

---

# Part C — Turn a Windows PC into the kiosk

Everything in this part happens on the machine **with the printer attached**,
in the **agent** folder:

```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\printvendo-agent"
```

If that is a different PC, copy the whole `printvendo-agent` folder over first —
USB stick, network share, whatever. The installer installs *from the folder it
sits in*, so it must be a real folder on that machine.

### C1. Print a test page yourself, first

Before involving any of this. Windows → right-click any PDF → Print → pick the
printer → it comes out. If that does not work, nothing below will, and the
problem is the printer or its driver.

### C2. Find the printer's exact name

```powershell
Get-Printer | Select-Object Name
```

Copy the name **exactly**, spaces and all — for example `HP LaserJet M1005`.
Ignore `Microsoft Print to PDF`, `OneNote` and `Fax`; they are not printers.

If there is exactly one real printer you can leave `-Printer` off the next
command and it will find it. With several you must say which, because guessing
means somebody's dissertation coming out of a label machine.

### C3. Run the installer

Open PowerShell **as Administrator**, then:

```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\printvendo-agent"
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Code dve_PASTE_YOURS_HERE -Api "http://192.168.1.7:8000" -Printer "HP LaserJet M1005"
```

Three things to replace: the code from B5, your `<LAN-IP>`, your printer name.

It prints its progress:

```
==> Checking what is installed
    python 3.12, gswin64c
==> Installing the agent
==> Checking the printer
    HP LaserJet M1005
==> Enrolling this machine
Enrolled. This machine now prints for its kiosk using 'HP LaserJet M1005'.
==> Installing the service
==> Checking it end to end
    Ready. Printing to 'HP LaserJet M1005' for http://192.168.1.7:8000.

Done. The kiosk is printing.
```

If it stops before "Done", it tells you what is wrong and how to fix it. It will
not finish on a machine that cannot print — that is the point of the last two
steps.

Where things went:

| | |
|---|---|
| The agent | `C:\Program Files\Printvendo\` |
| Its settings, including the token | `C:\ProgramData\Printvendo\agent.json` |
| What runs it | Scheduled Task `PrintvendoAgent`, starts at boot as SYSTEM |

### C4. Commands you will want later

```powershell
# is it healthy
& "C:\Program Files\Printvendo\venv\Scripts\printvendo-agent.exe" check

# what printers can it see
& "C:\Program Files\Printvendo\venv\Scripts\printvendo-agent.exe" printers

# stop / start it
Stop-ScheduledTask  -TaskName PrintvendoAgent
Start-ScheduledTask -TaskName PrintvendoAgent

# watch it work, in the foreground, with its log on screen
Stop-ScheduledTask -TaskName PrintvendoAgent
& "C:\Program Files\Printvendo\venv\Scripts\printvendo-agent.exe" run
```

That last pair is the useful one while testing: stop the background service,
run it in a window, and watch each job come through.

### C5. Removing it

```powershell
Unregister-ScheduledTask -TaskName PrintvendoAgent -Confirm:$false
Remove-Item -Recurse -Force "C:\Program Files\Printvendo", "C:\ProgramData\Printvendo"
```

---

# Part D — Turn a Raspberry Pi into the kiosk

Assumes Raspberry Pi OS installed and you can SSH in. Everything here happens
**on the Pi**.

### D1. Copy the agent folder to the Pi

From your **laptop**, in PowerShell:

```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade"
scp -r printvendo-agent pi@raspberrypi.local:~/
```

Replace `pi@raspberrypi.local` with your Pi's username and address. If
`raspberrypi.local` does not resolve, use its IP — your router's device list
will show it.

### D2. SSH in

```powershell
ssh pi@raspberrypi.local
```

Everything from here is typed **on the Pi**.

### D3. Install CUPS and add the printer

CUPS is the Pi's printing system. It has to know the printer before the agent
can use it.

```bash
sudo apt-get update
sudo apt-get install -y cups
sudo usermod -aG lpadmin $USER      # lets you manage printers
```

Plug the printer into the Pi's USB and switch it on. Then:

```bash
lpinfo -v
```

It lists devices. Find the line starting `direct usb://` — that whole
`usb://...` string is the printer's address. Then, using it:

```bash
sudo lpadmin -p Shop -E -v "usb://HP/LaserJet%20M1005?serial=XXXX" -m everywhere
lpstat -e
```

- `-p Shop` is the name you are giving it. Short, no spaces. You will pass this
  to the installer.
- `-m everywhere` is driverless printing and works with almost any printer made
  since about 2015. For an older one: `lpinfo -m | grep -i <your model>` lists
  the drivers this Pi has, and you pass one of those instead.

Prove it works before going further:

```bash
lp -d Shop /usr/share/cups/data/testprint
```

A page should come out. If it does not, fix that first — the agent cannot make a
printer work that CUPS cannot.

### D4. Run the installer

```bash
cd ~/printvendo-agent
sudo bash install-pi.sh --code dve_PASTE_YOURS_HERE --api http://192.168.1.7:8000 --printer Shop
```

Same three replacements: the code from B5, your laptop's `<LAN-IP>`, the printer
name from D3.

It installs Python, CUPS and Ghostscript if missing, enrols the Pi, and leaves a
service that starts at boot and restarts itself if it crashes. It finishes by
checking the whole thing and fails if the kiosk is not actually ready.

### D5. Commands you will want later

```bash
/opt/printvendo/venv/bin/printvendo-agent check      # is it healthy
journalctl -u printvendo-agent -f                    # watch it work
sudo systemctl restart printvendo-agent
sudo systemctl stop printvendo-agent
```

Settings, including the device token, live in `/etc/printvendo/agent.json`.

---

# Part E — Print something

1. On your laptop, in a **third** PowerShell window:

   ```powershell
   cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\printvendo-web"
   npm run dev
   ```

   Open <http://localhost:3000>.

2. Sign up, or use a seeded account. `python -m app.cli seed` in the backend
   folder builds a whole world — a shop, students, an owner — and prints their
   passwords.

3. Upload a PDF, choose **Test Shop**, pick colour and duplex, and pay. The
   seeded student has wallet money, so no card is needed.

   Wallet only works at **PLATFORM** kiosks — the default from B5 — which is
   deliberate.

4. Watch the job on screen:

   **Queued → Printing → Printed**

   Those come off the printer's own queue. "Printing" means the printer has the
   job in its hands, not that the agent sent it — so a job waiting behind
   another one honestly says Queued.

If nothing happens, the agent asks for work every fifteen seconds. Wait that long
before deciding it is broken.

### Using the app from your phone

The app is already set up to talk to `http://localhost:8000`, which only works
on the laptop itself. To upload from a phone on the same Wi-Fi — which is what a
student actually does — two edits:

`printvendo-web\.env`:

```
NEXT_PUBLIC_API_URL=http://<LAN-IP>:8000
```

`printvendo-backend\.env`, add your address to the list that is already there:

```
CORS_ORIGINS=http://localhost:3000,http://localhost:3002,http://<LAN-IP>:3000
```

Restart both, then open `http://<LAN-IP>:3000` on the phone. The firewall rule
from B3 covers port 8000; add a second one for 3000 the same way.

> **`next build` while `next dev` is running breaks the dev server** — they
> share the `.next` folder and every page starts erroring. If that happens: stop
> dev, delete `.next`, start dev again.

---

# Part F — When something goes wrong

Run `check` first, on the kiosk machine. It answers one question — can this
machine print a job right now — and names the fix for every reason it cannot.

| It says | What it means | Fix |
|---|---|---|
| `This machine is not enrolled` | the install did not finish | run the installer again with a fresh code |
| `Could not reach http://…` | the kiosk cannot see the server | B3 firewall rule; check `<LAN-IP>` is still right |
| `The printer '…' is not there any more` | renamed, unplugged, or off | `printvendo-agent printers` for the current list |
| `Ghostscript` in the message | not installed, or not on PATH | Part A step 2 |
| `Enrolment failed` | the code is spent or over twelve hours old | provision again (B5) for a new one |

Other symptoms:

| Symptom | Cause |
|---|---|
| Job stays **Queued** for ever | agent not running. `Get-ScheduledTask PrintvendoAgent`, or `systemctl status printvendo-agent` |
| Kiosk shows **offline** in the app | same — the agent sends a heartbeat every minute |
| Job goes to **Failed** at once | the printer is out of paper, offline, or jammed. The agent reads that from the queue and does not pretend |
| Prints, but ignores colour or duplex | the printer driver, not the agent. Print the same PDF by hand with the same settings — if that ignores them too, it is the driver |
| Everything worked yesterday, nothing today | your laptop's `<LAN-IP>` changed. Re-check `ipconfig`, and update `--api` |

Your `<LAN-IP>` changing is the single most common cause of a kiosk that stopped
working, because home and campus routers hand out addresses that expire. On a
real server with a real domain name this stops being a problem.

**The device token in the settings file *is* the kiosk.** Nothing the agent
sends says which kiosk it belongs to — the token decides. Treat it like a
password. If a machine is lost or replaced, provision a new kiosk rather than
copying the file across.

---

# Part G — What changes for a real shop

This guide points the kiosk at your laptop over plain HTTP, which is right for a
bench and wrong for a shop: a device token crosses that wire on every request.
In production:

- the backend runs on a server with a domain name and HTTPS;
- `--api` / `-Api` becomes `https://api.printvendo.com` — the installers already
  default to it, so the flag disappears;
- no firewall rule and no `<LAN-IP>`, because the address stops moving.

Nothing else about the kiosk changes. The same two installer commands, the same
enrolment code from the same `provision-kiosk`.
