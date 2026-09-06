"""The PIN that lets somebody out, and how slowly it may be guessed.

Kept apart from `guard.py` because everything here is arithmetic and can be
tested without a keyboard hook, a browser or a screen -- the same split as
`build_windows_command` and the printing it drives.

**The PIN is not stored.** A salted PBKDF2 digest is. This file sits on a shop
PC that students stand in front of, and a plaintext PIN in it would be a PIN
written on the counter.

**It is not in `agent.json`.** That file holds the device token, which *is* the
kiosk: anyone with it can claim that shop's print jobs. This is a counter
convenience for getting to the desktop. They must not share a blast radius, so
they do not share a file.
"""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path

# Deliberately high for four digits. A PIN has ~10^4 possibilities, so the only
# thing standing between a stolen config file and the PIN is how long each
# guess takes -- 480k rounds makes an offline sweep of the whole space take
# hours on a shop PC rather than seconds.
ROUNDS = 480_000

# How long a wrong PIN costs, in seconds, by attempt. The first two are free:
# somebody who has just mistyped should not be punished for it. After that it
# grows, so a bored student with an afternoon runs out of afternoon.
BACKOFF = (0, 0, 5, 15, 45, 120)


def config_path() -> Path:
    """Beside the agent's settings, not inside them."""
    override = os.environ.get("PRINTVENDO_DISPLAY_CONFIG")
    if override:
        return Path(override)
    root = os.environ.get("PROGRAMDATA", "C:/ProgramData")
    return Path(root) / "Printvendo" / "display.json"


@dataclass(frozen=True)
class Lock:
    salt: str
    digest: str

    def opens_with(self, pin: str) -> bool:
        """Whether this PIN is the one.

        `compare_digest`, not `==`: string comparison stops at the first wrong
        character, and the time it takes says how much of the PIN was right.
        """
        return hmac.compare_digest(self.digest, _digest(pin, self.salt))


def make(pin: str) -> Lock:
    salt = os.urandom(16).hex()
    return Lock(salt=salt, digest=_digest(pin, salt))


def _digest(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt), ROUNDS
    ).hex()


def save(lock: Lock, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"salt": lock.salt, "digest": lock.digest}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load(path: Path | None = None) -> Lock | None:
    """The lock, or None if this machine has no PIN set.

    None means the guard refuses to lock at all rather than locking with a PIN
    nobody knows -- a screen that cannot be dismissed is a shop PC that has to
    be power-cycled to use.
    """
    path = path or config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Lock(salt=raw["salt"], digest=raw["digest"])
    except (ValueError, KeyError, OSError):
        return None


def wait_after(failures: int) -> int:
    """Seconds to refuse for, after this many wrong tries in a row."""
    if failures < len(BACKOFF):
        return BACKOFF[failures]
    return BACKOFF[-1]
