"""Talking to the backend.

Eight calls, and one credential. `X-Device-Token` **is** the kiosk: nothing the
agent sends says which kiosk it belongs to, because the token decides. The old
`/pi/*` routes checked that a token was valid and then trusted the printer id in
the URL, so one shop's machine could fetch another shop's file.

Claiming is a POST that returns one task or nothing. That is deliberate on the
server's side -- a single `FOR UPDATE SKIP LOCKED` statement -- so two devices
racing cannot be handed the same job. The agent's part of the bargain is to keep
asking until the answer is nothing.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("agent")

# Long enough for a big PDF on a bad connection, short enough that a dead
# server does not hold the loop for ever.
TIMEOUT = httpx.Timeout(30.0, read=120.0)

# A download on a bad link: how long to keep resuming before giving up, and how
# long a connection may go silent before it is dropped and resumed. Well inside
# the server's fifteen-minute lease, so the agent reports the failure itself
# rather than leaving the server to guess.
DOWNLOAD_PATIENCE_SECONDS = 10 * 60
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=30.0)
DOWNLOAD_BACKOFF_MAX_SECONDS = 15.0


# How this agent asks for only a job's pages, and how the server says it sent
# them. The server's side is `app/api/device/tasks.py`; a test holds the two
# spellings together.
SELECTED_PAGES = "selected"
PAGES_HEADER = "X-Printvendo-Pages"


class DownloadIncomplete(Exception):
    """Every retry ran out before the whole file arrived."""


class _DifferentFile(Exception):
    """The answer to a resumed request is not the file we have half of."""


@dataclass(frozen=True)
class Fetched:
    """A file on disk, and whether it is already only the job's pages.

    `pages_selected` is true only when the server said so. The page range must
    be applied exactly once: a file the server cut is printed as it is, and any
    other has the range applied at the printer, as always.
    """

    path: Path
    pages_selected: bool = False


class Backend:
    """The device API, as this agent uses it."""

    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client or httpx.Client(timeout=TIMEOUT)

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Device-Token": self.token}

    def heartbeat(
        self,
        *,
        agent_version: str,
        ssh_host: str | None = None,
        task_id: str | None = None,
        printers: list[dict] | None = None,
    ) -> dict:
        """Say the machine is alive, and hear back what this shop looks like.

        Whether a device is *online* is derived from this on the server, never
        from a status column: a Pi whose power was pulled cannot send "I am
        going offline", which is why the old backend's status stayed ONLINE
        until somebody noticed.

        **The answer used to be discarded.** It carries the shop's name, its
        paper count and capacity, and how much is queued -- everything the
        screen above the counter shows -- so throwing it away meant the display
        would have needed a credential and a second route to learn what this
        call already returns every sixty seconds.
        """
        body: dict = {"agent_version": agent_version}
        # Only when there is one. A kiosk with no tailnet has no answer, and
        # sending null would overwrite the last good name the server had.
        if ssh_host:
            body["ssh_host"] = ssh_host
        # The job in hand, so the server renews its lease rather than deciding
        # it is lost while it waits behind an empty tray. Absent when idle.
        if task_id:
            body["task_id"] = task_id
        # The machines this box has, so the server can keep a tray for each.
        # Omitted when there is nothing to say, which leaves the server's
        # trays exactly as they are.
        if printers:
            body["printers"] = printers

        response = self._client.post(
            f"{self.base_url}/v1/device/heartbeat",
            headers=self._headers,
            json=body,
        )
        response.raise_for_status()
        # An older server, or one that answers 204, leaves nothing to read.
        # Returning {} rather than raising keeps a heartbeat a heartbeat.
        try:
            return response.json() or {}
        except ValueError:
            return {}

    def next_task(self) -> dict | None:
        """Claim one task, or find out there is none."""
        response = self._client.post(
            f"{self.base_url}/v1/device/tasks/next", headers=self._headers, json={}
        )
        response.raise_for_status()
        body = response.json()
        return body or None

    def download(
        self,
        task,
        into: Path,
        *,
        on_tick: Callable[[], None] | None = None,
        patience: float = DOWNLOAD_PATIENCE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Fetched:
        """Fetch the file for a task we hold, surviving a connection that drops.

        **Only the pages, when the job names some.** Two pages of a
        hundred-page scan used to mean fetching the hundred over a shop's
        connection and handing them all to the print system, with the order at
        "paid" meanwhile. The server is asked for the selected pages and says
        in a header when that is what it sent; an older server ignores the
        question, says nothing, and the range is applied at the printer as
        before.

        **Resumed, never restarted.** A shop on unstable internet lost the
        connection partway through and the job sat at "paid" for as long as
        that went on -- or failed on the first drop. The bytes already on disk
        are kept and the rest is asked for with `Range: bytes=N-`; the server
        answers 206 with only what is missing. A server that ignores the range
        answers 200 with the whole file, which replaces what we had rather than
        being appended to it.

        **The printer sees it only when every byte is here.** Until then it is
        `<task>.pdf.part`, renamed only once its size matches what the server
        said the file is. A half file printed is half a document a student paid
        for.

        A connection silent for thirty seconds is dropped and resumed rather
        than trickled on for ever. `on_tick` is the heartbeat -- it only runs
        when called, and a download that takes minutes must not make the shop
        look offline or let the job's lease run out. A refusal (409: no longer
        ours; 404: the file is gone) is raised at once; dropped connections and
        5xx are retried with backoff until `patience` runs out.

        Streamed to disk rather than read into memory: a sixty-megabyte upload
        on a Pi with half a gigabyte of RAM is the difference between printing
        and being killed by the kernel.

        **Named after the task, never after the student's file.** `lp` submits
        the job under the filename on disk, and CUPS keeps job history long
        after it has removed the document -- a shop's completed-jobs list would
        otherwise carry a student's filename for as long as the Pi runs.
        """
        destination = into / f"{task.task_id}.pdf"
        partial = into / f"{task.task_id}.pdf.part"
        partial.unlink(missing_ok=True)
        url = f"{self.base_url}/v1/device/tasks/{task.task_id}/file"
        if getattr(task, "page_range", None):
            url += f"?pages={SELECTED_PAGES}"
        deadline = time.monotonic() + patience
        attempt = 0
        # Whether the bytes on disk are the page cut: empty until an answer,
        # then one value. A list so it is recorded the moment the headers
        # arrive -- a connection that dies mid-body must not lose it.
        kind: list[bool] = []

        while True:
            have = partial.stat().st_size if partial.exists() else 0
            headers = dict(self._headers)
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                total = self._fetch_into(url, headers, partial, have, on_tick, kind)
                size = partial.stat().st_size if partial.exists() else 0
                if total is None or size >= total:
                    partial.replace(destination)
                    return Fetched(destination, pages_selected=kind == [True])
                # The connection closed cleanly but short. Resume like a drop.
                problem = f"closed at {size} of {total} bytes"
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    partial.unlink(missing_ok=True)
                    raise
                problem = f"server said {exc.response.status_code}"
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
            except _DifferentFile:
                # A cut and a whole document are different files. What is on
                # disk is half of the other one, so it goes and this starts
                # again rather than joining the two.
                partial.unlink(missing_ok=True)
                kind.clear()
                problem = "the server changed which file it was sending"

            attempt += 1
            if time.monotonic() >= deadline:
                partial.unlink(missing_ok=True)
                raise DownloadIncomplete(
                    f"{task.task_id}: gave up after {attempt} tries ({problem})"
                )
            got = partial.stat().st_size if partial.exists() else 0
            log.warning(
                "download of %s interrupted at %s bytes (%s); resuming",
                task.task_id,
                got,
                problem,
            )
            _tick(on_tick)
            sleep(min(2.0 ** min(attempt, 4), DOWNLOAD_BACKOFF_MAX_SECONDS))

    def _fetch_into(
        self,
        url: str,
        headers: dict[str, str],
        partial: Path,
        have: int,
        on_tick: Callable[[], None] | None,
        kind: list[bool],
    ) -> int | None:
        """One request. Writes to `partial` and returns the file's full size,
        or None when the server did not say. Records in `kind` whether what is
        being written is the page cut."""
        with self._client.stream(
            "GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT
        ) as response:
            if have and response.status_code == 416:
                # We already have everything the server has.
                return have
            response.raise_for_status()
            resumed = response.status_code == 206
            answer = response.headers.get(PAGES_HEADER, "").lower() == SELECTED_PAGES
            if resumed and kind and kind != [answer]:
                raise _DifferentFile
            kind[:] = [answer]
            total = _full_size(response, offset=have if resumed else 0)
            since_tick = 0
            with partial.open("ab" if resumed else "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
                    since_tick += len(chunk)
                    if since_tick >= 1 << 20:
                        since_tick = 0
                        _tick(on_tick)
            return total

    def report(self, task_id: str, state: str, *, sheets_used: int | None = None) -> None:
        """Say what happened. Paper is deducted from this.

        The field is `sheets_used` because that is what the server reads. It
        was sent as `sheets_printed` and the server ignored it -- FastAPI drops
        unknown fields rather than refusing them, so every report succeeded,
        every job printed, and the tray count never moved. A shop would have
        run out of paper with the system believing it was full, and the paper
        watcher that exists to warn about exactly that would never have fired.

        Nothing caught it because every test replaces this class with a fake
        that takes the same wrong keyword, so the JSON body was the one thing
        never exercised. `test_the_wire_format` is that test.
        """
        payload: dict = {"state": state}
        if sheets_used is not None:
            payload["sheets_used"] = sheets_used

        response = self._client.post(
            f"{self.base_url}/v1/device/tasks/{task_id}/status",
            headers=self._headers,
            json=payload,
        )
        response.raise_for_status()


    def next_commands(self) -> list[dict]:
        """Everything an operator has asked this machine to do.

        A list, unlike a print task: restarting the agent kills the loop that
        would have come back for whatever was queued behind it, so taking one
        per pass would silently drop the second half of "restart cups and then
        restart the agent".
        """
        response = self._client.post(
            f"{self.base_url}/v1/device/commands/next", headers=self._headers, json={}
        )
        response.raise_for_status()
        return response.json() or []

    def report_command(
        self, command_id: str, *, succeeded: bool, error_message: str | None = None
    ) -> None:
        """How a command went. The message is shown to an operator verbatim."""
        response = self._client.post(
            f"{self.base_url}/v1/device/commands/{command_id}/result",
            headers=self._headers,
            json={"succeeded": succeeded, "error_message": error_message},
        )
        response.raise_for_status()

    def report_printer_paper(self, *, printer: str, out_of_paper: bool) -> None:
        """Say that one printer has run out of paper.

        Named, because a shop may have two machines and one of them running dry
        is not the shop running dry: the server empties that tray, stops routing
        work to it, and tells whoever refills it which machine to walk to.

        Refilling is deliberately not reported from here. A printer can tell
        that paper ran out; how much somebody then put in is a person's word.
        """
        response = self._client.post(
            f"{self.base_url}/v1/device/printer-paper",
            headers=self._headers,
            json={"printer": printer, "out_of_paper": out_of_paper},
        )
        response.raise_for_status()

    def report_printer_health(self, *, stuck: bool, detail: str | None = None) -> None:
        """Whether this machine can currently get a job out of the printer.

        Stuck closes the shop to students — the kiosk stops selling, so nobody
        pays for a print that was never going to come out — while every
        operator surface still shows it and says why. Saying it is working
        again reopens it, but only a shop this mechanism closed.
        """
        response = self._client.post(
            f"{self.base_url}/v1/device/printer-health",
            headers=self._headers,
            json={"stuck": stuck, "detail": detail},
        )
        response.raise_for_status()


def enrol(base_url: str, *, code: str, agent_version: str, device_key: str | None = None) -> dict:
    """Spend a one-time enrolment code and get this machine's token.

    The code is what an installer types; the token is what the machine keeps.
    A code that has been read aloud or left in a terminal's scrollback is
    worthless a moment later, and the token exists only in the config file and
    as a hash on the server.

    Re-enrolling **replaces**: a kiosk has at most one device, so swapping a
    machine kills the old one's access in the same breath. That machine may
    have been sold, returned or stolen.
    """
    payload = {"enrolment_code": code, "agent_version": agent_version}
    if device_key:
        payload["device_key"] = device_key

    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(f"{base_url.rstrip('/')}/v1/device/register", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(_message(response))
        return response.json()


def _message(response: httpx.Response) -> str:
    """The server's sentence, which is written for a person to read."""
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001
        detail = None
    if isinstance(detail, str):
        return detail
    return f"the server refused that ({response.status_code})"


def _full_size(response: httpx.Response, *, offset: int) -> int | None:
    """How big the whole file is: from `Content-Range` on a 206, otherwise
    what this response carries plus what we already had."""
    ranged = response.headers.get("content-range", "")
    if "/" in ranged:
        tail = ranged.rsplit("/", 1)[1]
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("content-length")
    return offset + int(length) if length and length.isdigit() else None


def _tick(on_tick: Callable[[], None] | None) -> None:
    """The heartbeat. A failure here must not stop a download."""
    if on_tick is None:
        return
    try:
        on_tick()
    except Exception:  # noqa: BLE001
        log.warning("heartbeat during download failed", exc_info=True)
