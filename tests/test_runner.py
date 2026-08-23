"""The claim loop.

The bug this replaces: the Windows agent fetched **one** task per pass and then
waited, so a student who sent four files at once got the first one printed and
the rest whenever something else happened to wake the loop. The Pi agent did not
have it, which is the whole argument for there being one agent rather than two.

The rule here: **claim until the server says there is nothing left.** A wake is
a hint that work exists, never a count of it.
"""

from pathlib import Path

from agent.printing import Task
from agent.runner import run_once
from agent.waiting import JobState


class FakeBackend:
    """A backend that hands out tasks and remembers what it was told."""

    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.queue = list(tasks or [])
        self.claims = 0
        self.reports: list[tuple[str, str, int | None]] = []
        self.downloaded: list[str] = []

    def next_task(self) -> dict | None:
        self.claims += 1
        return self.queue.pop(0) if self.queue else None

    def download(self, task: Task, into: Path) -> Path:
        self.downloaded.append(task.task_id)
        path = into / task.filename
        path.write_bytes(b"%PDF-1.4 pretend")
        return path

    def report(self, task_id: str, state: str, *, sheets_printed: int | None = None) -> None:
        self.reports.append((task_id, state, sheets_printed))


def a_task(task_id: str = "tsk_1", **kwargs) -> dict:
    return {
        "task_id": task_id,
        "document_id": "doc_1",
        "filename": f"{task_id}.pdf",
        "page_count": 4,
        "copies": 1,
        "duplex": True,
        "colour": False,
        "page_range": None,
        "expected_sheets": 2,
        **kwargs,
    }


def printer_that_works(printed: list[Task]):
    def _print(task: Task, *, file_path: Path, printer: str, on_state=None, **_) -> None:
        # A printer with nothing queued ahead of it.
        if on_state:
            on_state(JobState.PRINTING)
        printed.append(task)

    return _print


def printer_that_fails(task: Task, *, file_path: Path, printer: str, **_) -> None:
    raise RuntimeError("out of toner")


# ── claiming ────────────────────────────────────────────────────────────────


def test_every_queued_job_is_printed_in_one_pass(tmp_path):
    """Four files sent together are four tasks, and all four come out.

    The defect this file exists for: one task per pass left the other three
    waiting on whatever woke the loop next.
    """
    printed: list[Task] = []
    backend = FakeBackend([a_task(f"tsk_{n}") for n in range(4)])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works(printed))

    assert [task.task_id for task in printed] == ["tsk_0", "tsk_1", "tsk_2", "tsk_3"]


def test_it_asks_once_more_than_it_receives(tmp_path):
    """The empty answer is how the agent knows to stop. Anything that stops
    earlier is guessing."""
    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works([]))

    assert backend.claims == 2


def test_an_empty_queue_prints_nothing_and_reports_nothing(tmp_path):
    printed: list[Task] = []
    backend = FakeBackend([])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works(printed))

    assert printed == []
    assert backend.reports == []


# ── reporting ───────────────────────────────────────────────────────────────


def test_a_printed_job_is_reported_with_the_sheets_it_used(tmp_path):
    """Paper is deducted from what the agent reports, so this number is the
    tray count. The server's expectation is used when the printer cannot say --
    never a second calculation done here."""
    backend = FakeBackend([a_task("tsk_1", expected_sheets=2)])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works([]))

    # The last word, after the "printing" the test below asks for.
    assert backend.reports[-1] == ("tsk_1", "printed", 2)


def test_a_job_is_reported_as_started_before_it_is_printed(tmp_path):
    """So a shop watching the queue sees something happening, and so a task
    that never comes back is visibly stuck rather than silently missing."""
    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works([]))

    assert [state for _, state, _ in backend.reports] == ["printing", "printed"]


def test_a_failed_job_is_reported_failed(tmp_path):
    """Not swallowed. A failure the server never hears about is a student
    charged for a job that will never come out and nobody knowing."""
    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_fails)

    assert ("tsk_1", "failed", None) in backend.reports


def test_one_failure_does_not_stop_the_rest(tmp_path):
    """Three students are waiting behind the one whose file jammed."""
    printed: list[Task] = []
    attempts = {"n": 0}

    def flaky(task: Task, *, file_path: Path, printer: str, **_) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("out of toner")
        printed.append(task)

    backend = FakeBackend([a_task(f"tsk_{n}") for n in range(3)])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=flaky)

    assert [task.task_id for task in printed] == ["tsk_1", "tsk_2"]


def test_a_download_that_fails_is_reported_rather_than_printed(tmp_path):
    """There is nothing to print, and the task must not be left claimed and
    silent -- its lease would expire and it would be handed out again."""

    class Broken(FakeBackend):
        def download(self, task, into):
            raise RuntimeError("connection reset")

    backend = Broken([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works([]))

    assert ("tsk_1", "failed", None) in backend.reports


def test_the_file_is_deleted_after_printing(tmp_path):
    """A kiosk holds other people's documents. It keeps them for exactly as
    long as it needs them."""
    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_works([]))

    assert list(tmp_path.iterdir()) == []


def test_the_file_is_deleted_even_when_printing_fails(tmp_path):
    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_fails)

    assert list(tmp_path.iterdir()) == []


def test_a_runaway_queue_stops_at_the_limit(tmp_path):
    """A server that never says "nothing left" -- a bug, or a queue being fed
    faster than it drains -- must not spin here for ever. The loop yields and
    the next pass picks up where it left off."""
    printed: list[Task] = []
    backend = FakeBackend([a_task(f"tsk_{n}") for n in range(500)])

    run_once(
        backend,
        printer="HP-01",
        workspace=tmp_path,
        printer_fn=printer_that_works(printed),
        max_per_pass=10,
    )

    assert len(printed) == 10


# ── what the student sees, and when ─────────────────────────────────────────


def test_printing_is_reported_when_the_printer_starts_not_when_we_send(tmp_path):
    """The state the whole chain exists for.

    A job sitting behind somebody else's two hundred pages is **queued**. The
    agent used to say "printing" the moment it handed the file over, which sent
    students walking to the shop to collect nothing.
    """
    from agent.waiting import JobState

    def printer_with_a_queue(task, *, file_path, printer, on_state=None, **_):
        # As a real queue would: waiting, then on the printer, then gone.
        on_state(JobState.QUEUED)
        on_state(JobState.PRINTING)
        on_state(JobState.GONE)

    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_with_a_queue)

    assert [state for _, state, _ in backend.reports] == ["printing", "printed"]


def test_a_job_that_waits_its_turn_says_nothing_until_it_starts(tmp_path):
    """Queued is where the task already is as far as the server is concerned --
    it was claimed, and nothing has happened since. Reporting it again would be
    noise on every job at a busy shop."""
    from agent.waiting import JobState

    def printer_that_waits(task, *, file_path, printer, on_state=None, **_):
        on_state(JobState.QUEUED)
        raise RuntimeError("printer switched off")

    backend = FakeBackend([a_task("tsk_1")])

    run_once(backend, printer="HP-01", workspace=tmp_path, printer_fn=printer_that_waits)

    assert [state for _, state, _ in backend.reports] == ["failed"]
