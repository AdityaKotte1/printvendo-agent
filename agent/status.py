"""What the screen above the counter is told, and nothing more.

A pure function over what the agent already knows, so the shop display needs no
credential, no second enrolment and no route of its own to the backend. The
agent fetches a heartbeat every sixty seconds and, until now, threw the answer
away; that answer is most of this document.

**There is no filename field, and there must never be one.** `api.download`
already saves a job as `prt_abc.pdf` rather than under the student's own name,
because `lp` submits under the filename on disk and CUPS keeps job history long
after removing the document. A forty-inch screen above a shop counter reading
"Medical Results Ravi Kumar.pdf" is that same leak, enlarged and in public. The
field's absence is the mechanism -- the display cannot render what it is never
sent, however it is later edited.
"""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Job:
    """The job on the machine right now, as the counter should see it."""

    state: str
    sheets: int | None = None


@dataclass(frozen=True)
class Snapshot:
    """Everything the agent knows that the screen is allowed to show.

    `heartbeat` is the last successful answer from the backend, or None if
    there has never been one. `connected` says whether the *most recent*
    attempt worked -- the two differ, and that difference is the whole point:
    a shop that has been printing all day and lost its line for a minute should
    show its real paper count, greyed, not a blank.
    """

    kiosk_name: str | None = None
    sheets_remaining: int | None = None
    paper_capacity: int | None = None
    queue_depth: int | None = None
    connected: bool = False
    printer_ok: bool = True
    job: Job | None = None
    updated_at: datetime | None = None


def document(snapshot: Snapshot, *, now: datetime | None = None) -> dict:
    """The JSON the display polls.

    Every value is optional on purpose. The screen goes up the moment the agent
    starts, which is before the first heartbeat has returned -- and a display
    that renders nothing until the network answers is a black rectangle above a
    counter, which reads as a broken machine rather than a starting one.
    """
    now = now or datetime.now(UTC)

    paper = None
    if snapshot.sheets_remaining is not None and snapshot.paper_capacity:
        # Guarded against a capacity of zero as well as None: a shop whose tray
        # size was never set would otherwise divide by it. A bar with no
        # denominator is not drawn at all rather than drawn full, because full
        # is the one reading that stops somebody refilling.
        paper = {
            "remaining": snapshot.sheets_remaining,
            "capacity": snapshot.paper_capacity,
            "fraction": min(
                1.0, max(0.0, snapshot.sheets_remaining / snapshot.paper_capacity)
            ),
        }

    return {
        "kiosk_name": snapshot.kiosk_name,
        # Online means "this shop can take a job right now", which is the
        # question a student in front of it is asking -- not "the process is
        # running", which they can see from the screen being on.
        "online": bool(snapshot.connected and snapshot.printer_ok),
        "connected": snapshot.connected,
        "printer_ok": snapshot.printer_ok,
        "paper": paper,
        "queue_depth": snapshot.queue_depth,
        "job": {"state": snapshot.job.state, "sheets": snapshot.job.sheets}
        if snapshot.job is not None
        else None,
        # How old the figures are, so the page can say "not connected" without
        # having to keep its own clock or guess how long a poll has been failing.
        "as_of": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
        "served_at": now.isoformat(),
    }
