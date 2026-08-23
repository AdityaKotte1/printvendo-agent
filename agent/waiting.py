"""Waiting for the paper rather than for the command.

`lp` returns when a job is **queued**; Ghostscript returns when it has handed
the data to the spooler. Neither means anything came out. Reporting PRINTED at
that moment tells a student their document is ready while it is still behind
somebody else's two hundred pages, or while the printer is jammed — and the
order's state, and so their screen, is only as honest as this.

So the agent watches the queue and reports when the job leaves it.

**Not leaving is a failure, never a success.** A wait that gave up and said
"done" would be the silent version of the same bug, and a job the printer never
managed has to be reportable as failed — that is what puts a refund within
reach.

**A queue that cannot be read is not an empty queue.** `lpstat` failing is not
evidence that anything printed.
"""

import logging
import re
import subprocess
import time
from collections.abc import Callable
from enum import Enum

log = logging.getLogger("agent")

# How long a job may sit before the agent stops believing in it. Long, because
# a genuine two-hundred-page duplex job on a small laser printer is slow, and a
# false failure refunds somebody who got their document.
DEFAULT_TIMEOUT = 15 * 60

# Between looks. Frequent enough that a short job is not reported late, rare
# enough that a Pi is not spending its evening running `lpstat`.
DEFAULT_INTERVAL = 2.0

# `lp` answers: "request id is HP-01-42 (1 file(s))". The printer's name may
# contain dashes, so the id is everything up to the last one.
_REQUEST_ID = re.compile(r"request id is (\S+)")


class Outcome(Enum):
    """Whether the printer is finished with it."""

    GONE = "gone"
    QUEUED = "queued"


JobGone = Outcome.GONE
StillQueued = Outcome.QUEUED


def cups_job_id(lp_output: str) -> str:
    """The job `lp` just made.

    Raises rather than returning None when there is no id: without one there is
    nothing to wait for, and carrying on would report the job printed the
    instant it was sent.
    """
    match = _REQUEST_ID.search(lp_output)
    if not match:
        raise ValueError(f"lp did not say which job it made: {lp_output.strip()[:200]}")
    return match.group(1)


def wait_for_job(
    still_queued: Callable[[], bool],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    on_tick: Callable[[], None] | None = None,
) -> Outcome:
    """Look until the job is gone, or until we stop believing in it.

    `still_queued` is passed in rather than chosen here so the loop can be
    driven without a printer -- the platform-specific half is the argument.

    `on_tick` is where the caller heartbeats. A machine that goes quiet for the
    four minutes a long job takes is a machine an operator is told has gone
    offline.
    """
    deadline = time.monotonic() + timeout

    while True:
        try:
            if not still_queued():
                return JobGone
        except Exception as exc:  # noqa: BLE001
            # Not evidence of anything. Keep looking until the deadline, and
            # then report it as still queued rather than as printed.
            log.warning("could not read the print queue: %s", exc)

        if time.monotonic() >= deadline:
            return StillQueued

        if on_tick is not None:
            on_tick()
        time.sleep(interval)


def cups_still_queued(job_id: str) -> Callable[[], bool]:
    """Whether CUPS still has this job.

    `lpstat -W not-completed` lists what has not finished; the job leaving that
    list is the completion signal. A non-zero exit raises, which the loop above
    treats as "unknown", not as "finished".
    """

    def _look() -> bool:
        result = subprocess.run(
            ["lpstat", "-W", "not-completed"], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "lpstat failed")
        return any(line.startswith(job_id) for line in result.stdout.splitlines())

    return _look


def windows_job_ids(printer: str) -> set[int]:
    """The spooler's job ids for this printer, right now."""
    import win32print

    handle = win32print.OpenPrinter(printer)
    try:
        return {job["JobId"] for job in win32print.EnumJobs(handle, 0, 99, 1)}
    finally:
        win32print.ClosePrinter(handle)


def windows_still_queued(printer: str, before: set[int]) -> Callable[[], bool]:
    """Whether the spooler still holds a job that was not there before we
    printed.

    By **job id**, snapshotted before the print, not by document name: the
    spooler calls every Ghostscript job "Ghostscript output" regardless of the
    file, so matching on the filename matched nothing and the wait returned
    instantly -- a no-op that looked exactly like a working one. Verified by
    watching a real queue.

    Ids rather than "is the queue empty" so a printer shared with a shop's own
    computer does not have this agent waiting on somebody else's document.
    """

    def _look() -> bool:
        return bool(windows_job_ids(printer) - before)

    return _look
