"""Queued, printing, printed — read off the printer rather than guessed.

The three words a student sees come from here, so each has to mean what it
says. **Printing** means the printer has this job in its hands; a job sitting
behind somebody else's two hundred pages is **queued**, and telling the student
otherwise sends them walking to the shop for nothing. **Printed** means the job
left the queue.

`lp` returns when a job is queued and Ghostscript returns when the spooler has
the data. Neither is a state worth reporting, which is why the agent polls the
queue for one instead of trusting either.

Two rules hold everywhere below:

**A queue that cannot be read is not an empty queue.** `lpstat` failing is not
evidence that anything printed.

**Not finishing is a failure, never a success.** A job the printer gave up on
has to be reportable as failed -- that is what puts a refund within reach -- so
a wait that timed out and said "done" would be the silent version of the same
bug.
"""

import logging
import re
import subprocess
import time
from collections.abc import Callable
from enum import Enum

log = logging.getLogger("agent")

# How long a job may sit before the agent stops believing in it. Long, because
# a genuine two-hundred-page duplex job on a small laser printer is slow and a
# false failure refunds somebody who got their document.
DEFAULT_TIMEOUT = 15 * 60

# Between looks. Frequent enough that a short job is not reported late, rare
# enough that a Pi is not spending its evening running `lpstat`.
DEFAULT_INTERVAL = 2.0

# `lp` answers: "request id is HP-01-42 (1 file(s))". A printer's name may
# contain dashes, so the id is the whole token.
_REQUEST_ID = re.compile(r"request id is (\S+)")

# The Windows spooler's job status bits (winspool.h). Named here because
# `status & 0x40` at a call site is a number nobody can check.
JOB_STATUS_PAUSED = 0x00000001
JOB_STATUS_ERROR = 0x00000002
JOB_STATUS_SPOOLING = 0x00000008
JOB_STATUS_PRINTING = 0x00000010
JOB_STATUS_OFFLINE = 0x00000020
JOB_STATUS_PAPEROUT = 0x00000040
JOB_STATUS_BLOCKED = 0x00000200

# Everything that means "this will not print until somebody does something
# physical". Paused is deliberately not here: it resumes.
WINDOWS_TROUBLE = (
    JOB_STATUS_ERROR | JOB_STATUS_OFFLINE | JOB_STATUS_PAPEROUT | JOB_STATUS_BLOCKED
)


class JobState(Enum):
    """Where a job is, as far as the printer is concerned."""

    QUEUED = "queued"
    PRINTING = "printing"
    GONE = "gone"
    # The printer has stopped on it: jammed, out of paper, switched off.
    ERROR = "error"


def cups_job_id(lp_output: str) -> str:
    """The job `lp` just made.

    Raises rather than returning None when there is no id: without one there is
    nothing to watch, and carrying on would report the job printed the instant
    it was sent.
    """
    match = _REQUEST_ID.search(lp_output)
    if not match:
        raise ValueError(f"lp did not say which job it made: {lp_output.strip()[:200]}")
    return match.group(1)


def cups_state_from_lpstat(
    listing: str, *, job_id: str, printer_stopped: bool = False
) -> JobState:
    """Read one job's state out of `lpstat -o`.

    A job that is not in the listing has finished -- `lpstat -o` shows only what
    has not. A job that is in it is either being printed or waiting its turn,
    and CUPS works a queue in order, so **the first line is the one on the
    printer**. That is how the agent this replaces did it, and it is right for
    the case that matters: one kiosk, one printer, jobs in order.
    """
    active = [line.split()[0] for line in listing.splitlines() if line.strip()]

    if job_id not in active:
        return JobState.GONE
    if printer_stopped:
        # CUPS has given up on this printer. The job will sit there for ever
        # and a student watching "printing" is watching nothing.
        return JobState.ERROR
    return JobState.PRINTING if active[0] == job_id else JobState.QUEUED


def windows_state_from_jobs(jobs: list[dict], *, ours: set[int]) -> JobState:
    """Read our job's state out of what `EnumJobs` returned.

    `ours` is the set of job ids that appeared after we printed. By id, because
    the spooler calls **every** Ghostscript job "Ghostscript output" -- matching
    on the document name matched nothing and made the whole wait a no-op that
    looked exactly like a working one. Found by watching a real queue.

    Ids also mean a printer shared with the shop's own computer does not have
    this agent waiting on somebody else's document.
    """
    mine = [job for job in jobs if job.get("JobId") in ours]
    if not mine:
        return JobState.GONE

    statuses = [int(job.get("Status") or 0) for job in mine]

    if any(status & WINDOWS_TROUBLE for status in statuses):
        return JobState.ERROR
    if any(status & JOB_STATUS_PRINTING for status in statuses):
        return JobState.PRINTING
    return JobState.QUEUED


def watch_job(
    look: Callable[[], JobState],
    *,
    on_state: Callable[[JobState], None] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    on_tick: Callable[[], None] | None = None,
) -> JobState:
    """Follow a job until it is gone, has failed, or we stop believing in it.

    `on_state` is called **when the state changes**, never on every look: a
    two-hundred-page job would otherwise post "printing" every two seconds for
    four minutes.

    `look` is passed in rather than chosen here, so the loop can be driven
    without a printer -- the platform-specific half is the argument, and the
    part that decides what a student sees is the part under test.
    """
    deadline = time.monotonic() + timeout
    last: JobState | None = None
    current = JobState.QUEUED

    while True:
        try:
            current = look()
        except Exception as exc:  # noqa: BLE001
            # Not evidence of anything. Keep looking until the deadline, and
            # report whatever we last knew rather than "printed".
            log.warning("could not read the print queue: %s", exc)
        else:
            if current is not last:
                last = current
                if on_state is not None:
                    on_state(current)

            if current in (JobState.GONE, JobState.ERROR):
                return current

        if time.monotonic() >= deadline:
            return last or JobState.QUEUED

        if on_tick is not None:
            on_tick()
        time.sleep(interval)


# ── the platform-specific halves ────────────────────────────────────────────


def cups_watcher(job_id: str, printer: str) -> Callable[[], JobState]:
    """`lpstat -o` for the queue, `lpstat -p` for whether it has stopped."""

    def _look() -> JobState:
        listing = subprocess.run(
            ["lpstat", "-o"], capture_output=True, text=True, timeout=30
        )
        if listing.returncode != 0:
            raise RuntimeError(listing.stderr.strip() or "lpstat -o failed")

        stopped = False
        state = subprocess.run(
            ["lpstat", "-p", printer], capture_output=True, text=True, timeout=30
        )
        if state.returncode == 0:
            stopped = "disabled" in state.stdout or "stopped" in state.stdout

        return cups_state_from_lpstat(
            listing.stdout, job_id=job_id, printer_stopped=stopped
        )

    return _look


def windows_job_ids(printer: str) -> set[int]:
    """The spooler's job ids for this printer, right now."""
    import win32print

    handle = win32print.OpenPrinter(printer)
    try:
        return {job["JobId"] for job in win32print.EnumJobs(handle, 0, 99, 1)}
    finally:
        win32print.ClosePrinter(handle)


def windows_watcher(printer: str, ours: set[int]) -> Callable[[], JobState]:
    def _look() -> JobState:
        import win32print

        handle = win32print.OpenPrinter(printer)
        try:
            jobs = win32print.EnumJobs(handle, 0, 99, 1)
        finally:
            win32print.ClosePrinter(handle)

        return windows_state_from_jobs(list(jobs), ours=ours)

    return _look
