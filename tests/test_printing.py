"""Finding the things this machine prints with.

The command builders are exercised elsewhere. What is here is the part that
depends on how a machine happens to be set up, which is where the surprises
live.
"""

import pytest

# ── finding Ghostscript when PATH does not have it ──────────────────────────


def test_ghostscript_is_found_under_program_files_without_path(tmp_path, monkeypatch):
    """Its Windows installer does not add itself to PATH, so a perfectly good
    install was invisible to `which` and the agent reported "not installed"
    about software sitting right there."""
    from agent import printing

    gs = tmp_path / "gs" / "gs10.07.0" / "bin"
    gs.mkdir(parents=True)
    (gs / "gswin64c.exe").write_text("")

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nothing"))

    assert printing._ghostscript_in_program_files() == str(gs / "gswin64c.exe")


def test_the_newest_ghostscript_wins(tmp_path, monkeypatch):
    """An upgraded machine keeps the old directory. Running the older binary is
    a subtler fault than running none: it prints, slightly differently, and
    nobody knows why."""
    from agent import printing

    for version in ("gs9.55.0", "gs10.07.0", "gs10.02.1"):
        where = tmp_path / "gs" / version / "bin"
        where.mkdir(parents=True)
        (where / "gswin64c.exe").write_text("")

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nothing"))

    assert "gs10.07.0" in printing._ghostscript_in_program_files()


def test_nothing_is_found_when_it_is_genuinely_absent(tmp_path, monkeypatch):
    from agent import printing

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))

    assert printing._ghostscript_in_program_files() is None


# ── giving up on a job takes our copy back out of the Windows queue ─────────
#
# Left in the spooler, a job the printer stopped on prints whenever paper goes
# in -- after the student was told it failed, and possibly refunded. One shop
# printed a stack of jobs that way with no new orders anywhere.


def _windows_job():
    from agent.printing import Task

    return Task.from_response(
        {
            "task_id": "tsk_w",
            "document_id": "doc_w",
            "filename": "tsk_w.pdf",
            "page_count": 2,
            "copies": 1,
            "duplex": False,
            "colour": False,
            "page_range": None,
            "expected_sheets": 2,
        }
    )


def _windows_print(monkeypatch, outcome):
    """Drive the Windows path with no Ghostscript and no spooler."""
    import subprocess as sp

    from agent import printing

    cancelled: list = []
    queue = iter([set(), {7}])
    monkeypatch.setattr(printing, "IS_WINDOWS", True)
    monkeypatch.setattr(printing, "build_command", lambda *a, **k: ["gs"])
    monkeypatch.setattr(
        printing.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", "")
    )
    monkeypatch.setattr(printing, "windows_job_ids", lambda printer: next(queue))
    monkeypatch.setattr(printing, "windows_watcher", lambda printer, ours: lambda: outcome)
    monkeypatch.setattr(printing, "watch_job", lambda watch, **_: outcome)
    monkeypatch.setattr(
        printing,
        "cancel_windows_jobs",
        lambda printer, ids: cancelled.append((printer, set(ids))),
    )
    return cancelled


def test_a_big_job_is_allowed_longer_to_print_than_a_small_one():
    """Fifteen minutes is generous for a letter and far too short for a thesis
    on an inkjet. The allowance follows the server's sheet count."""
    from agent.printing import printing_allowance
    from agent.waiting import DEFAULT_TIMEOUT

    assert printing_allowance(1) == DEFAULT_TIMEOUT
    assert printing_allowance(28) > DEFAULT_TIMEOUT
    assert printing_allowance(56) > printing_allowance(28)


def test_the_allowance_reaches_the_wait(monkeypatch, tmp_path):
    from agent import printing
    from agent.printing import print_task, printing_allowance
    from agent.waiting import JobState

    _windows_print(monkeypatch, JobState.GONE)
    seen: dict = {}

    def watch(_look, **kwargs):
        seen.update(kwargs)
        return JobState.GONE

    monkeypatch.setattr(printing, "watch_job", watch)

    print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert seen["printing_timeout"] == printing_allowance(2)


def test_a_job_the_printer_stopped_on_is_taken_back_out_of_the_queue(
    monkeypatch, tmp_path
):
    from agent.printing import PrinterStuck, print_task
    from agent.waiting import JobState

    cancelled = _windows_print(monkeypatch, JobState.ERROR)

    with pytest.raises(PrinterStuck):
        print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert cancelled == [("Shop", {7})]


def test_a_job_that_never_started_is_taken_back_out_too(monkeypatch, tmp_path):
    from agent.printing import PrinterStuck, print_task
    from agent.waiting import JobState

    cancelled = _windows_print(monkeypatch, JobState.QUEUED)

    with pytest.raises(PrinterStuck):
        print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert cancelled == [("Shop", {7})]


def test_a_job_still_printing_when_the_wait_ends_is_left_to_finish(
    monkeypatch, tmp_path
):
    """Still reported, but cutting it off would hand the student half a
    document."""
    from agent.printing import PrinterStuck, print_task
    from agent.waiting import JobState

    cancelled = _windows_print(monkeypatch, JobState.PRINTING)

    with pytest.raises(PrinterStuck):
        print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert cancelled == []


def test_a_job_that_printed_is_not_touched(monkeypatch, tmp_path):
    from agent.printing import print_task
    from agent.waiting import JobState

    cancelled = _windows_print(monkeypatch, JobState.GONE)

    print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert cancelled == []


def _cups_print(monkeypatch, end):
    """Drive the CUPS path with no lp, no queue and no scheduler."""
    import subprocess as sp

    from agent import printing
    from agent.waiting import JobState

    asked: list = []
    monkeypatch.setattr(printing, "IS_WINDOWS", False)
    monkeypatch.setattr(printing, "build_command", lambda *a, **k: ["lp"])
    monkeypatch.setattr(
        printing.subprocess,
        "run",
        lambda *a, **k: sp.CompletedProcess(a, 0, "request id is Shop-42 (1 file(s))", ""),
    )
    monkeypatch.setattr(printing, "cups_watcher", lambda job_id, printer: lambda: JobState.GONE)
    monkeypatch.setattr(printing, "watch_job", lambda watch, **_: JobState.GONE)
    monkeypatch.setattr(printing, "wait_until_idle", lambda printer: True)
    monkeypatch.setattr(printing, "wait_for_paper", lambda task, **kw: None)

    def ended(job_id):
        asked.append(job_id)
        return end

    monkeypatch.setattr(printing, "cups_job_end", ended)
    return asked


@pytest.mark.parametrize("outcome", ["ABORTED", "CANCELLED"])
def test_a_job_cups_gave_up_on_is_not_reported_printed(monkeypatch, tmp_path, outcome):
    """It left the queue exactly as a printed job does. Only CUPS knows the
    difference, so the agent asks -- and a failure is what reaches a refund."""
    from agent.printing import PrinterStuck, print_task
    from agent.waiting import JobEnd, JobEnding

    asked = _cups_print(monkeypatch, JobEnding(JobEnd[outcome]))

    with pytest.raises(RuntimeError) as raised:
        print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert asked == ["Shop-42"]
    # One job ending badly is not a jammed shop: the next file may print fine.
    assert not isinstance(raised.value, PrinterStuck)


def test_a_job_cups_completed_is_printed(monkeypatch, tmp_path):
    from agent.printing import print_task
    from agent.waiting import JobEnd, JobEnding

    asked = _cups_print(monkeypatch, JobEnding(JobEnd.COMPLETED))

    print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert asked == ["Shop-42"]


def test_a_job_cups_cannot_account_for_is_left_as_printed(monkeypatch, tmp_path):
    """No answer is not evidence of failure either: a CUPS that keeps no job
    history would otherwise fail every print in the shop and refund paper
    that came out."""
    from agent.printing import print_task
    from agent.waiting import JobEnd, JobEnding

    _cups_print(monkeypatch, JobEnding(JobEnd.UNKNOWN))

    print_task(_windows_job(), file_path=tmp_path / "a.pdf", printer="Shop")


# ── waiting for the paper, not the queue ────────────────────────────────────
#
# CUPS calls a USB job finished when it has *sent* it. An inkjet buffers
# pages, so the next job used to arrive while this one was still coming out --
# and the paper that should have followed never did, on a job already reported
# printed.


def _task(*, sheets=10, duplex=False, colour=False):
    from agent.printing import Task

    return Task.from_response(
        {
            "task_id": "tsk_p",
            "document_id": "doc_p",
            "filename": "tsk_p.pdf",
            "page_count": sheets,
            "copies": 1,
            "duplex": duplex,
            "colour": colour,
            "page_range": None,
            "expected_sheets": sheets,
        }
    )


def test_a_longer_job_takes_longer_to_come_out():
    from agent.printing import time_to_print

    assert time_to_print(_task(sheets=20)) > time_to_print(_task(sheets=2))


def test_both_sides_and_colour_take_longer():
    from agent.printing import time_to_print

    plain = time_to_print(_task())
    assert time_to_print(_task(duplex=True)) > plain
    assert time_to_print(_task(colour=True)) > plain


def _clock(start=0.0):
    state = {"now": start, "slept": 0.0, "ticks": 0}

    def now():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds
        state["slept"] += seconds

    def tick():
        state["ticks"] += 1

    return state, now, sleep, tick


def test_the_wait_lasts_until_the_paper_should_be_out():
    from agent.printing import time_to_print, wait_for_paper

    state, now, sleep, tick = _clock()
    task = _task(sheets=10)

    wait_for_paper(task, since=0.0, now=now, sleep=sleep, on_tick=tick)

    assert state["now"] >= time_to_print(task)


def test_time_already_spent_printing_counts_towards_it():
    """The queue took a while to empty; that was paper coming out too."""
    from agent.printing import time_to_print, wait_for_paper

    task = _task(sheets=10)
    state, now, sleep, tick = _clock(start=time_to_print(task) + 1)

    wait_for_paper(task, since=0.0, now=now, sleep=sleep, on_tick=tick)

    assert state["slept"] == 0


def test_a_long_wait_keeps_the_heartbeat_going():
    """The server's lease on the job is fifteen minutes. A silent wait longer
    than that fails a job whose paper is still coming out."""
    from agent.printing import wait_for_paper

    state, now, sleep, tick = _clock()

    wait_for_paper(_task(sheets=200, duplex=True, colour=True), since=0.0,
                   now=now, sleep=sleep, on_tick=tick)

    assert state["ticks"] >= state["slept"] // 60


def test_the_next_job_waits_for_this_ones_paper(monkeypatch, tmp_path):
    from agent import printing
    from agent.printing import print_task
    from agent.waiting import JobEnd, JobEnding

    _cups_print(monkeypatch, JobEnding(JobEnd.COMPLETED))
    waited: list = []
    monkeypatch.setattr(
        printing, "wait_for_paper", lambda task, **kw: waited.append(task.task_id)
    )

    print_task(_task(), file_path=tmp_path / "a.pdf", printer="Shop")

    assert waited == ["tsk_p"]
