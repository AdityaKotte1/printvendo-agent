"""Serving the shop screen, to this machine and nowhere else.

Two routes and a thread. `/` is the page; `/status` is what `agent.status`
built. Bound to 127.0.0.1 on purpose: a shop's screen is on the shop's own PC,
and a status page reachable from the shop's wifi would tell anyone in the
building what that counter is doing.

**It holds no credential.** The display never talks to the backend -- the agent
does, and hands over only what `agent.status.document` allows. So there is
nothing here to steal and nothing to enrol, and a display that is never
installed changes nothing about how the shop prints.

Run in a daemon thread beside the print loop. It must never be able to stop a
job: every failure here is logged and swallowed, and the thread dying leaves
the agent printing exactly as before.
"""

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("agent")

# High and unremarkable. Nothing else claims it, and it is only ever reached
# over the loopback address.
DEFAULT_PORT = 8765

PAGE_DIR = Path(__file__).resolve().parent.parent / "display" / "page"


class _Server(ThreadingHTTPServer):
    """`allow_reuse_address` off, deliberately.

    `ThreadingHTTPServer` turns it on, which on Linux means "reuse a port stuck
    in TIME_WAIT" and on **Windows** means something else entirely: a second
    process may bind a port another process is already listening on, and the
    two then split the connections between them. A second agent would report
    that its display was serving, and the screen would show whichever socket
    happened to win each poll.

    Off, the second bind fails, `serve` returns None, and the log says so.
    """

    allow_reuse_address = False


class _Handler(BaseHTTPRequestHandler):
    """The two routes, and nothing that could become a third by accident."""

    # Set by `serve`.
    snapshot: Callable[[], dict]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        route = self.path.split("?", 1)[0]

        if route == "/status":
            self._json(self.snapshot())
            return

        # Everything else is the page and its two assets. Resolved against a
        # fixed directory and checked to still be inside it, so a request for
        # `/../../ProgramData/Printvendo/agent.json` cannot read the token.
        name = {"/": "index.html"}.get(route, route.lstrip("/"))
        try:
            path = (PAGE_DIR / name).resolve()
            path.relative_to(PAGE_DIR.resolve())
        except (ValueError, OSError):
            self.send_error(404)
            return

        if not path.is_file():
            self.send_error(404)
            return

        self._file(path)

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # The page polls every couple of seconds; a cached status is a screen
        # that says a shop is online after it has gone.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        kinds = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".woff2": "font/woff2",
            ".svg": "image/svg+xml",
        }
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kinds.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Silence. The agent's log is about printing, and a page polling twice
        a second would bury every line that matters."""
        return


def serve(
    snapshot: Callable[[], dict], *, port: int = DEFAULT_PORT
) -> ThreadingHTTPServer | None:
    """Start the display server in the background. Returns None if it could not.

    Returns the server rather than its thread, so a caller that needs to stop
    it can. The agent never does -- it is a daemon thread and dies with the
    process -- but a test that could not shut it down would leave the port held
    for every test after it.

    A port already in use -- a second agent, or something else on 8765 -- is
    reported once and then ignored. The shop screen is a nicety; printing is
    the job, and refusing to start the agent because a display could not be
    served would trade the whole shop for a poster.
    """
    _Handler.snapshot = staticmethod(snapshot)  # type: ignore[assignment]

    try:
        server = _Server(("127.0.0.1", port), _Handler)
    except OSError as exc:
        log.warning("the shop display is not being served on %s: %s", port, exc)
        return None

    threading.Thread(target=server.serve_forever, name="display", daemon=True).start()
    log.info("the shop display is at http://127.0.0.1:%s", port)
    return server
