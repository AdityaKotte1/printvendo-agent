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
