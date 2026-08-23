"""Waiting for the paper, not for the command.

`lp` returns when a job is **queued**. Ghostscript returns when it has handed
the data to the spooler. Neither means anything came out — so reporting PRINTED
at that moment tells a student their document is ready while it is still in a
queue behind somebody else's two hundred pages, or while the printer is jammed.

The order's state, and so the student's screen, is only as honest as this.
"""

import pytest

from agent.waiting import (
    JobGone,
    StillQueued,
    cups_job_id,
    wait_for_job,
)

# ── finding the job we just sent ────────────────────────────────────────────


def test_a_cups_job_id_is_read_from_lp_output():
    """`lp` says which job it made, and that is the only handle on it."""
    assert cups_job_id("request id is HP-01-42 (1 file(s))") == "HP-01-42"


def test_a_cups_job_id_survives_a_printer_name_with_dashes():
    assert cups_job_id("request id is Front-Desk-HP-7 (1 file(s))") == "Front-Desk-HP-7"


def test_output_that_names_no_job_is_refused_rather_than_guessed():
    """No id means nothing can be waited for, and pretending otherwise would
    report a job printed the instant it was sent."""
    with pytest.raises(ValueError):
        cups_job_id("lp: Error - scheduler not responding.")


# ── the wait ────────────────────────────────────────────────────────────────


def poller(answers):
    """A queue that empties after a given number of looks."""
    remaining = list(answers)

    def _look() -> bool:
        return remaining.pop(0) if remaining else False

    return _look


def test_it_returns_once_the_job_leaves_the_queue():
    outcome = wait_for_job(poller([True, True, False]), timeout=5, interval=0)

    assert outcome is JobGone


def test_a_job_already_gone_does_not_wait_at_all():
    assert wait_for_job(poller([False]), timeout=5, interval=0) is JobGone


def test_a_job_that_never_leaves_gives_up_and_says_so():
    """A jam, or a printer somebody switched off. Reported as still queued so
    the caller can fail the task -- which is what puts a refund within reach.
    A wait that returned success on timeout would be the silent version of the
    same bug."""
    outcome = wait_for_job(lambda: True, timeout=0.05, interval=0.01)

    assert outcome is StillQueued


def test_it_keeps_the_kiosk_alive_while_it_waits():
    """A two-hundred-page job takes minutes, and a machine that stops
    heartbeating for minutes is a machine an operator is told has gone
    offline."""
    beats = []

    wait_for_job(
        poller([True, True, False]),
        timeout=5,
        interval=0,
        on_tick=lambda: beats.append(1),
    )

    assert beats


def test_a_queue_that_cannot_be_read_is_not_treated_as_empty():
    """`lpstat` failing is not evidence that the job printed. Guessing "done"
    here reports a job as printed on the strength of a broken command."""

    def broken() -> bool:
        raise RuntimeError("lpstat: scheduler not responding")

    assert wait_for_job(broken, timeout=0.05, interval=0.01) is StillQueued
