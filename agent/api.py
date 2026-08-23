"""Talking to the backend.

Five calls, and one credential. `X-Device-Token` **is** the kiosk: nothing the
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
        """
        destination = into / task.filename
        with self._client.stream(
            "GET", f"{self.base_url}/v1/device/tasks/{task.task_id}/file",
            headers=self._headers,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return destination

    def report(self, task_id: str, state: str, *, sheets_printed: int | None = None) -> None:
        """Say what happened. Paper is deducted from this."""
        payload: dict = {"state": state}
        if sheets_printed is not None:
            payload["sheets_printed"] = sheets_printed

        response = self._client.post(
            f"{self.base_url}/v1/device/tasks/{task_id}/status",
            headers=self._headers,
            json=payload,
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
