"""`printvendo-agent <command>` — enrol a machine, check it, run it.

Written for somebody standing at a shop counter with the shop waiting, or SSHed
into a headless Pi over a phone hotspot. So: three commands, one question asked
only when it cannot be answered by looking, and `check` that says what is wrong
in a sentence rather than leaving an installer to read a stack trace.

    printvendo-agent enrol --code dve_...     # once, at install
    printvendo-agent check                    # is this machine ready
    printvendo-agent run                      # the loop; what the service runs
"""

import argparse
import logging
import platform
import socket
import sys
import time
from datetime import UTC, datetime

import httpx

from agent.api import Backend, enrol
from agent.config import Config, config_path, default_printer, printers, ssh_host
from agent.pools import pick, pool_for
from agent.printing import IS_WINDOWS, ghostscript_path
from agent.runner import run_once
from agent.single import AlreadyRunning, only_one_agent
from agent.waiting import queue_depth

# Reported on every heartbeat and shown against the kiosk in the console, which
# makes it the only way to tell from a desk which build a shop is running.
# It sat at 1.0.0 through three releases that changed behaviour -- including the
# one that fixed an agent locking itself out of its own kiosk -- so "is the new
# version deployed?" could only be answered by SSHing in. Bump it whenever this
# package changes, or the field is worse than absent: it looks like an answer.
VERSION = "1.6.0"

# How often to ask when nothing has woken us. The socket makes a queued job
# prompt; this is the floor, and it is what kept every kiosk working before the
# socket existed.
POLL_SECONDS = 15
HEARTBEAT_SECONDS = 60

log = logging.getLogger("agent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="printvendo-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    joined = commands.add_parser("enrol", help="claim this machine for a kiosk")
    joined.add_argument("--code", required=True, help="the one-time enrolment code")
    joined.add_argument("--api", default=None, help=f"backend URL (default {Config().api_url})")
    joined.add_argument(
        "--printer",
        default=None,
        help="which printer to use; only needed when the machine has more than one",
    )
    # A xerox counter runs two mono machines and a colour one off one agent.
    # Repeatable, and the order is the order they are preferred in when both
    # are idle -- so an owner can say which machine they would rather wear out.
    joined.add_argument(
        "--bw",
        action="append",
        default=None,
        metavar="NAME",
        help="a printer for black-and-white work; repeat for more than one",
    )
    joined.add_argument(
        "--colour",
        "--color",
        action="append",
        default=None,
        dest="colour",
        metavar="NAME",
        help="a printer for colour work; repeat for more than one",
    )

    commands.add_parser("check", help="is this machine ready to print")
    commands.add_parser("printers", help="list the printers this machine can see")

    running = commands.add_parser("run", help="claim and print, for ever")
    running.add_argument("--once", action="store_true", help="one pass, then stop")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if args.command == "printers":
        return _list_printers()
    if args.command == "enrol":
        return _enrol(args)
    if args.command == "check":
        return _check()
    return _run(once=args.once)


def _list_printers() -> int:
    found = printers()
    if not found:
        print("No printers found.")
        print(
            "  On a Pi: install CUPS and add the printer first (`lpstat -e` should list it)."
            if not IS_WINDOWS
            else "  On Windows: add the printer in Settings first."
        )
        return 1
    for name in found:
        print(name)
    return 0


def _enrol(args) -> int:
    config = Config.load()
    if args.api:
        config.api_url = args.api

    bw = list(args.bw or [])
    colour = list(args.colour or [])
    if bw or colour:
        missing = [name for name in (*bw, *colour) if name not in printers()]
        if missing:
            print("This machine has no printer called: " + ", ".join(missing))
            print("What it does have:")
            for name in printers():
                print(f"  {name}")
            return 1
        config.printers_bw = bw
        config.printers_colour = colour
        # The single printer stays as the fallback for anything that asks
        # without knowing about pools, and so an operator can drop back to one
        # machine by clearing the pools alone.
        printer = args.printer or config.printer or (bw or colour)[0]
    else:
        printer = args.printer or config.printer or default_printer()

    if not printer:
        found = printers()
        if not found:
            print("No printer found on this machine. Add one, then run this again.")
            return 1
        # Only asked when it genuinely cannot be answered: guessing between two
        # printers means somebody's dissertation on the label machine.
        print("This machine has more than one printer. Choose one with --printer:")
        for name in found:
            print(f"  {name}")
        return 1

    # A name that survives re-enrolment, so an operator can tell one physical
    # box from another over SSH.
    device_key = config.device_key or f"{socket.gethostname()}-{platform.machine()}"

    try:
        issued = enrol(
            config.api_url, code=args.code, agent_version=VERSION, device_key=device_key
        )
    except Exception as exc:  # noqa: BLE001 - the message is for a person
        print(f"Enrolment failed: {exc}")
        return 1

    config.token = issued["token"]
    config.printer = printer
    config.device_key = issued.get("device_key", device_key)
    written = config.save()

    print(f"Enrolled. This machine now prints for its kiosk using '{printer}'.")
    print(f"Settings saved to {written}")
    print("Next: printvendo-agent check")
    return 0


def _token_was_rejected(exc: Exception) -> bool:
    """Did the server refuse this machine's credential?

    Re-enrolling a kiosk rotates the token on its existing device row, so a
    process that was already running keeps polling with one the server has just
    invalidated -- 401 on every call, for ever, while the config file on disk
    holds a perfectly good replacement.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401


def _looks_like_a_wrong_clock(exc: Exception) -> bool:
    """Does this failure smell of a clock rather than a cable?

    TLS says "certificate is not yet valid" when the machine believes it is
    earlier than the certificate's start date, and "certificate has expired"
    when it believes it is later. Both are almost always this, not a genuinely
    bad certificate, on hardware with no clock of its own.
    """
    text = str(exc).lower()
    return "certificate is not yet valid" in text or "certificate has expired" in text


def _check() -> int:
    """Everything that has to be true before a student's job can come out.

    Each failure names the fix. An installer who has to interpret a traceback
    at a counter will ring somebody instead, and the shop stays shut.
    """
    config = Config.load()
    problems: list[str] = []

    if not config.token:
        problems.append(
            "This machine is not enrolled. Run: printvendo-agent enrol --code dve_..."
        )
    if not config.any_printer:
        problems.append("No printer chosen. Run: printvendo-agent printers")
    else:
        # Every machine in every pool, not just the fallback. A pool naming a
        # printer CUPS has never heard of fails one kind of job and only that
        # kind -- the sort of fault a student finds rather than an installer.
        here = printers()
        for name in config.every_printer:
            if name not in here:
                problems.append(
                    f"The printer '{name}' is not there any more. "
                    "Run: printvendo-agent printers"
                )
        if config.printers_bw and not config.printers_colour:
            problems.append(
                "No colour printer is configured, so colour jobs will fail. "
                "Add one with: printvendo-agent enrol --colour NAME"
            )

    if IS_WINDOWS:
        try:
            ghostscript_path()
        except RuntimeError as exc:
            problems.append(str(exc))

    if config.token:
        try:
            Backend(config.api_url, config.token).heartbeat(agent_version=VERSION)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Could not reach {config.api_url}: {exc}")
            # A Pi has no battery-backed clock, so one that reboots without
            # network sits at the time it last knew and rejects every
            # certificate as "not yet valid". That reads as an unreachable
            # server, and somebody goes looking at the wifi. Named here because
            # `check` exists to say what to do, not what went wrong.
            if _looks_like_a_wrong_clock(exc):
                problems.append(
                    "That looks like this machine's clock, not the network. "
                    "It thinks it is " + datetime.now(UTC).strftime("%d %b %Y %H:%M UTC")
                    + ". Fix it with: sudo timedatectl set-ntp true"
                )

    if problems:
        print("Not ready:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"Ready. Printing to '{config.printer}' for {config.api_url}.")
    print(f"Settings: {config_path()}")
    return 0


def _run(*, once: bool = False) -> int:
    config = Config.load()
    if not config.ready:
        print("This machine is not set up yet. Run: printvendo-agent check")
        return 1

    try:
        with only_one_agent():
            return _loop(config, once=once)
    except AlreadyRunning:
        # Two agents both claim work. Nothing prints twice -- the server hands
        # a task out once -- but they submit independently, so the shop's queue
        # ends up several jobs deep with no gap, and a student walking up while
        # somebody else's fifteen pages come out takes the wrong sheets.
        #
        # It happens by running this over SSH to watch it work and walking away
        # with it still running beside the service. So it is refused rather
        # than left to be remembered.
        print("Another printvendo-agent is already running on this machine.")
        print("Only one may print, or students get each other's pages.")
        print("  what is running:            ps aux | grep printvendo-agent")
        print("  watch the service instead:  journalctl -u printvendo-agent -f")
        return 1


def _loop(config: Config, *, once: bool = False) -> int:

    backend = Backend(config.api_url, config.token)

    def choose(task) -> str | None:
        """Which machine this job goes to, asked once per job.

        Re-read from `config` each time rather than captured, so a pool changed
        by a re-enrol is picked up on the next job rather than at the next
        restart.
        """
        return pick(pool_for(config.pools, colour=task.colour), queue_depth)

    if config.pools.configured:
        log.info(
            "printing for %s -- mono: %s | colour: %s",
            config.api_url,
            ", ".join(config.printers_bw) or "none",
            ", ".join(config.printers_colour) or "none",
        )
    else:
        log.info("printing to %s for %s", config.printer, config.api_url)

    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            try:
                backend.heartbeat(agent_version=VERSION, ssh_host=ssh_host())
                last_heartbeat = now
            except Exception as exc:  # noqa: BLE001
                # Not fatal. A missed heartbeat makes the kiosk look offline to
                # an operator; refusing to print over it would make it actually
                # offline.
                log.warning("heartbeat failed: %s", exc)

        def beat(backend: Backend = backend) -> None:
            """Called while the printer works, so a long job does not make this
            machine look offline.

            `backend` is bound as a default rather than captured, because the
            loop replaces it when the server rejects a rotated token. Binding it
            here means this pass beats with the client it was given, and the
            next pass gets a fresh one.
            """
            nonlocal last_heartbeat
            if time.monotonic() - last_heartbeat < HEARTBEAT_SECONDS:
                return
            try:
                backend.heartbeat(agent_version=VERSION, ssh_host=ssh_host())
                last_heartbeat = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat failed: %s", exc)

        try:
            run_once(backend, printer=config.printer, choose=choose, on_tick=beat)
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end the loop
            log.error("this pass failed: %s", exc)
            if _token_was_rejected(exc):
                # Somebody re-ran the installer. Read the file again rather than
                # polling a dead credential until a person works out why a shop
                # went quiet -- the new token is already sitting there.
                config = Config.load()
                if config.token and config.token != backend.token:
                    log.warning("the token was replaced; picking up the new one")
                    backend = Backend(config.api_url, config.token)
                else:
                    log.error(
                        "this machine's token was refused and the one on disk is "
                        "the same. Re-enrol it: printvendo-agent enrol --code dve_..."
                    )

        if once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
