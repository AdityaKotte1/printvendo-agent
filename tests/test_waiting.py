"""Queued, printing, printed — read off the printer, not guessed.

The states a student sees are only as honest as this file. "Printing" must mean
the printer has the job in its hands, not that the agent sent it: a job sitting
behind somebody else's two hundred pages is **queued**, and telling the student
otherwise is telling them to walk to the shop.

So the queue is polled for a *state*, not a yes/no, and a change is reported the
moment it happens.
"""

import pytest

from agent.waiting import (
    JobState,
    cups_job_id,
    cups_state_from_lpstat,
    watch_job,
    windows_state_from_jobs,
)

# ── finding the job we just sent ────────────────────────────────────────────


def test_a_cups_job_id_is_read_from_lp_output():
    assert cups_job_id("request id is HP-01-42 (1 file(s))") == "HP-01-42"


def test_a_cups_job_id_survives_a_printer_name_with_dashes():
    assert cups_job_id("request id is Front-Desk-HP-7 (1 file(s))") == "Front-Desk-HP-7"


def test_output_that_names_no_job_is_refused_rather_than_guessed():
    """No id means nothing can be watched, and pretending otherwise would
    report the job printed the instant it was sent."""
    with pytest.raises(ValueError):
        cups_job_id("lp: Error - scheduler not responding.")


# ── reading the CUPS queue ──────────────────────────────────────────────────

WAITING = """HP-01-41  student  4096  Sat 23 Aug 2026 09:00:00 PM IST
HP-01-42  student  8192  Sat 23 Aug 2026 09:00:05 PM IST
"""


def test_a_job_in_the_queue_behind_another_is_queued():
    """Ours is second. The printer has not reached it, and a student told
    "printing" would walk to the shop for nothing."""
    state = cups_state_from_lpstat(WAITING, job_id="HP-01-42")

    assert state is JobState.QUEUED


def test_the_job_at_the_head_of_the_queue_is_printing():
    """`lpstat -o` lists oldest first and CUPS works the queue in order, so the
    first line is the one on the printer."""
    state = cups_state_from_lpstat(WAITING, job_id="HP-01-41")

    assert state is JobState.PRINTING


def test_a_job_that_has_left_the_queue_has_printed():
    state = cups_state_from_lpstat(WAITING, job_id="HP-01-99")

    assert state is JobState.GONE


def test_a_job_the_printer_has_stopped_on_is_an_error():
    """CUPS marks a job it cannot proceed with. Reporting that as "printing"
    leaves a student waiting at a machine that has given up."""
    stopped = "HP-01-42  student  8192  Sat 23 Aug 2026 09:00:05 PM IST\n"

    state = cups_state_from_lpstat(stopped, job_id="HP-01-42", printer_stopped=True)

    assert state is JobState.ERROR


# ── reading the Windows spooler ─────────────────────────────────────────────


def windows_job(job_id: int, status: int) -> dict:
    return {"JobId": job_id, "Status": status}


# The spooler's own bits, named here so the test reads as the thing it means.
SPOOLING = 0x00000008
PRINTING = 0x00000010
ERROR = 0x00000002
PAUSED = 0x00000001
OFFLINE = 0x00000020
PAPER_OUT = 0x00000040


def test_a_windows_job_still_spooling_is_queued():
    state = windows_state_from_jobs([windows_job(7, SPOOLING)], ours={7})

    assert state is JobState.QUEUED


def test_a_windows_job_the_printer_is_working_on_is_printing():
    state = windows_state_from_jobs([windows_job(7, PRINTING)], ours={7})

    assert state is JobState.PRINTING


def test_a_windows_job_that_has_left_the_spooler_has_printed():
    state = windows_state_from_jobs([windows_job(8, PRINTING)], ours={7})

    assert state is JobState.GONE


@pytest.mark.parametrize("status", [ERROR, PAPER_OUT, OFFLINE])
def test_a_windows_job_the_printer_cannot_do_is_an_error(status):
    """Out of paper, switched off, or jammed. Each is a student standing at a
    machine that will never produce their document, and each must be reportable
    as a failure rather than as work in progress."""
    state = windows_state_from_jobs([windows_job(7, status)], ours={7})

    assert state is JobState.ERROR


def test_a_paused_windows_job_is_queued_rather_than_failed():
    """Somebody paused the printer. It will resume, so this is not a failure --
    and the long wait is what eventually catches it if it does not."""
    state = windows_state_from_jobs([windows_job(7, PAUSED)], ours={7})

    assert state is JobState.QUEUED


def test_only_our_own_jobs_are_watched():
    """A printer shared with the shop's own computer. Their document is not
    ours to report on, and waiting for it would hold this job's report."""
    state = windows_state_from_jobs(
        [windows_job(1, PRINTING), windows_job(2, PRINTING)], ours=set()
    )

    assert state is JobState.GONE


# ── watching it change ──────────────────────────────────────────────────────


def looker(states):
    remaining = list(states)

    def _look() -> JobState:
        return remaining.pop(0) if remaining else JobState.GONE

    return _look


def test_it_reports_each_state_as_it_happens():
    """Queued, then printing, then gone -- and the student's screen follows."""
    seen: list[JobState] = []

    watch_job(
        looker([JobState.QUEUED, JobState.QUEUED, JobState.PRINTING, JobState.GONE]),
        on_state=seen.append,
        timeout=5,
        interval=0,
    )

    assert seen == [JobState.QUEUED, JobState.PRINTING, JobState.GONE]


def test_a_state_that_has_not_changed_is_not_reported_again():
    """Otherwise a two-hundred-page job posts "printing" every two seconds for
    four minutes."""
    seen: list[JobState] = []

    watch_job(
        looker([JobState.PRINTING] * 5 + [JobState.GONE]),
        on_state=seen.append,
        timeout=5,
        interval=0,
    )

    assert seen == [JobState.PRINTING, JobState.GONE]


def test_it_finishes_when_the_job_is_gone():
    assert watch_job(looker([JobState.GONE]), timeout=5, interval=0) is JobState.GONE


def test_a_job_that_never_finishes_ends_as_still_queued():
    """A jam nobody clears, or a printer switched off at the wall. Reported as
    unfinished so the caller fails the task, which is what puts a refund within
    reach."""
    outcome = watch_job(lambda: JobState.QUEUED, timeout=0.05, interval=0.01)

    assert outcome is JobState.QUEUED


def test_an_error_stops_the_wait_immediately():
    """There is nothing to wait for. The student should be told now, not in
    fifteen minutes."""
    outcome = watch_job(lambda: JobState.ERROR, timeout=5, interval=0)

    assert outcome is JobState.ERROR


def test_a_queue_that_cannot_be_read_is_not_a_finished_job():
    """`lpstat` failing is not evidence that anything printed."""

    def broken() -> JobState:
        raise RuntimeError("lpstat: scheduler not responding")

    assert watch_job(broken, timeout=0.05, interval=0.01) is not JobState.GONE


def test_it_keeps_the_kiosk_alive_while_it_waits():
    """A long job takes minutes, and a machine that goes quiet for minutes is
    one an operator is told has gone offline."""
    beats = []

    watch_job(
        looker([JobState.PRINTING, JobState.PRINTING, JobState.GONE]),
        timeout=5,
        interval=0,
        on_tick=lambda: beats.append(1),
    )

    assert beats


# ── a job the spooler keeps after printing ──────────────────────────────────
#
# Windows printers have a "Keep printed documents" setting, and a job also sits
# briefly at PRINTED before the spooler drops it. `EnumJobs` keeps returning it,
# with 0x80 set and no PRINTING bit -- which the reader treated as QUEUED, so
# the job never reached GONE. The wait then ran its full fifteen minutes and
# `print_task` raised PrinterStuck: the student's paper was in their hand, the
# task was reported FAILED, and **the shop was put into maintenance**. Every
# successful print closed the kiosk. Found by watching a real Windows queue.

PRINTED = 0x00000080
RETAINED = 0x00002000
COMPLETE = 0x00001000
DELETED = 0x00000100
USER_INTERVENTION = 0x00000800


@pytest.mark.parametrize("status", [PRINTED, RETAINED, COMPLETE, DELETED])
def test_a_windows_job_the_spooler_has_finished_is_done(status):
    """It came out. That the spooler is still listing it is a display setting,
    not a fact about the paper."""
    state = windows_state_from_jobs([windows_job(7, status)], ours={7})

    assert state is JobState.GONE


def test_a_printed_job_is_done_even_while_the_printing_bit_lingers():
    """The spooler sets PRINTED alongside PRINTING for a moment as it finishes.
    Done wins: the alternative is calling a finished job "printing" for ever."""
    state = windows_state_from_jobs([windows_job(7, PRINTED | PRINTING)], ours={7})

    assert state is JobState.GONE


def test_a_job_waiting_on_a_person_is_an_error():
    """"Load paper in tray 2" is a prompt on the machine, and nobody at a kiosk
    is going to answer it. It is a failure, not a queue."""
    state = windows_state_from_jobs([windows_job(7, USER_INTERVENTION)], ours={7})

    assert state is JobState.ERROR


def test_one_finished_job_does_not_make_a_second_one_finished():
    """Two of ours in the queue: one printed, one still going. The pass is not
    over until both are, or a job behind a finished one is reported printed
    while it is still spooling."""
    state = windows_state_from_jobs(
        [windows_job(7, PRINTED), windows_job(8, PRINTING)], ours={7, 8}
    )

    assert state is JobState.PRINTING


# ── the printer, not the queue ──────────────────────────────────────────────


def test_a_printer_that_is_still_printing_is_not_idle():
    """`lpstat -o` empties when CUPS has finished *sending*. A fifteen-page job
    streams into the printer's buffer in seconds and leaves the queue while page
    five is coming out -- so the next student's job went on top of it and two
    people's pages arrived in one pile."""
    from agent.waiting import cups_is_idle

    assert cups_is_idle("printer Shop now printing Shop-42.  enabled since Mon") is False


def test_an_idle_printer_is_idle():
    from agent.waiting import cups_is_idle

    assert cups_is_idle("printer Shop is idle.  enabled since Mon 01 Sep") is True


def test_output_that_cannot_be_read_is_not_idle():
    """"I could not tell" must not mean "finished" -- that is how the next job
    starts on top of this one."""
    from agent.waiting import cups_is_idle

    assert cups_is_idle("") is False
    assert cups_is_idle("lpstat: Bad file descriptor") is False


def test_waiting_returns_once_the_printer_stops():
    from agent.waiting import wait_until_idle

    states = iter([
        "printer Shop now printing Shop-42.",
        "printer Shop now printing Shop-42.",
        "printer Shop is idle.",
    ])

    assert wait_until_idle("Shop", interval=0, look=lambda: next(states)) is True


def test_waiting_gives_up_rather_than_blocking_for_ever():
    """A timeout here is a slower shop, never a wrong report: the job has
    already left the queue and has already been judged."""
    from agent.waiting import wait_until_idle

    assert (
        wait_until_idle(
            "Shop", timeout=0, interval=0, look=lambda: "printer Shop now printing Shop-42."
        )
        is False
    )
