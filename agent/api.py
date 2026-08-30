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
from pathlib import Path

import httpx

log = logging.getLogger("agent")

# Long enough for a big PDF on a bad connection, short enough that a dead
# server does not hold the loop for ever.
TIMEOUT = httpx.Timeout(30.0, read=120.0)


class Backend:
    """The device API, as this agent uses it."""

    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client or httpx.Client(timeout=TIMEOUT)

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Device-Token": self.token}

    def heartbeat(self, *, agent_version: str) -> None:
        """Say the machine is alive.

        Whether a device is *online* is derived from this on the server, never
        from a status column: a Pi whose power was pulled cannot send "I am
        going offline", which is why the old backend's status stayed ONLINE
        until somebody noticed.
        """
        response = self._client.post(
            f"{self.base_url}/v1/device/heartbeat",
            headers=self._headers,
            json={"agent_version": agent_version},
        )
        response.raise_for_status()

    def next_task(self) -> dict | None:
        """Claim one task, or find out there is none."""
        response = self._client.post(
            f"{self.base_url}/v1/device/tasks/next", headers=self._headers, json={}
        )
        response.raise_for_status()
        body = response.json()
        return body or None

    def download(self, task, into: Path) -> Path:
        """Fetch the file for a task we hold.

        Streamed to disk rather than read into memory: a sixty-megabyte upload
        on a Pi with half a gigabyte of RAM is the difference between printing
        and being killed by the kernel.

        **Named after the task, never after the student's file.** The bytes are
        deleted the moment the job ends, but the *name* outlives them: `lp`
        submits the job under the filename on disk, and CUPS keeps job history
        long after it has removed the document itself. A shop's completed-jobs
        list -- `lpstat -W completed`, or the CUPS web interface anyone on that
        machine can open -- would otherwise read "Medical Results Ravi
        Kumar.pdf" for as long as the Pi runs. The server already anonymises its
        own Content-Disposition for the same reason; this was the one place the
        original name still reached a disk in a shop.
        """
        destination = into / f"{task.task_id}.pdf"
        with self._client.stream(
            "GET", f"{self.base_url}/v1/device/tasks/{task.task_id}/file",
            headers=self._headers,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return destination

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
