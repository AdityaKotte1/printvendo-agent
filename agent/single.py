"""One agent per machine, enforced rather than remembered.

Two agents on one Pi both claim work. `FOR UPDATE SKIP LOCKED` stops them being
handed the *same* task, so nothing prints twice -- but they submit
independently, so the shop's queue ends up several jobs deep with no gap
between them. A student who walks up while somebody else's fifteen pages are
coming out takes the wrong sheets, and the pile in the tray is nobody's.

It happens the ordinary way: somebody runs `printvendo-agent run` over SSH to
watch it work, forgets, and walks away leaving it running beside the service.
Nothing anywhere said no.

An advisory lock on a file, held for the life of the process. Not a PID file: a
PID file records a claim and a crash leaves it lying, so the next start has to
guess whether the process it names is still alive. A lock is released by the
kernel when the holder dies, however it dies -- nothing to clean up, nothing to
guess.
"""

import os
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Past anything written into the file, so the lock never collides with the pid
# text. Windows locks a byte range from the current position and the file is
# sparse, so the byte need not exist.
_LOCK_BYTE = 4096


def lock_path() -> Path:
    """Beside the machine's own state, never in /tmp.

    /tmp is cleared on some systems while a long print is still running, and a
    lock that can vanish underneath its holder is not a lock.
    """
    override = os.environ.get("PRINTVENDO_LOCK")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Printvendo" / "agent.lock"
    return Path("/var/lock/printvendo-agent.lock")


class AlreadyRunning(RuntimeError):
    """Another agent holds the lock on this machine."""


@contextmanager
def only_one_agent(path: Path | None = None):
    """Hold the machine's agent lock, or refuse to start.

    Raises `AlreadyRunning` rather than exiting, so the caller decides what a
    person sees. It guards against a second *agent*, which is the thing that
    actually happens -- not against somebody printing from the desktop, which
    CUPS already serialises.
    """
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    handle = None
    try:
        # Opening is inside the guard because on Windows the lock is
        # *mandatory*: a second process is refused at open() rather than at the
        # lock call. On Linux it is advisory and the refusal comes from flock.
        # Both mean the same thing to a caller -- somebody is already printing
        # on this machine.
        handle = path.open("a+")
        # Written before the lock is taken, so it is never inside the locked
        # range. For a person reading the file over SSH; never read back here,
        # because the lock is the truth and this is a courtesy.
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()) + "\n")
        handle.flush()
        _take(handle)
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise AlreadyRunning(f"another printvendo-agent already holds {path}") from exc

    try:
        yield
    finally:
        try:
            _release(handle)
        finally:
            handle.close()


def _take(handle) -> None:
    if IS_WINDOWS:
        import msvcrt

        handle.seek(_LOCK_BYTE)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle) -> None:
    if IS_WINDOWS:
        import msvcrt

        try:
            handle.seek(_LOCK_BYTE)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            # Already gone, or never taken. Closing the handle releases it
            # either way, and failing to unlock on the way out must not be the
            # thing that crashes a shutdown.
            pass
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
