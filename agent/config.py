"""Where the machine keeps what it knows, and how it finds its printer.

Three values: the API, the device token, and which printer to use. Everything
else is discovered, because a setup that asks fewer questions is a setup that
gets done correctly at a shop counter with somebody waiting.

The token is a credential. The file is written `0600` where the platform
supports it, and it is the only place the token exists on this machine.
"""

import json
import os
import platform
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.pools import Pools

IS_WINDOWS = platform.system() == "Windows"

DEFAULT_API = "https://api.printvendo.com"


def config_path() -> Path:
    """Beside the agent on Windows, under /etc on a Pi.

    `PRINTVENDO_CONFIG` overrides both, which is what lets one machine run two
    agents while testing without either of them finding the other's token.
    """
    override = os.environ.get("PRINTVENDO_CONFIG")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Printvendo" / "agent.json"
    return Path("/etc/printvendo/agent.json")


@dataclass
class Config:
    api_url: str = DEFAULT_API
    token: str = ""
    # The machine a one-printer shop uses, which is nearly all of them. Kept as
    # the answer when no pools are set rather than migrated to a one-item list:
    # every kiosk in the field is configured this way and none should have to
    # be touched.
    printer: str = ""
    # A shop with several machines -- a xerox counter running two mono printers
    # and a colour one off one agent. Mono work spreads across `printers_bw`,
    # colour work across `printers_colour`, and colour never falls back to a
    # mono machine: it would come out grey on a job the student paid colour
    # prices for. See `agent.pools`.
    printers_bw: list[str] = field(default_factory=list)
    printers_colour: list[str] = field(default_factory=list)
    # Recorded so an operator can tell one physical box from another over SSH.
    # An identifier, never a credential -- it survives re-enrolment.
    device_key: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls()
        return cls(**{**asdict(cls()), **json.loads(path.read_text(encoding="utf-8"))})

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        if not IS_WINDOWS:
            # The token is a credential and this file is where it lives.
            path.chmod(0o600)
        return path

    @property
    def ready(self) -> bool:
        return bool(self.token and self.api_url and self.any_printer)

    @property
    def any_printer(self) -> bool:
        """At least one machine to print on, however it was configured."""
        return bool(self.printer or self.printers_bw or self.printers_colour)

    @property
    def every_printer(self) -> list[str]:
        """Every machine this agent may use, without duplicates.

        `check` validates all of them: a pool naming a printer CUPS has never
        heard of fails one kind of job and only that kind, which is the sort of
        fault that gets found by a student rather than by an installer.
        """
        seen: list[str] = []
        for name in [self.printer, *self.printers_bw, *self.printers_colour]:
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def pools(self) -> "Pools":
        from agent.pools import Pools

        return Pools(
            bw=list(self.printers_bw),
            colour=list(self.printers_colour),
            fallback=self.printer,
        )


def printers() -> list[str]:
    """Every printer this machine can see, in the platform's own words."""
    if IS_WINDOWS:
        return _windows_printers()
    return _cups_printers()


def default_printer() -> str | None:
    """The printer to use when nobody said.

    A shop with one printer -- which is nearly all of them -- should never have
    to answer this question. A shop with several must, because guessing means
    somebody's dissertation comes out on the label machine.
    """
    found = printers()
    if len(found) == 1:
        return found[0]
    return None


def _cups_printers() -> list[str]:
    try:
        result = subprocess.run(
            ["lpstat", "-e"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _windows_printers() -> list[str]:
    try:
        import win32print
    except ImportError:
        return []

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [printer[2] for printer in win32print.EnumPrinters(flags)]
