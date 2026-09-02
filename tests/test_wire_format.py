"""The JSON this agent actually puts on the wire.

Every other test in this suite replaces `Backend` with a fake that takes the
same keywords the real one does. That is the right shape for testing the loop
-- but it means the request body was the one thing nothing exercised, and a
field name can be wrong in `api.py` while every test in the project passes.

One was. `report()` sent `sheets_printed`; the server reads `sheets_used`.
FastAPI drops unknown fields rather than refusing them, so every report
returned 200, every job printed, and the tray count never moved -- a shop would
have run out of paper with the system believing it was full, and the paper
watcher that exists to warn about exactly that would never have fired.

So these tests assert the bytes, against the names in the server's schemas
(`app/api/schemas.py`, `app/api/device/commands.py`). If one of these fails
after a backend change, the contract moved and this agent has not.
"""

import json

import httpx
import pytest

from agent.api import Backend

BASE = "https://api.example.test"
TOKEN = "dvt_secret"


@pytest.fixture
def sent():
    """Captures the one request made, and answers 200."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        return httpx.Response(200, json={})

    return seen, httpx.Client(transport=httpx.MockTransport(handler))


def _backend(client) -> Backend:
    return Backend(BASE, TOKEN, client=client)


# ── the field that was wrong ────────────────────────────────────────────────


def test_a_printed_report_names_the_field_the_server_reads(sent):
    """`sheets_used`. Paper is deducted from this and from nothing else."""
    seen, client = sent

    _backend(client).report("prt_abc", "printed", sheets_used=7)

    assert seen[0]["body"] == {"state": "printed", "sheets_used": 7}
    assert seen[0]["path"] == "/v1/device/tasks/prt_abc/status"


def test_a_report_without_a_sheet_count_omits_the_field(sent):
    """None means the agent could not tell, which is not the same as zero --
    so the field is left out rather than sent as null."""
    seen, client = sent

    _backend(client).report("prt_abc", "printing")

    assert seen[0]["body"] == {"state": "printing"}


def test_the_token_is_the_credential_on_every_call(sent):
    """`X-Device-Token` is the kiosk. Nothing in the body says which shop this
    is, because the token decides."""
    seen, client = sent

    _backend(client).report("prt_abc", "printed", sheets_used=1)

    assert seen[0]["headers"]["x-device-token"] == TOKEN


# ── the rest of the surface, so this cannot happen twice ────────────────────


def test_a_command_result_matches_CommandResultRequest(sent):
    seen, client = sent

    _backend(client).report_command("cmd_1", succeeded=False, error_message="no rights")

    assert seen[0]["body"] == {"succeeded": False, "error_message": "no rights"}
    assert seen[0]["path"] == "/v1/device/commands/cmd_1/result"


def test_a_printer_health_report_matches_PrinterHealthRequest(sent):
    seen, client = sent

    _backend(client).report_printer_health(stuck=True, detail="tray jammed")

    assert seen[0]["body"] == {"stuck": True, "detail": "tray jammed"}
    assert seen[0]["path"] == "/v1/device/printer-health"


def test_a_heartbeat_matches_DeviceHeartbeatRequest(sent):
    seen, client = sent

    _backend(client).heartbeat(agent_version="1.2.3")

    assert seen[0]["body"] == {"agent_version": "1.2.3"}


# ── a failure to report is a failure ────────────────────────────────────────


def test_a_refused_report_raises_rather_than_passing_quietly():
    """`runner._report` catches this on purpose -- a report that raised would
    leave a printed job looking failed. But it has to *be* raised to be caught
    and logged, or a rejected body is indistinguishable from a delivered one.
    """
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(422, json={}))
    )

    with pytest.raises(httpx.HTTPStatusError):
        _backend(client).report("prt_abc", "printed", sheets_used=3)


# ── a clock, not a cable ────────────────────────────────────────────────────


def test_a_not_yet_valid_certificate_is_recognised_as_a_clock():
    """A Pi has no battery-backed clock. One that reboots without network sits
    at the time it last knew and refuses every certificate as "not yet valid",
    which reads as an unreachable server -- so somebody goes looking at the
    wifi, or reinstalls, or rings somebody. It cost a real install.
    """
    from agent.__main__ import _looks_like_a_wrong_clock

    ssl_error = Exception(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "certificate is not yet valid (_ssl.c:1029)"
    )
    assert _looks_like_a_wrong_clock(ssl_error) is True
    assert _looks_like_a_wrong_clock(Exception("certificate has expired")) is True
    # A genuine network failure must not be blamed on the clock.
    assert _looks_like_a_wrong_clock(Exception("Connection refused")) is False


# ── a credential the server has replaced ────────────────────────────────────


def test_a_401_is_recognised_as_a_rejected_token():
    """Re-enrolling a kiosk rotates the token on its existing device row, so a
    process already running keeps polling with one the server has just
    invalidated. It cost two shops a morning: 401 on every heartbeat, every
    command poll and every claim, while the config file on disk held a
    perfectly good replacement and the installer's own `check` -- which reads
    that file -- reported the kiosk healthy.
    """
    from agent.__main__ import _token_was_rejected

    def _status(code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", f"{BASE}/v1/device/tasks/next")
        return httpx.HTTPStatusError(
            "refused", request=request, response=httpx.Response(code, request=request)
        )

    assert _token_was_rejected(_status(401)) is True
    # Everything else is a different problem and must not trigger a re-read.
    assert _token_was_rejected(_status(403)) is False
    assert _token_was_rejected(_status(500)) is False
    assert _token_was_rejected(httpx.ConnectError("no route")) is False


# ── a shop never learns what a student called their file ────────────────────


def test_a_download_is_named_after_the_task_not_the_student_file(tmp_path):
    """The bytes are deleted when the job ends; the name is not. `lp` submits
    under the filename on disk, and CUPS keeps job history long after removing
    the document -- so a shop's completed-jobs list would read "Medical Results
    Ravi Kumar.pdf" for as long as the Pi runs.
    """
    from types import SimpleNamespace

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"%PDF-1.4 pretend")
        )
    )
    task = SimpleNamespace(
        task_id="prt_abc", filename="Medical Results Ravi Kumar.pdf"
    )

    saved = _backend(client).download(task, tmp_path)

    assert saved.name == "prt_abc.pdf"
    assert "Ravi" not in str(saved)


# ── where an operator can reach this machine ────────────────────────────────


def test_a_heartbeat_carries_the_tailnet_name_when_there_is_one(sent):
    """A kiosk is behind a shop's NAT, so nobody outside can work out where it
    is. The machine has to say, and the heartbeat is the one thing it says
    regularly."""
    seen, client = sent

    _backend(client).heartbeat(agent_version="1.5.1", ssh_host="pi-1.tail1234.ts.net")

    assert seen[0]["body"] == {
        "agent_version": "1.5.1",
        "ssh_host": "pi-1.tail1234.ts.net",
    }


def test_a_machine_with_no_tailnet_sends_no_host_at_all(sent):
    """Not null: sending one would overwrite the last good name the server
    had, and the name somebody would try first is the one it knew before."""
    seen, client = sent

    _backend(client).heartbeat(agent_version="1.5.1", ssh_host=None)

    assert seen[0]["body"] == {"agent_version": "1.5.1"}


def test_a_trailing_dot_is_stripped_from_the_tailnet_name(monkeypatch):
    """Tailscale reports "pi-1.tail1234.ts.net." fully qualified. ssh accepts
    it, but nobody wants to read or copy it."""
    import json
    import subprocess

    from agent import config as config_module

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"Self": {"DNSName": "pi-1.tail1234.ts.net."}}),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert config_module.ssh_host() == "pi-1.tail1234.ts.net"


def test_no_tailscale_means_no_host_rather_than_a_hostname(monkeypatch):
    """`raspberrypi` looks like an answer and reaches nothing. A console saying
    it does not know beats one showing a command that cannot work."""
    import subprocess

    from agent import config as config_module

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(subprocess, "run", missing)

    assert config_module.ssh_host() is None
