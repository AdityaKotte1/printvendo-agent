"""Turning a print task into a command, and running it.

**One mapping per platform, both pure functions, both tested by the same
tests.** That shape is the whole point of this module. The agent it replaces
had a correct `lp` command on Linux and, on Windows, a DEVMODE it filled in and
never applied followed by `cmd /c start /print` — the shell's print verb, which
uses the default printer, takes no options, and has no notion of a page range.
Colour, duplex, copies and range were all silently dropped, on jobs a student
had already paid for.

Ghostscript does the printing on Windows. It is already a dependency of this
system — the backend normalises every uploaded PDF with it — it drives a named
printer through `mswinpr2` without a dialog, and it accepts all four options.
The alternative was writing raw spool data, which means rendering the PDF
ourselves.

**Nothing here reads a JSON options blob.** The backend sends the options as
fields on the task, already resolved: how many sheets this will use is decided
by one calculation server-side, and the agent's job is to obey rather than to
work anything out. The old agent parsed an `options` string and re-derived
things, which is how a price, a paper count and a printed job come to disagree.
"""

import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent.waiting import (
    JobState,
    cups_job_id,
    cups_watcher,
    wait_until_idle,
    watch_job,
    windows_job_ids,
    windows_watcher,
)

IS_WINDOWS = platform.system() == "Windows"

# Where Ghostscript lives, by the names it uses. The backend has the same
# fallback list for the same reason: one config file has to work on a
# developer's laptop and on the machine at a shop.
GHOSTSCRIPT_NAMES = ("gswin64c", "gswin32c", "gs")


@dataclass(frozen=True)
class Task:
    """One print task, exactly as the backend describes it.

    Every field is already decided. `expected_sheets` in particular is the
    server's number, and the agent reports against it rather than recomputing
    it -- two opinions about how much paper a job uses is how a tray count
    drifts from the physical tray.
    """

    task_id: str
    document_id: str
    filename: str
    page_count: int | None
    copies: int
    duplex: bool
    colour: bool
    page_range: str | None
    expected_sheets: int

    @classmethod
    def from_response(cls, body: dict) -> "Task":
        return cls(
            task_id=body["task_id"],
            document_id=body["document_id"],
            filename=body["filename"],
            page_count=body.get("page_count"),
            copies=int(body.get("copies", 1)),
            duplex=bool(body.get("duplex", False)),
            colour=bool(body.get("colour", False)),
            page_range=body.get("page_range"),
            expected_sheets=int(body.get("expected_sheets", 0)),
        )


def pages_in(page_range: str | None) -> list[int]:
    """"1,4-6" -> [1, 4, 5, 6]. Sorted, de-duplicated, empty when absent.

    An empty result means "everything", never "nothing" -- a range that fails
    to parse must not silently print a blank job somebody has paid for.
    """
    if not page_range or not page_range.strip():
        return []

    pages: set[int] = set()
    for token in page_range.split(","):
        token = "".join(token.split())
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            if start.isdigit() and end.isdigit():
                pages.update(range(int(start), int(end) + 1))
        elif token.isdigit():
            pages.add(int(token))
    return sorted(pages)


def as_cups_range(page_range: str | None) -> str:
    """The same pages, written the way CUPS wants them: ascending, compact.

    CUPS refuses "12-17,1" outright, so the input is parsed and rewritten
    rather than passed through.
    """
    pages = pages_in(page_range)
    if not pages:
        return ""

    parts: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def build_cups_command(task: Task, *, file_path: str, printer: str) -> list[str]:
    """`lp`, with every option stated explicitly."""
    cmd = ["lp", "-d", printer, "-n", str(task.copies)]

    # Stated even when false. A shop that has left duplex on in the driver
    # would otherwise halve the paper of every single-sided job while the
    # student is charged for -- and the tray is counted for -- single sides.
    cmd += ["-o", f"sides={'two-sided-long-edge' if task.duplex else 'one-sided'}"]

    if task.colour:
        cmd += ["-o", "print-color-mode=color", "-o", "Ink=COLOR"]
    else:
        # Both, and this was learned on real hardware: HP's hpcups driver
        # prints in colour anyway with `print-color-mode` alone. `Ink=MONO` is
        # what actually stops the colour cartridges. Drivers that do not know
        # the option ignore it.
        cmd += ["-o", "print-color-mode=monochrome", "-o", "Ink=MONO"]

    pages = as_cups_range(task.page_range)
    if pages:
        cmd += ["-o", f"page-ranges={pages}"]

    cmd.append(file_path)
    return cmd


def build_windows_command(
    task: Task, *, file_path: str, printer: str, ghostscript: str = "gswin64c"
) -> list[str]:
    """Ghostscript, printing to a named Windows printer with no dialog.

    Takes the executable rather than looking it up, so building a command is a
    pure function that can be tested on any machine -- including the Linux one
    that runs CI, which has no `gswin64c` to find. `print_task` resolves the
    real path and passes it in.
    """
    cmd = [
        ghostscript,
        "-dNOPAUSE",
        "-dBATCH",
        "-dNoCancel",
        # A PDF is somebody else's file and Ghostscript will run what is inside
        # it given the chance. The backend renders under -dSAFER for the same
        # reason; the agent is the machine sitting in a shop, so it matters
        # more here, not less.
        "-dSAFER",
        "-sDEVICE=mswinpr2",
        f"-sOutputFile=%printer%{printer}",
        "-dPrinted",
    ]

    # Copies, twice over, and deliberately: which of the two a Windows driver
    # honours varies by model, exactly as `print-color-mode` alone fails to
    # force greyscale on HP's CUPS driver. Setting both costs nothing; setting
    # the wrong one prints one copy of a job somebody paid three for.
    cmd += [f"-dNumCopies={task.copies}"]
    cmd += ["-c", f"<< /NumCopies {task.copies} >> setpagedevice"]

    # Duplex is a page-device setting rather than a switch, and it is stated in
    # both directions for the same reason as on CUPS.
    if task.duplex:
        cmd += ["-c", "<< /Duplex true /Tumble false >> setpagedevice"]
    else:
        cmd += ["-c", "<< /Duplex false >> setpagedevice"]

    # Advisory, and honestly so: some drivers ignore it exactly as some CUPS
    # drivers ignore print-color-mode. Forcing true greyscale on a particular
    # model may need a driver-specific setting, and that has to be checked
    # against the machine rather than assumed here.
    cmd += ["-c", f"<< /BitsPerPixel {24 if task.colour else 1} >> setpagedevice"]

    pages = pages_in(task.page_range)
    if pages:
        # An explicit list: -sPageList does not take CUPS's range syntax.
        cmd += ["-sPageList=" + ",".join(str(page) for page in pages)]

    # -f ends the -c postscript and says "the rest is input", so a filename
    # that begins with a dash cannot be read as a switch.
    cmd += ["-f", file_path]
    return cmd


def ghostscript_path(configured: str | None = None) -> str:
    """Where Ghostscript is, by whichever name this machine uses."""
    from shutil import which

    if configured and (found := which(configured)):
        return found
    for name in GHOSTSCRIPT_NAMES:
        if found := which(name):
            return found
    # Named rather than guessed: a missing Ghostscript is an install problem
    # with a one-line fix, and it must not present as a printing failure.
    raise RuntimeError(
        "Ghostscript is not installed, or not on PATH. The agent prints with "
        "it. Install it and try again."
    )


def build_command(
    task: Task, *, file_path: str, printer: str, ghostscript: str | None = None
) -> list[str]:
    """The command for this machine."""
    if IS_WINDOWS:
        return build_windows_command(
            task,
            file_path=file_path,
            printer=printer,
            ghostscript=ghostscript_path(ghostscript),
        )
    return build_cups_command(task, file_path=file_path, printer=printer)


class PrinterStuck(RuntimeError):
    """The printer took the job and did not finish it.

    Distinct from an ordinary failure because the *shop* is what is broken, not
    the file. A PDF Ghostscript refuses fails one student and the next job
    prints fine; a jammed tray fails everybody until somebody walks over to it,
    and the kiosk should stop selling rather than take money for prints nobody
    will collect.

    A subclass of RuntimeError so every existing handler still catches it: the
    task is still reported FAILED, which is still what puts a refund within
    reach. What is added is the second sentence to the server, not a different
    outcome for the student.
    """


def print_task(
    task: Task,
    *,
    file_path: Path,
    printer: str,
    ghostscript: str | None = None,
    timeout: int = 600,
    on_tick: Callable[[], None] | None = None,
    on_state: Callable[[JobState], None] | None = None,
) -> None:
    """Send it to the printer and wait for the queue to let go of it.

    **Returning is what makes the student's screen say "printed"**, so it must
    not happen a moment early. `lp` returns when the job is queued and
    Ghostscript returns when the spooler has the data; neither means paper. So
    the command is only the first half, and the second half follows the job
    through the queue.

    `on_state` is called as the job moves -- queued, then printing -- so the
    student sees where it actually is rather than a guess made when it was
    sent. A job behind somebody else's two hundred pages is queued, and saying
    "printing" would send them walking to the shop.

    A non-zero exit, a printer that has stopped, or a job that never leaves the
    queue all raise: the caller reports the task FAILED, which is what puts a
    refund within reach. Swallowing any of them would leave a student holding
    nothing and a screen saying it printed.
    """
    cmd = build_command(
        task, file_path=str(file_path), printer=printer, ghostscript=ghostscript
    )

    # Taken before printing: what is in the queue now is somebody else's, and
    # what appears next is ours. Ghostscript does not tell us the spool job it
    # made, and the spooler names every one of them "Ghostscript output".
    before = windows_job_ids(printer) if IS_WINDOWS else set()

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"printing failed ({result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:300]}"
        )

    if IS_WINDOWS:
        # Whatever appeared after we printed is ours.
        watch = windows_watcher(printer, windows_job_ids(printer) - before)
    else:
        # `lp` names the job it made. Without that there is nothing to follow,
        # and `cups_job_id` raises rather than letting the agent guess.
        watch = cups_watcher(cups_job_id(result.stdout), printer)

    outcome = watch_job(watch, on_state=on_state, on_tick=on_tick)

    if outcome is JobState.ERROR:
        raise PrinterStuck(
            "the printer stopped on this job -- it may be out of paper, "
            "jammed, or switched off"
        )
    if outcome is not JobState.GONE:
        raise PrinterStuck(
            "the printer still has this job after a long wait -- it may be out "
            "of paper, jammed, or switched off"
        )

    # The queue letting go means CUPS finished *sending*, not that paper has
    # stopped. A fifteen-page job streams into the printer's buffer in seconds
    # and leaves `lpstat -o` while page five is coming out -- so without this
    # the next student's job was submitted on top of it, and two people's pages
    # arrived in one pile with nothing to separate them. Students were taking
    # each other's sheets off the top.
    #
    # Only about spacing, never about the outcome: this job is already out of
    # the queue and has already been judged. A timeout here is a slower shop,
    # not a wrong report, which is why it returns rather than raising.
    if not IS_WINDOWS:
        wait_until_idle(printer)
