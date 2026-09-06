"""What the shop screen can and cannot fetch."""

import json
import urllib.error
import urllib.request

import pytest

from agent import display_server


@pytest.fixture
def served():
    """A real server on a free port, so the routing is the library's own."""
    state = {"kiosk_name": "SRK Xerox", "online": True}
    server = display_server.serve(lambda: state, port=8791)
    assert server is not None
    try:
        yield "http://127.0.0.1:8791"
    finally:
        # Shut down, or the port stays held and every test after this one
        # cannot bind it -- `allow_reuse_address` is off on purpose.
        server.shutdown()
        server.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def test_the_status_route_answers_what_it_was_given(served):
    status, body = _get(f"{served}/status")

    assert status == 200
    assert json.loads(body) == {"kiosk_name": "SRK Xerox", "online": True}


def test_the_status_is_never_cached(served):
    """The page polls every couple of seconds. A cached status is a screen
    saying a shop is online after it has gone."""
    with urllib.request.urlopen(f"{served}/status", timeout=5) as response:
        assert response.headers["Cache-Control"] == "no-store"


def test_a_path_climbing_out_of_the_page_folder_is_refused(served):
    r"""`C:\ProgramData\Printvendo\agent.json` holds the device token, which
    *is* the kiosk. A file server one directory away from it must not be
    talked into reading upwards."""
    with pytest.raises(urllib.error.HTTPError) as refused:
        _get(f"{served}/../../agent/config.py")

    assert refused.value.code in (400, 404)


def test_a_file_that_is_not_there_is_a_404_not_a_crash(served):
    with pytest.raises(urllib.error.HTTPError) as missing:
        _get(f"{served}/nothing.css")

    assert missing.value.code == 404


def test_a_taken_port_does_not_stop_the_agent(served):
    """The shop screen is a nicety; printing is the job. Refusing to start
    because a display could not be served would trade the whole shop for a
    poster."""
    again = display_server.serve(lambda: {}, port=8791)

    assert again is None
