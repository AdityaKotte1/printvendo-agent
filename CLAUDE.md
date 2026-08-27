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

`SETUP.md` is the whole procedure for a person doing it: what to install first,
how to add a printer to CUPS over SSH, the bench setup against a laptop, and
what each failure means. What follows is the shape of it.

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
POST /v1/device/commands/next       everything an operator has asked for
POST /v1/device/commands/{id}/result   how it went
POST /v1/device/printer-health      stuck / working again -- closes the shop
WS   /v1/device/ws                  {"type": "wake"} means ask now
```

`X-Device-Token` **is** the kiosk. Nothing the agent sends says which kiosk it
belongs to; the old `/pi/*` routes trusted a printer id in the URL, so one
shop's machine could fetch another's file.

Claiming **commands** returns a list, not one. Restarting the agent kills the
loop that would have come back for whatever was queued behind it, so a machine
handed one per pass would silently drop the second half of "restart the print
service, then restart the agent".

Claiming a **task** returns one because the server claims with a single
`FOR UPDATE SKIP LOCKED` statement — two devices racing cannot be handed the
same job. The agent's half of that bargain is to keep asking until the answer
is nothing.

`sheets_printed` is reported from the server's own `expected_sheets`, not
recomputed here. One calculation decides the price, the tray count and what the
printer is asked for; a second opinion in the agent is how a counter drifts
from the physical tray.

## Queued, printing, printed

The three words a student reads are produced here, so each has to mean what it
says. **Printing means the printer has the job**, not that the agent sent it: a
job behind somebody else's two hundred pages is queued, and telling a student
otherwise sends them walking to the counter for nothing.

`lp` returns when a job is queued and Ghostscript returns when the spooler has
the data — neither is a state worth reporting. So the queue is polled for one:

| | Pi (CUPS) | Windows (spooler) |
|---|---|---|
| Queued | in `lpstat -o`, not at the head | job present, `SPOOLING` or paused |
| Printing | at the head of `lpstat -o` | `JOB_STATUS_PRINTING` |
| Printed | gone from `lpstat -o` | job id gone from `EnumJobs` |
| Failed | printer `disabled`/`stopped` | `ERROR`, `PAPEROUT`, `OFFLINE`, `BLOCKED` |

A change is reported once, when it happens — a long job must not post
"printing" every two seconds for four minutes. A paused Windows job is *queued*,
not failed: it resumes, and the long timeout catches it if it does not.

**A queue that cannot be read is not an empty queue**, and **not finishing is a
failure, never a success**. A wait that timed out and said "done" would be the
silent version of the bug this replaced.

The Windows watcher matches **job ids snapshotted before printing**. Matching by
document name was tried first and was a no-op that looked exactly like a working
one: the spooler calls every Ghostscript job "Ghostscript output", whatever the
file. Found by watching a real queue, not by reading the docs.

## Not done yet

- **The wake socket is not connected.** The loop polls every fifteen seconds,
  which is what every kiosk did before the socket existed and is correct but
  slower. `/v1/device/ws` is built and waiting.
- **`sheets_printed` is the server's figure, not the printer's count.** By
  decision: one calculation decides the price, the tray count and what the
  printer is asked for, and a second opinion in the agent is how a counter
  drifts from the physical tray. CUPS can report
  `job-media-sheets-completed` if that is ever wanted.
- **A printer that swallows a job and jams silently still reports printed.**
  The queue letting go is the best signal the spooler gives; distinguishing
  "came out correctly" needs the driver's own page accounting.
- **The restart commands have not been run on a Pi.** `restart_printing` was
  invoked for real on Windows and failed with the Spooler's own words because
  the shell was not elevated -- the correct behaviour, and the reason the
  message is passed through rather than a return code. Under the installer the
  agent runs as SYSTEM with `RunLevel Highest`, which has the rights. The CUPS
  and systemd halves are covered by tests and have not met hardware.
- **Tested against a Windows spooler, not against a Pi.** The CUPS half is
  covered by tests over real `lpstat` output and is the same shape the previous
  agent used in production, but it has not been run on hardware in this
  session.

## Commands, and admitting the printer is stuck

`agent/commands.py` does what the console asked. Two commands and no more:

- `restart_printing` — CUPS on a Pi, the Print Spooler on Windows. The server
  sends one name and the machine knows which it has.
- `restart_agent` — through the service manager (`systemctl restart
  printvendo-agent`, or stop/start the `PrintvendoAgent` scheduled task), never
  by exiting. A bare `sys.exit` on a machine whose supervisor was not told to
  restart it leaves a shop with no agent and no way back but driving to it.

**`restart_agent` is reported before it runs.** The process that would report it
afterwards is the one being killed, so a command that waited would always end in
silence and read as a failure. The call is detached for the same reason.

**Which is why it checks first.** `restart_agent.precheck` asks whether anything
supervises this process -- `systemctl is-enabled printvendo-agent`, or
`Get-ScheduledTask` -- and refuses if not. An agent started by hand from a
terminal would otherwise detach a restart that does nothing while the console
reported it succeeded. The check is a `precheck` on the handler rather than the
first line of the function, because by the time the function runs the caller has
already sent the report.

Nothing restarts Ghostscript. It is not a service: a copy runs for one file and
exits.

**Commands are claimed before work.** A restart asked for because nothing is
printing must not queue behind the twenty jobs that exist because nothing is
printing. `_do_commands` never raises — work is the job, commands are the
favour.

**A stuck printer closes the shop; a bad file does not.** `print_task` raises
`PrinterStuck` when the queue will not let go of a job or the printer has
stopped, and only that raises `report_printer_health(stuck=True)`. Ghostscript
refusing a PDF fails one student and the next job prints fine; a jammed tray
fails everybody. `PrinterHealth` reports only on the *change*, so a jam is one
alert rather than one per failed job, and a failed report puts the flag back so
the next job tries again — a shop left selling because one request failed is the
whole thing this prevents.
