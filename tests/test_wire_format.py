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
