"""The thing that makes a browser in fullscreen into a locked screen.

Edge draws the page. This holds it there.

**Browser kiosk mode is not a lock.** `--kiosk` is a presentation mode: Alt+Tab,
the Windows key, Ctrl+W and Alt+F4 all walk out of it, and a web page cannot
install a keyboard hook to stop them. So the page and the lock are two
processes, and this is the lock:

* a global low-level keyboard hook, swallowing the ways out;
* the Edge window kept topmost and foregrounded;
* Edge relaunched if it is closed;
* `Ctrl+Alt+U` (or five taps in the top-left corner) asks for the PIN, and the
  right one releases everything and leaves the desktop.

**It cannot stop printing.** The agent is a third, separate process. This
crashing, being killed, or never being installed changes nothing about whether
a shop prints -- which is why the display is not a window the agent opens.

**Ctrl+Alt+Del is not blockable** from user space, by anything, by design. The
installer disables Task Manager by policy, which closes the useful half of that
door. Holding the power button always wins. Said here rather than discovered at
a counter.
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import subprocess
import sys
import threading
import time
from pathlib import Path

from display import lock as locking

USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
HC_ACTION = 0

VK_TAB, VK_ESCAPE, VK_F4, VK_U = 0x09, 0x1B, 0x73, 0x55
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_CONTROL, VK_MENU, VK_SHIFT = 0x11, 0x12, 0x10

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001

DEFAULT_URL = "http://127.0.0.1:8765"

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT)
)


def _down(vk: int) -> bool:
    """Is this key held right now? The high bit is the answer."""
    return bool(USER32.GetAsyncKeyState(vk) & 0x8000)


class Guard:
    """Everything that has to be undone to give the desktop back."""

    def __init__(self, *, url: str, the_lock: locking.Lock) -> None:
        self.url = url
        self.lock = the_lock
        self.edge: subprocess.Popen | None = None
        self.hook = None
        self._proc = HOOKPROC(self._on_key)
        self.unlocking = False
        self.finished = threading.Event()
        self.failures = 0

    # ── the keyboard ───────────────────────────────────────────────────────

    def _swallow(self, vk: int) -> bool:
        """Is this the beginning of a way out?

        Checked against the modifiers held *now* rather than tracked, because a
        hook that kept its own idea of which keys were down would get it wrong
        the first time a key went up while the screen did not have focus.
        """
        ctrl, alt, shift = _down(VK_CONTROL), _down(VK_MENU), _down(VK_SHIFT)

        if vk in (VK_LWIN, VK_RWIN):
            return True
        if alt and vk == VK_TAB:  # Alt+Tab
            return True
        if alt and vk == VK_F4:  # Alt+F4
            return True
        if ctrl and vk == VK_ESCAPE:  # Ctrl+Esc, the Start menu
            return True
        if ctrl and shift and vk == VK_ESCAPE:  # Task Manager
            return True
        return False

    def _on_key(self, code, wparam, lparam):
        if code == HC_ACTION and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            vk = lparam[0].vkCode

            # The way out, asked for properly.
            if vk == VK_U and _down(VK_CONTROL) and _down(VK_MENU):
                self.ask_for_pin()
                return 1

            if self._swallow(vk):
                # 1 rather than passing to the next hook: the keystroke never
                # reaches the shell, which is the whole point.
                return 1

        return USER32.CallNextHookEx(None, code, wparam, lparam)

    def hold(self) -> None:
        self.hook = USER32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, KERNEL32.GetModuleHandleW(None), 0
        )
        if not self.hook:
            raise OSError(
                "Could not install the keyboard hook, so this would look locked "
                "and not be. Run it as the logged-in user, not as SYSTEM."
            )

    def release(self) -> None:
        if self.hook:
            USER32.UnhookWindowsHookEx(self.hook)
            self.hook = None

    # ── the browser ────────────────────────────────────────────────────────

    def edge_path(self) -> str:
        for candidate in EDGE_PATHS:
            if Path(candidate).is_file():
                return candidate
        raise FileNotFoundError(
            "Microsoft Edge was not found. It ships with Windows 10 and 11; on "
            "a machine without it, install Edge and run this again."
        )

    def open_page(self) -> None:
        self.edge = subprocess.Popen(  # noqa: S603 - a fixed path and our own URL
            [
                self.edge_path(),
                "--kiosk",
                self.url,
                "--edge-kiosk-type=fullscreen",
                "--no-first-run",
                "--disable-features=Translate",
                # A shop screen has no business remembering anything, and a
                # fresh profile means a student cannot leave one page open
                # behind another.
                "--kiosk-idle-timeout-minutes=0",
            ]
        )

    def keep_it_there(self) -> None:
        """Relaunch Edge if it goes, and keep it on top while it is there.

        A student who gets as far as closing the window finds it back within a
        second, which is a shorter game than they were hoping for.
        """
        while not self.finished.is_set():
            if self.edge is not None and self.edge.poll() is not None and not self.unlocking:
                self.open_page()

            window = USER32.GetForegroundWindow()
            if window:
                USER32.SetWindowPos(
                    window, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE
                )
            self.finished.wait(1.0)

    # ── getting out ────────────────────────────────────────────────────────

    def ask_for_pin(self) -> None:
        """The PIN box. Drawn by Tk so it owes nothing to the browser it is
        covering -- a prompt inside the page would be a prompt the page could
        be made to fake."""
        if self.unlocking:
            return
        self.unlocking = True
        threading.Thread(target=self._pin_window, daemon=True).start()

    def _pin_window(self) -> None:
        import tkinter as tk

        wait = locking.wait_after(self.failures)
        root = tk.Tk()
        root.title("Printvendo")
        root.attributes("-topmost", True)
        root.geometry("360x170")
        root.configure(bg="#0a0a0a")

        message = tk.StringVar(
            value=f"Wait {wait}s" if wait else "Enter the PIN to leave this screen"
        )
        tk.Label(
            root, textvariable=message, bg="#0a0a0a", fg="#f2f2ef", pady=14
        ).pack()

        entry = tk.Entry(root, show="•", justify="center", font=("Segoe UI", 20))
        entry.pack(pady=6)
        entry.focus_force()

        started = time.monotonic()

        def submit(_event=None) -> None:
            if time.monotonic() - started < wait:
                return
            if self.lock.opens_with(entry.get()):
                self.failures = 0
                root.destroy()
                self.finish()
                return
            self.failures += 1
            entry.delete(0, tk.END)
            message.set("That is not the PIN.")

        def give_up(_event=None) -> None:
            self.unlocking = False
            root.destroy()

        entry.bind("<Return>", submit)
        root.bind("<Escape>", give_up)
        root.protocol("WM_DELETE_WINDOW", give_up)
        root.mainloop()

    def finish(self) -> None:
        """Give the machine back."""
        self.finished.set()
        self.release()
        if self.edge is not None and self.edge.poll() is None:
            self.edge.terminate()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="printvendo-display")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--set-pin",
        default=None,
        help="set the PIN that unlocks this screen, then exit",
    )
    args = parser.parse_args(argv)

    if args.set_pin:
        where = locking.save(locking.make(args.set_pin))
        print(f"PIN set. {where}")
        return 0

    the_lock = locking.load()
    if the_lock is None:
        # Refused rather than locked with a PIN nobody knows. A screen that
        # cannot be dismissed is a shop PC that has to be power-cycled to use.
        print("No PIN is set, so this would lock the machine with no way out.")
        print("Set one first:  printvendo-display --set-pin 2468")
        return 1

    guard = Guard(url=args.url, the_lock=the_lock)
    try:
        guard.hold()
    except OSError as exc:
        print(str(exc))
        return 1

    try:
        guard.open_page()
    except FileNotFoundError as exc:
        guard.release()
        print(str(exc))
        return 1

    threading.Thread(target=guard.keep_it_there, daemon=True).start()

    # The message pump. A low-level keyboard hook is only called while its
    # installing thread is pumping messages -- without this the hook is
    # installed, does nothing, and the screen looks locked while every key
    # works.
    message = wintypes.MSG()
    while not guard.finished.is_set():
        if USER32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            USER32.TranslateMessage(ctypes.byref(message))
            USER32.DispatchMessageW(ctypes.byref(message))
        time.sleep(0.01)

    guard.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
