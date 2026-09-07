"""Why the keyboard hook was refused, in Windows' own words.

A diagnostic, not part of the lock. `SetWindowsHookExW` returning NULL says
nothing on its own, and the guard's guess -- "run it as the logged-in user" --
was wrong for the first machine that hit it, which was already the logged-in
user.

    printvendo-display-hookcheck

Prints the real error number and message for each way of asking, so the next
machine that refuses says which reason rather than being guessed at.
"""

import ctypes

from display.guard import (
    HOOKPROC,
    KERNEL32,
    LRESULT,
    USER32,
    WH_KEYBOARD_LL,
)


def main(argv: list[str] | None = None) -> int:
    print("python  :", ctypes.sizeof(ctypes.c_void_p) * 8, "bit")
    print("session :", KERNEL32.GetCurrentProcessId())

    def nothing(code, wparam, lparam) -> LRESULT:
        return USER32.CallNextHookEx(None, code, wparam, lparam)

    proc = HOOKPROC(nothing)

    for label, hmod in (
        ("the executable's module handle", KERNEL32.GetModuleHandleW(None)),
        ("NULL, as the docs say for a low-level hook", None),
    ):
        ctypes.set_last_error(0)
        hook = USER32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, hmod, 0)
        code = ctypes.get_last_error()

        if hook:
            print(f"  OK      {label}")
            USER32.UnhookWindowsHookEx(hook)
        else:
            print(f"  refused {label}")
            print(f"          error {code}: {ctypes.FormatError(code).strip()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
