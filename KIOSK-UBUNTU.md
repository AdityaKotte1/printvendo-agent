# An unattended Ubuntu kiosk

For a shop PC or thin client (this was written on an HP thin client) that must
come back on its own after a power cut: power itself on, log in to the shop's
captive portal, start printing, and show the shop screen -- with nobody there.

Everything here is typed on that machine, over SSH from your laptop.

The chain, in order. Each step below is one link, and the test at the end pulls
the plug to prove the whole chain rather than any one link:

```
AC returns -> firmware powers the PC on -> Ubuntu boots -> autologin (openbox)
           -> portal-keeper logs in to the captive portal, retrying
           -> printvendo-agent claims and prints
           -> Chromium shows http://127.0.0.1:8765, relaunched if it dies
```

## What you need in hand

- The thin client, a monitor, a keyboard, and a USB stick (4GB+).
- The shop's LAN cable, and the captive portal username and password.
- The printer(s), with a USB cable each.
- An enrolment code for this kiosk from the admin console (single use, valid 12
  hours) -- mint it when you reach step 5, not before.
- Another machine to SSH from.

## Installing the operating system

**Ubuntu Server 24.04 LTS**, not Ubuntu Desktop. A thin client has a few GB of
flash and 2-4GB of RAM; GNOME spends most of both on a machine whose entire job
is one browser window. The desktop parts this build needs -- an X server,
openbox, a display manager -- are four packages, installed below.

1. Write the ISO to the USB stick (Raspberry Pi Imager, Rufus or
   `dd` all do it).
2. Boot the thin client from USB -- `F9` on HP for the boot menu.
3. Through the installer: pick the whole internal disk, no LVM encryption (a
   kiosk that boots unattended cannot be asked for a passphrase), create your
   user, and **tick "Install OpenSSH server"**. Skip every snap it offers.
4. Reboot, pull the USB stick, and log in once at the console.

Debian 12 is a slightly lighter alternative and ships Chromium as a real
package rather than a snap; everything below works on it unchanged except that
`snap install chromium` becomes `apt-get install -y chromium`.

## Everything this kiosk installs

One line, so there is nothing to discover later:

```bash
sudo apt-get update
sudo apt-get install -y   openssh-server curl git ca-certificates   python3 python3-pip python3-venv   cups ghostscript   xserver-xorg xinit x11-xserver-utils openbox lightdm unclutter   snapd dmidecode
sudo snap install chromium
```

What each group is for: SSH and curl are how you work on it and how the portal
is logged in to; python runs the agent; CUPS and Ghostscript are printing; the
X server, openbox and LightDM are the shop screen and nothing else; `unclutter`
hides the mouse pointer; `dmidecode` names the board when you are looking for
its firmware menu.

## 0. Before you start

SSH is how you get back in once the login screen is gone. Set it up first.

```bash
sudo apt-get update
sudo apt-get install -y openssh-server
sudo systemctl enable --now ssh
ip -4 addr show scope global | grep inet      # note the address
id -un; id -u                                 # note the user and uid
```

A wired kiosk wants a fixed address or a DHCP reservation, or you will be
hunting for it after every outage.

## 1. Firmware: power on when the power returns

While the PC is off, nothing in Ubuntu is running, so this can only be fixed in
firmware. Reboot into setup (`F10` or `Esc` on HP, `F2`/`Del` elsewhere) and set:

- **After Power Loss** / "Restore on AC Power Loss" / "AC Recovery" -> **Power On**
  (not "Last State" -- that leaves a PC which was off during a dip off)
- **ErP / EuP Ready** -> Disabled
- **Deep Sleep / Deep S5** -> Disabled
- **Wake on LAN** -> Enabled (harmless, and some boards need the NIC powered
  for AC-restore to work at all)

Save and exit. Identify the board if the menu names differ:

```bash
sudo dmidecode -t1 -t2 | grep -E 'Manufacturer|Product Name'
```

If these settings do not survive a power cut, the CMOS battery is flat -- a
CR2032. That also loses the clock, which breaks TLS, which reads as "the agent
is dead" rather than as a battery.

## 2. The clock

A PC has a battery-backed clock, so this is usually nothing. Check anyway,
because everything fails at TLS when it is wrong:

```bash
timedatectl status          # want: System clock synchronized: yes
```

Not syncing, and `timedatectl show-timesync --all` shows `Packet count: 0`? The
network is blocking NTP (UDP 123). Use HTTP time instead:

```bash
sudo apt-get install -y htpdate
sudo systemctl enable --now htpdate
sudo timedatectl set-ntp false        # stop the two fighting
```

Behind a captive portal, time sync only works *after* step 4 lets it out.

## 3. The printers

CUPS must know every printer before the agent can use one. Plug them in, switch
them on:

```bash
sudo apt-get install -y cups ghostscript
sudo usermod -aG lpadmin $USER && newgrp lpadmin
lpinfo -v                              # find each  usb://...  line
```

One `lpadmin` per machine. Short names, no spaces -- these names appear in the
admin console as tray names:

```bash
sudo lpadmin -p Mono1 -E -v "usb://HP/LaserJet%20M1005?serial=XXXX" -m everywhere
sudo lpadmin -p Mono1 -o media-default=iso_a4_210x297mm
sudo lpadmin -p Mono1 -o printer-error-policy=stop-printer
```

`-m everywhere` is driverless IPP, right for nearly anything made since about
2015; for an older machine, `lpinfo -m | grep -i <model>` lists what is
available.

**`stop-printer` is not optional.** On `abort-job`, CUPS throws a failed job
away and it looks exactly like a finished one, so the agent reports it printed
and the student pays for paper that never moved -- that happened on three
kiosks and seventy-four jobs.

Prove each one prints before the agent exists:

```bash
lp -d Mono1 /usr/share/cups/data/testprint
lpstat -o                              # should drain to empty
```

## 4. The captive portal, unattended

A portal session dies on reboot and usually expires on a timer, so this has to
be a loop and not a one-shot: after a power cut this PC is often up minutes
before the switch and the ISP are, and a single early attempt fails with
nothing to retry it.

### 4a. Ask for a MAC exemption first

An always-on machine that prints unattended is what MAC bypass exists for, and
it retires everything else in this section. Get the address, then ask whoever
runs the firewall:

```bash
ip -br link show | grep -v LOOPBACK        # the wired NIC's ether address
```

### 4b. Check whether automation is needed at all

Some portals authenticate by MAC, and the session then survives a reboot:

```bash
sudo reboot
# once it is back, having logged in to nothing:
curl -fsS -o /dev/null -w '%{http_code}' https://api.printvendo.com/health; echo
```

`200` means there is nothing to automate. Build the keeper only if that fails.

### 4c. Find the portal

Let it tell you where it is. Plain HTTP on purpose -- an HTTPS URL gets a
certificate error instead of a redirect:

```bash
curl -sIL --max-time 10 http://1.1.1.1 | grep -iE '^location|^HTTP'
curl -sL --max-time 10 -o /tmp/portal.html -w 'landed on: %{url_effective}' http://1.1.1.1; echo
grep -oE '<form[^>]*>|<input[^>]*>' /tmp/portal.html
```

That `Location:` header, or the `landed on:` line, **is** the portal URL. Some
networks also hand it over in DHCP:

```bash
grep -riE 'captive|portal|OPTION_114' /run/systemd/netif/leases/ 2>/dev/null
```

Nothing redirects? Then the portal is the gateway on an odd port, and which
port it is names the vendor:

```bash
ip route | grep default                   # the gateway
sudo apt-get install -y nmap
sudo nmap -Pn -p 80,443,1000,1003,8000,8002,8080,8090,8443 GATEWAY
```

| Open port | Likely | Login endpoint |
|---|---|---|
| 8002 / 8003 | **pfSense** | `/index.php?zone=<zone>` |
| 8090 | Cyberoam / Sophos | `/login.xml` |
| 1000 / 1003 | Fortinet FortiGate | `/login` |
| 80 plus `/login` | MikroTik hotspot | `/login` |
| 8080 / 443 | Ruckus / Aruba / Cisco | vendor-specific |

When the shape is unfamiliar, do it the reliable way: open `http://1.1.1.1` in
the browser on that machine, `F12` -> **Network** -> tick "Preserve log", log in
by hand, find the request that carried the credentials, and right-click ->
**Copy as cURL**. That one string holds the URL, the method, every field name
and any hidden token.

**pfSense** is the common one on a hostel LAN, and needs no guessing. A portal
URL shaped like `http://<gateway>:8002/index.php?zone=<zonename>` is pfSense:
it posts back to that same URL with `auth_user`, `auth_pass`, a hidden `zone`,
`redirurl`, and a submit field named `accept`. Its session is bound to the
client MAC and IP, so a DHCP change ends it -- get a static lease -- and the
zone's **hard timeout** will log a kiosk out mid-day, which the loop below
recovers from within a minute. Better still, ask for the kiosk's MAC under that
zone's **Allowed MAC addresses**, and then none of this is needed.

Store the credentials where only root can read them:

```bash
sudo install -d -m 700 /etc/printvendo
sudo tee /etc/printvendo/portal.env >/dev/null <<'EOF'
PORTAL_URL=http://172.20.28.1:8002/index.php?zone=hostelzone
PORTAL_ZONE=hostelzone
PORTAL_USER=theusername
PORTAL_PASS=thepassword
EOF
sudo chmod 600 /etc/printvendo/portal.env
```

Prove the login works once, by hand, before wrapping it in a service: a
service that fails silently looks exactly like a network that is down.

```bash
. /etc/printvendo/portal.env
curl -fsS --max-time 20 -o /dev/null --data-urlencode "auth_user=$PORTAL_USER" --data-urlencode "auth_pass=$PORTAL_PASS" --data-urlencode "zone=$PORTAL_ZONE" --data-urlencode "redirurl=http://neverssl.com" --data-urlencode "accept=Continue" "$PORTAL_URL"
curl -fsS -o /dev/null -w 'health: %{http_code}' https://api.printvendo.com/health; echo
```

`health: 200` and it works. Then the keeper, which does the same thing for
ever:

```bash
sudo tee /usr/local/bin/portal-keeper >/dev/null <<'EOF'
#!/bin/sh
# Stay logged in to the shop's captive portal, for ever.
#
# A loop rather than a boot-time one-shot: after a power cut this PC is often
# up before the switch and the ISP are, so the first attempt has nothing to
# talk to. It logs nothing about the credentials it uses -- a line naming them
# would leave the shop's password in journalctl for ever.
set -u
while :; do
  if ! curl -fsS --max-time 10 -o /dev/null https://api.printvendo.com/health; then
    . /etc/printvendo/portal.env
    curl -fsS --max-time 20 -o /dev/null \
      --data-urlencode "auth_user=$PORTAL_USER" \
      --data-urlencode "auth_pass=$PORTAL_PASS" \
      --data-urlencode "zone=${PORTAL_ZONE:-}" \
      --data-urlencode "redirurl=http://neverssl.com" \
      --data-urlencode "accept=Continue" \
      "$PORTAL_URL" || true
    if curl -fsS --max-time 10 -o /dev/null https://api.printvendo.com/health; then
      logger -t portal-keeper "online again"
    else
      logger -t portal-keeper "still offline, will retry"
    fi
  fi
  sleep 60
done
EOF
sudo chmod 755 /usr/local/bin/portal-keeper

sudo tee /etc/systemd/system/portal-keeper.service >/dev/null <<'EOF'
[Unit]
Description=Keep the kiosk logged in to the captive portal
After=network.target

[Service]
ExecStart=/usr/local/bin/portal-keeper
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now portal-keeper
journalctl -t portal-keeper -n 5 --no-pager
```

It checks `api.printvendo.com/health` rather than a generic "am I online" URL,
because that is the only reachability this shop cares about.

The agent is deliberately **not** ordered after this. It retries on its own, and
making it wait for a network that may be ten minutes away would leave it
stopped when the network arrives.

## 5. The agent

Skip if it is already installed. Otherwise mint an enrolment code in the admin
console for this kiosk (single use, 12 hours), then:

```bash
cd ~
git clone https://github.com/AdityaKotte1/printvendo-agent.git
cd printvendo-agent
sudo bash install-pi.sh --code dve_PASTE_YOURS --api https://api.printvendo.com --printer Mono1
```

Two printers: drop `--printer` and name the pools instead, repeating a flag per
machine -- `--bw Mono1 --colour Colour1`. The installer works on Ubuntu as well
as a Pi: it installs what is missing, fixes error policies, enrols, and leaves a
service that starts at boot and restarts itself.

```bash
/opt/printvendo/venv/bin/printvendo-agent check
systemctl is-enabled printvendo-agent        # must be: enabled
```

Then, in the admin console, set each tray's size and what is actually loaded. A
newly announced tray arrives **empty** on purpose -- a tray defaulting to full
would route jobs at paper nobody has loaded.

## 6. No login screen, and only the two windows you need

GNOME is not wanted here. openbox, autologin, and nothing else: the locked shop
screen, plus a browser window you can open for the portal when you have to.

```bash
sudo apt-get install -y openbox xinit x11-xserver-utils lightdm
which chromium || sudo snap install chromium

sudo mkdir -p /etc/lightdm/lightdm.conf.d
sudo tee /etc/lightdm/lightdm.conf.d/60-kiosk.conf >/dev/null <<EOF
[Seat:*]
autologin-user=$(id -un)
autologin-user-timeout=0
user-session=openbox
EOF
sudo systemctl set-default graphical.target
sudo systemctl enable lightdm
```

The screen itself, relaunched for ever -- a crashed browser is a black
rectangle nobody is watching, while the agent goes on printing:

```bash
mkdir -p ~/.config/openbox
cat > ~/.config/openbox/autostart <<'EOF'
# The shop screen must stay lit.
xset s off -dpms &

while :; do
  # A crash otherwise leaves Chromium's "didn't shut down correctly" bar
  # across the page, on a screen above a counter.
  sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' \
      ~/.config/chromium/Default/Preferences 2>/dev/null
  chromium --app=http://127.0.0.1:8765 --kiosk --noerrdialogs --disable-infobars \
           --incognito --disable-session-crashed-bubble \
           --check-for-update-interval=31536000
  sleep 3
done &
EOF
chmod +x ~/.config/openbox/autostart
```

A shortcut for the portal window, `Super+P`, in its own profile so it cannot
disturb the display:

```bash
cp /etc/xdg/openbox/rc.xml ~/.config/openbox/rc.xml
python3 - <<'PY'
import pathlib
p = pathlib.Path.home()/".config/openbox/rc.xml"
s = p.read_text()
bind = """    <keybind key="W-p">
      <action name="Execute"><command>chromium --new-window --user-data-dir=/tmp/portal http://1.1.1.1</command></action>
    </keybind>
"""
assert "<keyboard>" in s
p.write_text(s.replace("<keyboard>", "<keyboard>\n" + bind, 1))
print("bound Super+P")
PY
```

`Alt+Tab` switches windows, `Alt+F4` closes the portal one. The display window
has no address bar, so the counter cannot browse from it. Note that openbox is
not a lock: anyone with a keyboard can `Alt+F4` out of the display. The locked
screen with a PIN exists only on Windows today.

Hide the pointer if there is no touchscreen:

```bash
sudo apt-get install -y unclutter
echo 'unclutter -idle 0 &' >> ~/.config/openbox/autostart
```

## 7. One command that says whether the shop is fine

```bash
sudo tee /usr/local/bin/kiosk-selfcheck >/dev/null <<'EOF'
#!/bin/sh
echo "clock:    $(date -u '+%F %T') UTC"
echo "internet: $(curl -fsS --max-time 8 -o /dev/null -w ok https://api.printvendo.com/health || echo DOWN)"
echo "agent:    $(systemctl is-active printvendo-agent)"
echo "portal:   $(systemctl is-active portal-keeper)"
echo "display:  $(curl -fsS --max-time 5 -o /dev/null -w served http://127.0.0.1:8765/status || echo DOWN)"
echo "browser:  $(pgrep -c chromium 2>/dev/null || echo 0) processes"
echo "printers: $(lpstat -e 2>/dev/null | tr '\n' ' ')"
echo "queue:    $(lpstat -o 2>/dev/null | wc -l) waiting"
/opt/printvendo/venv/bin/printvendo-agent check
EOF
sudo chmod 755 /usr/local/bin/kiosk-selfcheck
kiosk-selfcheck
```

## 8. Prove it, by pulling the plug

Not `reboot` -- that tests half of this. Pull the plug at the wall, wait thirty
seconds, plug it back in, and touch nothing.

```bash
# from your laptop, once it answers again:
ssh user@kiosk kiosk-selfcheck
ssh user@kiosk 'journalctl -b -t portal-keeper -u printvendo-agent --no-pager | tail -30'
```

What good looks like: the PC powers itself on, autologins to openbox, the shop
screen is up within seconds (briefly saying offline), `portal-keeper` logs
`online again` as soon as the uplink returns, and the agent's next poll claims
whatever queued while the shop was dark. Finish by placing a real order and
watching paper come out.

Nothing re-prints from before the cut: a job whose machine goes quiet has its
lease expire and is failed, never handed out again.

## When something is wrong

| Symptom | Where to look |
|---|---|
| PC stays off after a cut | firmware setting reverted -> CMOS battery; or it is set to "Last State" |
| Boots to a login screen | `systemctl get-default`, and the autologin file in step 6 |
| Black screen, agent printing | `pgrep -a chromium`; run the `chromium` line by hand over SSH with `DISPLAY=:0` |
| Screen says offline for ever | `journalctl -t portal-keeper`; the form fields in step 4 are probably wrong |
| Everything fails at TLS | the clock (step 2). "certificate is not yet valid" means a wrong clock, not a bad certificate |
| Jobs stay Queued | `systemctl status printvendo-agent`; or no tray has enough paper for that job |
| Job claimed, nothing prints | `lpstat -p -l` -- queue disabled by a jam, or the wrong driver |
| Shop shows closed after a jam | clear the jam; the agent reports recovered and the shop reopens. It only reopens what it closed |

## Undoing it

```bash
sudo systemctl disable --now portal-keeper
sudo rm -f /etc/systemd/system/portal-keeper.service /usr/local/bin/portal-keeper
sudo rm -f /etc/lightdm/lightdm.conf.d/60-kiosk.conf     # login screen returns
rm -f ~/.config/openbox/autostart                        # no shop screen
```

A local keyboard always gets you a text console on `Ctrl+Alt+F3`.
