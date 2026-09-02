"""Doing what the console asked: restart the print service, or restart myself.

Two commands, and the machine decides what each means on the platform it is
actually running on. The server sends `restart_printing`; on a Pi that is CUPS
and on a Windows PC it is the Print Spooler. Sending `restart_cups` to a machine
that has never had CUPS would be a name that lies on half the estate.

There is nothing here for Ghostscript. It is not a service — a copy is started
for one file and exits — so there is nothing running to restart, and a button
that pretended otherwise would be a placebo an operator presses while a shop
waits.

**Restarting the agent is reported before it happens.** The process that would
say "that worked" is the one being killed, so success is claimed up front and
the operator confirms it by watching the machine come back — which is what the
heartbeat already tells them. The alternative is a command that always ends in
silence and reads as a failure.

That is only honest if something is actually going to restart us, so
`restart_agent` **asks the supervisor whether it knows us first**. A machine
run by hand from a terminal, or one whose unit or scheduled task was never
installed, would otherwise detach a command that does nothing while the console
said "succeeded" about a shop that never came back. Checking costs one cheap
call and turns a silent lie into a sentence an operator can act on.
"""

import logging
import platform
import shlex
import subprocess
import sys

from agent.config import Config

log = logging.getLogger("agent")

IS_WINDOWS = platform.system() == "Windows"

# Where an update comes from. The zip rather than a git URL, so no machine needs
# git installed; the branch rather than a tag, so pushing is the release.
UPDATE_SOURCE = (
    "https://github.com/AdityaKotte1/printvendo-agent/archive/refs/heads/main.zip"
)
UPDATE_LOG = (
    "C:/ProgramData/Printvendo/update.log" if IS_WINDOWS
    else "/var/log/printvendo-update.log"
)

# Long enough for a service that is mid-restart, short enough that a wedged
# service manager does not hold the print loop.
TIMEOUT = 90

RESTART_PRINTING = "restart_printing"
RESTART_AGENT = "restart_agent"
UPDATE_AGENT = "update_agent"

NOT_A_SERVICE = (
    "this agent is not running as a service on this machine, so nothing would "
    "restart it -- start it with the installer rather than by hand"
)

# What the installers create. Changing either here without changing the
# installer would give an operator a button that silently restarts nothing.
LINUX_AGENT_UNIT = "printvendo-agent"
WINDOWS_AGENT_TASK = "PrintvendoAgent"


class CommandFailed(RuntimeError):
    """The machine could not do it, and the operator is told why."""


def _run(command: list[str]) -> str:
    """Run something and raise with what it said, not with a return code.

    An operator reading "restart failed (1)" learns nothing. "Failed to restart
    cups.service: Access denied" tells them the agent is not running as root,
    which is the actual fault and is fixable.
    """
    log.info("running %s", " ".join(command))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT
        )
    except FileNotFoundError as exc:
        raise CommandFailed(f"{command[0]} is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandFailed(f"{command[0]} did not finish within {TIMEOUT}s") from exc

    if result.returncode != 0:
        said = (result.stderr or result.stdout or "").strip()
        raise CommandFailed(said[:300] or f"{command[0]} exited {result.returncode}")
    return (result.stdout or "").strip()


def restart_printing() -> str:
    """Restart whatever prints on this machine.

    CUPS on Linux; the Print Spooler on Windows. This is the one an operator
    reaches for when jobs are queuing and nothing comes out, and it is safe to
    run while the shop is open — a spooler restart loses queued jobs, which are
    exactly the jobs that were not going to print.
    """
    if IS_WINDOWS:
        # `net stop` refuses when other services depend on the spooler;
        # Restart-Service takes them with it and brings them back.
        _run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Restart-Service -Name Spooler -Force -ErrorAction Stop",
        ])
        return "the Windows Print Spooler was restarted"

    _run(["systemctl", "restart", "cups"])

    # Restarting the daemon does not re-enable a printer CUPS has stopped.
    # `printer-error-policy=stop-printer` is the default and the one this agent
    # is built around -- a jam holds the job and marks the printer `disabled`,
    # which is what `waiting.cups_watcher` reads to raise PrinterStuck and close
    # the shop. But nothing then reopens it: the release in `report_recovered`
    # needs a job to succeed, and no job can succeed while the printer is
    # disabled. Without this line the console's restart button did not recover a
    # jammed shop and somebody had to SSH in and type `cupsenable`.
    #
    # Best effort. A printer that is disabled for a reason that has not been
    # cleared simply stops again on the next job, which is the correct outcome
    # and is reported as such.
    printer = Config.load().printer
    if printer:
        try:
            _run(["cupsenable", printer])
            _run(["cupsaccept", printer])
        except Exception as exc:  # noqa: BLE001
            return f"cups was restarted; {printer} could not be re-enabled: {exc}"
        return f"cups was restarted and {printer} re-enabled"

    return "cups was restarted"


def _supervises_us() -> bool:
    """Whether something is set up to start this agent again after it stops.

    Read rather than assumed. `systemctl` and `Get-ScheduledTask` both answer
    cheaply, and the answer is the difference between a restart and a shop that
    goes quiet until somebody drives to it.
    """
    try:
        if IS_WINDOWS:
            _run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"Get-ScheduledTask -TaskName {WINDOWS_AGENT_TASK} -ErrorAction Stop "
                "| Out-Null",
            ])
        else:
            _run(["systemctl", "is-enabled", LINUX_AGENT_UNIT])
    except CommandFailed:
        return False
    return True


def restart_agent() -> str:
    """Restart this agent, by asking the thing that supervises it.

    Never by exiting: a bare `sys.exit` on a machine whose service manager has
    not been told to restart it leaves a shop with no agent at all, and the
    only way back is somebody driving to it.

    Refuses outright when nothing supervises us, because the caller has already
    reported this as succeeded by the time the restart runs -- see the module
    docstring. A failure here is raised before that report is sent.

    The call is detached, because the service manager kills this process as
    part of doing what it was asked. Waiting for a command that is going to
    kill the waiter is how a restart reports itself failed.
    """
    if IS_WINDOWS:
        _detach([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"Stop-ScheduledTask -TaskName {WINDOWS_AGENT_TASK}; "
            f"Start-Sleep -Seconds 2; "
            f"Start-ScheduledTask -TaskName {WINDOWS_AGENT_TASK}",
        ])
        return "this agent is restarting"

    _detach(["systemctl", "restart", LINUX_AGENT_UNIT])
    return "this agent is restarting"


def update_agent() -> str:
    """Fetch the current agent and restart into it.

    Installed from the project's zip on GitHub rather than from a git checkout.
    A shop PC has no git, and a Pi that does has a clone somewhere nobody
    recorded -- one machine had a nested copy from an old tar extract, so a
    pull in one directory and an install from the other updated nothing. A URL
    depends on neither.

    Reported before it happens, like `restart_agent` and for the same reason:
    the process that would report afterwards is the one being replaced. So the
    honest answer is "this is updating", and the console's version, which comes
    from the heartbeat, is what says whether it worked. If it does not change
    within a minute or two, it did not.

    Output goes to a log rather than nowhere, because a detached command that
    fails silently leaves an operator with a shop that did not come back and
    nothing to read.
    """
    installer = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", UPDATE_SOURCE]

    if IS_WINDOWS:
        quoted = " ".join(f"'{part}'" for part in installer)
        _detach([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"& {quoted} *>> '{UPDATE_LOG}'; "
            f"Stop-ScheduledTask -TaskName {WINDOWS_AGENT_TASK}; "
            f"Start-Sleep -Seconds 2; "
            f"Start-ScheduledTask -TaskName {WINDOWS_AGENT_TASK}",
        ])
        return f"updating from {UPDATE_SOURCE}, then restarting"

    joined = " ".join(shlex.quote(part) for part in installer)
    _detach([
        "sh", "-c",
        f"{joined} >> {shlex.quote(str(UPDATE_LOG))} 2>&1; "
        f"systemctl restart {LINUX_AGENT_UNIT} >> {shlex.quote(str(UPDATE_LOG))} 2>&1",
    ])
    return f"updating from {UPDATE_SOURCE}, then restarting"


def _must_be_supervised() -> None:
    """Refuse before the caller commits to saying this worked."""
    if not _supervises_us():
        raise CommandFailed(NOT_A_SERVICE)


# Checked *before* success is reported, not inside the action: by the time
# `restart_agent` runs, the caller has already told the server it worked,
# because in a moment there will be no process left to tell it anything.
restart_agent.precheck = _must_be_supervised
# Same reason, more sharply: an update that installs and then cannot restart
# leaves the shop running the old code with no sign anything happened.
update_agent.precheck = _must_be_supervised


def _detach(command: list[str]) -> None:
    """Start something that will outlive this process.

    On Windows the child must not be in this process's job or console, or the
    service manager stopping us takes the restarter down before it has started
    us again. On Linux `start_new_session` does the same job.
    """
    log.info("detaching %s", " ".join(command))
    if IS_WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        subprocess.Popen(command, creationflags=flags, close_fds=True)
        return
    subprocess.Popen(command, start_new_session=True, close_fds=True)


# What each command does, and whether doing it ends this process. A table
# rather than an if-chain in the loop: adding one is adding a row, and the
# loop cannot forget the second half of the answer.
HANDLERS = {
    RESTART_PRINTING: (restart_printing, False),
    RESTART_AGENT: (restart_agent, True),
    UPDATE_AGENT: (update_agent, True),
}


def run_commands(backend, commands: list[dict]) -> None:
    """Do each of them, and say how it went.

    A command this agent does not recognise is reported failed rather than
    ignored. A newer server asking for something this machine cannot do is a
    real situation — an estate is not upgraded all at once — and an operator
    must see "this machine does not know how to do that" rather than a request
    that vanishes.

    One that fails does not stop the rest: an operator who asked for both
    restarts because printing is broken should get the second one even if the
    first is what is broken.
    """
    for command in commands:
        command_id = command.get("id")
        kind = command.get("command")
        handler = HANDLERS.get(kind)

        if handler is None:
            _report(backend, command_id, False, f"this agent does not know how to {kind}")
            continue

        action, ends_this_process = handler

        # Everything that can refuse this command refuses it here, while there
        # is still somebody to hear the answer.
        precheck = getattr(action, "precheck", None)
        if precheck is not None:
            try:
                precheck()
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                log.error("%s refused: %s", kind, exc)
                _report(backend, command_id, False, str(exc))
                continue

        if ends_this_process:
            # Said before it is done, because the process that would say it
            # afterwards is the one about to be killed.
            _report(backend, command_id, True, None)

        try:
            said = action()
            log.info("%s: %s", kind, said)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            log.error("%s failed: %s", kind, exc)
            if not ends_this_process:
                _report(backend, command_id, False, str(exc))
            continue

        if not ends_this_process:
            _report(backend, command_id, True, None)


def _report(backend, command_id, succeeded: bool, error: str | None) -> None:
    """Telling the server must not become the failure.

    A report that raises inside the print loop would stop an agent that had
    just done exactly what it was asked.
    """
    try:
        backend.report_command(command_id, succeeded=succeeded, error_message=error)
    except Exception as exc:  # noqa: BLE001
        log.error("could not report command %s: %s", command_id, exc)
