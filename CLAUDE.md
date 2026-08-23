# printvendo-agent

The machine at a shop. One agent, both platforms — a Raspberry Pi with CUPS and
a Windows PC behave identically, because they run the same code and differ only
in one function.

Replaces `pi-agent/` and `windows-agent (1)/`, both of which stay in the repo
until cutover and neither of which should be edited.

## Why it exists rather than a fix to what was there

`pi-agent` printed correctly on Linux and **already contained Windows code** —
so there was never one Linux agent and one Windows agent, there were two
half-finished Windows implementations:

- `pi-agent`'s Windows path filled in a `DEVMODE` with copies, duplex and
  colour, **never applied it** (no `SetPrinter`, no `DocumentProperties`), read
  the file into a variable it never used, and then printed by shelling out to
  `cmd /c start /print` — the shell's print verb, which uses the *default*
  printer with default settings and has no notion of a page range. Every option
  was silently dropped on a job the student had already paid for.
- `windows-agent` had its own registration path and claimed **one task per
  pass**, which is why four files sent together printed one.

Both spoke the legacy API, so a rewrite was required regardless. Two
implementations of claiming is also how the same job reaches two devices.

## The two rules

**Claim until the server says there is nothing left.** A wake is a hint that
work exists, never a count of it. `tests/test_runner.py` is mostly this.

**Every option reaches the printer, on both platforms.** The mapping is a pure
function per platform and *the same tests run against both*, so an option
cannot be added to one and forgotten on the other.

| Option | Pi (CUPS) | Windows (Ghostscript) |
|---|---|---|
| Copies | `-n 3` | `-dNumCopies` **and** `/NumCopies` |
| Duplex | `-o sides=two-sided-long-edge` | `<< /Duplex true /Tumble false >>` |
| Colour | `print-color-mode` **and** `Ink=MONO/COLOR` | `<< /BitsPerPixel 1\|24 >>` |
| Page range | `-o page-ranges=1,12-17` | `-sPageList=1,12,13,…` |

Both sides are stated even when false: a shop that has left duplex on in the
driver would otherwise halve the paper of every single-sided job while the
student is charged for single sides.

Two things are doubled deliberately, and neither is redundancy for its own
sake. `Ink=MONO` was learned on real hardware — HP's hpcups prints in colour
with `print-color-mode=monochrome` alone. Copies on Windows is set twice
because which one a driver honours varies, and the failure mode is one copy of
a job somebody paid three for.

**Ghostscript is the print engine on Windows.** It is already a dependency of
this system, it drives a named printer through `mswinpr2` with no dialog, and
it takes every option. It runs under `-dSAFER`: a PDF is somebody else's file
and Ghostscript will execute what is in it given the chance.

**Colour is honestly advisory.** `/BitsPerPixel` is a hint some drivers ignore,
exactly as some CUPS drivers ignore `print-color-mode`. Forcing true greyscale
on a particular model may need a driver-specific setting, and that has to be
checked against the machine rather than assumed.

## Setting one up

Pi, headless, over SSH:

```bash
sudo bash install-pi.sh --code dve_xxxx --api https://api.printvendo.com
```

Windows, in an Administrator PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Code dve_xxxx
```

Both install what is missing, enrol the machine, and leave something that
starts at boot — systemd on the Pi, a scheduled task on Windows (a task rather
than a service because a service needs a wrapper and this needs none).

The **enrolment code** comes from provisioning the kiosk:
`python -m app.cli provision-kiosk --name "…"` prints one. It is one-time and
lives twelve hours, so a code left in a terminal's scrollback is worthless by
morning. The **token** it exchanges for is the only credential on the machine
and lives in one file, `0600`.

The printer is not asked about when the machine has exactly one — which is
nearly all of them. With several, `enrol` refuses and lists them, because
guessing means somebody's dissertation on the label machine.

## The commands

```
printvendo-agent enrol --code dve_...   # once, at install
printvendo-agent check                  # is this machine ready, and if not, why
printvendo-agent printers               # what this machine can see
printvendo-agent run                    # the loop; what the service runs
```

`check` names the fix for each problem rather than printing a traceback. An
installer at a shop counter who has to interpret a stack trace will ring
somebody, and the shop stays shut.

## What the backend expects

```
POST /v1/device/register            spend an enrolment code, receive a token
POST /v1/device/heartbeat           alive; "online" is derived from this
POST /v1/device/tasks/next          claim one task, or nothing
GET  /v1/device/tasks/{id}/file     the PDF, streamed to disk
POST /v1/device/tasks/{id}/status   printing / printed / failed / blocked
WS   /v1/device/ws                  {"type": "wake"} means ask now
```

`X-Device-Token` **is** the kiosk. Nothing the agent sends says which kiosk it
belongs to; the old `/pi/*` routes trusted a printer id in the URL, so one
shop's machine could fetch another's file.

Claiming returns **one** task because the server claims with a single
`FOR UPDATE SKIP LOCKED` statement — two devices racing cannot be handed the
same job. The agent's half of that bargain is to keep asking until the answer
is nothing.

`sheets_printed` is reported from the server's own `expected_sheets`, not
recomputed here. One calculation decides the price, the tray count and what the
printer is asked for; a second opinion in the agent is how a counter drifts
from the physical tray.

## Not done yet

- **The wake socket is not connected.** The loop polls every fifteen seconds,
  which is what every kiosk did before the socket existed and is correct but
  slower. `/v1/device/ws` is built and waiting.
- **Nothing waits for the spooler.** A task is reported PRINTED when the print
  command returns, which on Windows means "Ghostscript finished handing it
  over". Polling `EnumJobs` would let it report what the printer actually did,
  and report a jam as a failure rather than a success.
- **`sheets_printed` is the prediction, not the count.** CUPS can report
  `job-media-sheets-completed`; Windows can be asked too. Until then the
  server's figure is used, which is exactly what it is designed to fall back
  to.
- **No test against real hardware.** Every option is asserted in the built
  command; whether a given driver honours it is a question for a printer.
