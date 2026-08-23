"""The loop: claim, print, report, repeat.

**Claim until the server says there is nothing left.** The agent this replaces
took one task per pass on Windows, so four files sent together printed one and
left three waiting on whatever woke the loop next. A wake is a hint that work
exists; it is never a count of it.

Everything else here is about a job never going quiet. A task is reported
PRINTING before the printer is touched and PRINTED or FAILED after, and a
failure — printing, downloading, anything — is reported rather than swallowed.
A claimed task that is never reported simply stops: its lease expires, the
server hands it to a device again, and nobody learns why.
"""

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

from agent.printing import Task, print_task
from agent.waiting import JobState

log = logging.getLogger("agent")

# How many tasks one pass will take before yielding. A server that never
# answers "nothing left" -- a bug, or a queue being filled faster than it
# drains -- must not hold this loop for ever; the next pass continues where
# this one stopped.
MAX_PER_PASS = 25

PrinterFn = Callable[..., None]


def run_once(
    backend,
    *,
    printer: str,
    workspace: Path | None = None,
    printer_fn: PrinterFn | None = None,
    max_per_pass: int = MAX_PER_PASS,
    on_tick: Callable[[], None] | None = None,
) -> int:
    """Drain the queue for this device. Returns how many tasks were handled.

    `backend` and `printer_fn` are passed in rather than imported so this can
    be driven end to end in a test without a server or a printer -- which is
    what makes the claim-until-empty rule something that is checked rather than
    asserted in a comment.
    """
    printer_fn = printer_fn or print_task
    handled = 0

    while handled < max_per_pass:
        body = backend.next_task()
        if not body:
            return handled

        task = Task.from_response(body)
        handled += 1
        _do_one(
            backend,
            task,
            printer=printer,
            workspace=workspace,
            printer_fn=printer_fn,
            on_tick=on_tick,
        )

    log.info("stopping this pass at %s tasks; more are waiting", handled)
    return handled


def _do_one(
    backend,
    task: Task,
    *,
    printer: str,
    workspace: Path | None,
    printer_fn: PrinterFn,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """One task, from file to report. Never raises: the next student is waiting."""
    with _scratch(workspace) as folder:
        path: Path | None = None
        try:
            path = backend.download(task, folder)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            log.error("could not fetch %s: %s", task.task_id, exc)
            _report(backend, task.task_id, "failed")
            return

        def announce(state: JobState) -> None:
            """Say "printing" when the **printer** starts, not when we send.

            A job behind somebody else's two hundred pages is queued, and the
            server already knows that -- the task was claimed and nothing has
            happened since. Saying it again on every job would be noise at a
            busy shop; saying "printing" early sends students walking to the
            counter to collect nothing.
            """
            if state is JobState.PRINTING:
                _report(backend, task.task_id, "printing")

        try:
            # `on_tick` is the heartbeat, called while the printer works. A
            # two-hundred-page job takes minutes, and a machine that goes quiet
            # for minutes is one an operator is told has gone offline.
            printer_fn(
                task,
                file_path=path,
                printer=printer,
                on_tick=on_tick,
                on_state=announce,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("printing %s failed: %s", task.task_id, exc)
            _report(backend, task.task_id, "failed")
            return

        # The server's own figure. The agent does not recompute how much paper
        # a job used -- one calculation decides the price, the tray count and
        # what the printer is asked for, and a second opinion here is how a
        # counter drifts from the physical tray.
        _report(backend, task.task_id, "printed", sheets_printed=task.expected_sheets)


def _report(backend, task_id: str, state: str, *, sheets_printed: int | None = None) -> None:
    """Tell the server, and do not let saying so become the failure.

    A report that raises would leave a printed job looking failed, or -- worse,
    on the PRINTING report -- stop the agent before it printed something the
    student has paid for.
    """
    try:
        backend.report(task_id, state, sheets_printed=sheets_printed)
    except Exception as exc:  # noqa: BLE001
        log.error("could not report %s as %s: %s", task_id, state, exc)


class _scratch:
    """A folder for one job's file, emptied afterwards however the job went.

    A kiosk holds other people's documents. It keeps them for exactly as long
    as it needs them and not a moment longer -- the machine sits in a shop,
    and the next person to use it is not the person whose file this was.
    """

    def __init__(self, workspace: Path | None) -> None:
        self._workspace = workspace
        self._temporary: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> Path:
        if self._workspace is not None:
            self._workspace.mkdir(parents=True, exist_ok=True)
            return self._workspace
        self._temporary = tempfile.TemporaryDirectory(prefix="printvendo-")
        return Path(self._temporary.name)

    def __exit__(self, *exc_info) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            return
        if self._workspace is None:
            return
        for leftover in self._workspace.iterdir():
            try:
                leftover.unlink()
            except OSError:
                log.warning("could not remove %s", leftover)
