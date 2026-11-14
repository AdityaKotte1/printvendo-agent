"""Fetching a file over a connection that keeps dropping.

A shop on unstable internet lost the connection partway through a download.
The agent started again from byte zero, lost it again, and the job sat at
"paid" on the student's screen for as long as that went on -- or failed
outright on the first drop. Now the bytes already on disk are kept, the rest
is asked for with `Range`, and the printer sees the file only once every byte
has arrived.
"""

from types import SimpleNamespace

import httpx
import pytest

from agent import api
from agent.api import Backend, DownloadIncomplete

BASE = "https://api.example.test"
FILE = bytes(range(256)) * 400  # 100 KiB, every byte position distinguishable


class Dropping(httpx.SyncByteStream):
    """Yields some bytes, then the connection dies."""

    def __init__(self, data: bytes, cut: int) -> None:
        self.data, self.cut = data, cut

    def __iter__(self):
        yield self.data[: self.cut]
        raise httpx.ReadError("connection reset by peer")


class Server:
    """Answers `Range` as the backend does, and drops the first `drops`
    connections after `cut` bytes of whatever it was sending."""

    def __init__(self, *, drops: int = 0, cut: int = 30_000, honours_range=True):
        self.drops, self.cut, self.honours_range = drops, cut, honours_range
        self.ranges: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        asked = request.headers.get("range")
        self.ranges.append(asked)
        start = int(asked.split("=")[1].rstrip("-")) if asked and self.honours_range else 0
        body = FILE[start:]
        headers = {"accept-ranges": "bytes", "content-length": str(len(body))}
        status = 200
        if start:
            status = 206
            headers["content-range"] = f"bytes {start}-{len(FILE) - 1}/{len(FILE)}"
        if self.drops:
            self.drops -= 1
            return httpx.Response(status, headers=headers, stream=Dropping(body, self.cut))
        return httpx.Response(status, headers=headers, content=body)


def _task():
    return SimpleNamespace(task_id="prt_abc")


def _fetch(server, tmp_path, **kwargs):
    backend = Backend(BASE, "dvt_x", client=httpx.Client(transport=httpx.MockTransport(server)))
    return backend.download(_task(), tmp_path, sleep=lambda _s: None, **kwargs)


def test_a_good_connection_fetches_in_one_go(tmp_path):
    server = Server()
    path = _fetch(server, tmp_path)
    assert path.read_bytes() == FILE
    assert server.ranges == [None]


def test_a_dropped_connection_resumes_where_it_stopped(tmp_path):
    """Not from byte zero: on a bad link, starting over is how a download
    never finishes."""
    server = Server(drops=2, cut=30_000)

    path = _fetch(server, tmp_path)

    assert path.read_bytes() == FILE
    assert server.ranges == [None, "bytes=30000-", "bytes=60000-"]


def test_nothing_is_handed_to_the_printer_until_every_byte_is_here(tmp_path):
    """A half file printed is half a document a student paid for. While it is
    incomplete it lives under another name, so nothing can mistake it."""
    server = Server(drops=1000)

    with pytest.raises(DownloadIncomplete):
        _fetch(server, tmp_path, patience=0)

    assert not (tmp_path / "prt_abc.pdf").exists()


def test_a_server_that_ignores_the_range_starts_the_file_again(tmp_path):
    """A 200 to a ranged request is the whole file. Appending it to what we
    had would print a corrupt document."""
    server = Server(drops=1, honours_range=False)

    path = _fetch(server, tmp_path)

    assert path.read_bytes() == FILE


def test_the_heartbeat_keeps_beating_while_it_waits(tmp_path):
    """The heartbeat runs only when the agent calls it. A download that takes
    minutes on a bad link would otherwise make the shop look offline -- and
    let the job's lease run out while its bytes were still arriving."""
    ticks: list[int] = []
    server = Server(drops=3)

    _fetch(server, tmp_path, on_tick=lambda: ticks.append(1))

    assert len(ticks) >= 3


def test_a_refusal_is_not_retried(tmp_path):
    """409 means the job is no longer ours, 404 that the file is gone. Asking
    again for ten minutes would hold a job that is not coming."""
    calls: list[int] = []

    def refuses(request):
        calls.append(1)
        return httpx.Response(409, json={"detail": "no longer held"})

    with pytest.raises(httpx.HTTPStatusError):
        _fetch(refuses, tmp_path, patience=0)
    assert len(calls) == 1


def test_a_server_hiccup_is_retried(tmp_path):
    answers = [httpx.Response(502), None]
    server = Server()

    def flaky(request):
        first = answers.pop(0) if answers else None
        return first if first is not None else server(request)

    assert _fetch(flaky, tmp_path).read_bytes() == FILE


def test_it_gives_up_well_inside_the_lease(tmp_path):
    """The server fails a job whose lease runs out; giving up first means the
    agent reports it, rather than the server guessing."""
    assert api.DOWNLOAD_PATIENCE_SECONDS < 15 * 60


def test_a_connection_that_closes_cleanly_but_short_is_resumed(tmp_path):
    """Not every drop is an error: a proxy can end the response early and
    politely. The size the server announced is what decides "complete"."""
    calls: list[int] = []
    server = Server()

    def short_once(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"content-length": str(len(FILE))},
                content=FILE[:40_000],
            )
        return server(request)

    path = _fetch(short_once, tmp_path)

    assert path.read_bytes() == FILE
    assert server.ranges == ["bytes=40000-"]
