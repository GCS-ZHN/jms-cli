#!/usr/bin/env python3
"""Windows terminal-stack feasibility probe (runs headless, e.g. CI).

On a Windows host (including ``windows-latest`` GitHub runners where no
interactive console is attached) empirically reports which pieces of a
local interactive-terminal stack are usable:

    1. console attachment -- GetConsoleMode on stdin/stdout
    2. stdlib console primitives -- msvcrt (kbhit/getch/getwch/setmode)
    3. pywinpty / ConPTY -- spawn cmd.exe, write/read, resize
    4. windows-curses -- import availability

On non-Windows platforms the script prints SKIP and exits 0, so the same
invocation can be wired into CI without platform conditionals.

The probe never asserts: each section prints PASS/FAIL with evidence and
the exit code stays 0. Read the report, don't parse the exit code.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes

RESULTS: list[tuple[str, bool, str]] = []


def probe(name: str, fn) -> None:
    """Run one probe and record (name, ok, detail)."""
    try:
        ok, detail = fn()
    except Exception as exc:  # a probe must never take the script down
        RESULTS.append((name, False, f"raised {type(exc).__name__}: {exc}"))
        return
    RESULTS.append((name, bool(ok), str(detail)))


def probe_console() -> tuple[bool, str]:
    """Check whether stdin/stdout are attached to an interactive console."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.GetConsoleMode.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    lines = []
    for label, handle_id in (("stdin", -10), ("stdout", -11)):
        handle = kernel32.GetStdHandle(wintypes.DWORD(handle_id))
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            lines.append(f"{label}: console attached (mode=0x{mode.value:08x})")
        else:
            lines.append(f"{label}: no console (err={ctypes.get_last_error()})")
    return True, "; ".join(lines)


def probe_msvcrt() -> tuple[bool, str]:
    """Check stdlib console primitives and binary-mode setmode."""
    import msvcrt

    names = ("kbhit", "getch", "getwch", "setmode")
    missing = [n for n in names if not hasattr(msvcrt, n)]
    if missing:
        return False, f"missing: {missing}"
    fd, path = tempfile.mkstemp()
    try:
        msvcrt.setmode(fd, os.O_BINARY)
    finally:
        os.close(fd)
        os.unlink(path)
    return True, "kbhit/getch/getwch/setmode present; setmode(O_BINARY) ok"


def _bounded_read(reader, timeout: float = 2.0) -> bytes:
    """Read from a possibly-blocking call in a bounded daemon thread."""
    result: list[bytes] = []

    def pump() -> None:
        try:
            chunk = reader()
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            result.append(chunk)
        except Exception:
            pass

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    thread.join(timeout)
    return b"".join(result)


def _api_call(fn, *payloads) -> None:
    """Call fn with the first payload whose type the API accepts."""
    for payload in payloads:
        try:
            fn(payload)
            return
        except TypeError:
            continue
    fn(payloads[0])  # surface the real error for reporting


def probe_pywinpty() -> tuple[bool, str]:
    """Spawn cmd.exe through pywinpty (ConPTY), echo, then resize."""
    try:
        import winpty
    except ImportError as exc:
        return False, f"import failed: {exc}"

    notes = [
        f"import ok (PTY={hasattr(winpty, 'PTY')}, "
        f"PtyProcess={hasattr(winpty, 'PtyProcess')})",
    ]
    ok = True
    try:
        from winpty import PTY

        pty = PTY(80, 25)
        _api_call(pty.spawn, r"C:\windows\system32\cmd.exe",
                  r"C:\windows\system32\cmd.exe".encode("utf-8"))
        _api_call(pty.write, b"echo PYWINPTY_OK\r\n",
                  "echo PYWINPTY_OK\r\n")
        out = b""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and b"PYWINPTY_OK" not in out:
            out += _bounded_read(pty.read)
            time.sleep(0.05)
        echoed = b"PYWINPTY_OK" in out
        notes.append(
            f"spawn+write+read: {'ok' if echoed else 'no echo'} "
            f"(out={out[:120]!r})",
        )
        pty.set_size(100, 30)
        notes.append("set_size(100, 30): ok")
        notes.append(f"isalive: {pty.isalive()}")
        _api_call(pty.write, "exit\r\n", b"exit\r\n")
        del pty
        ok = echoed
    except Exception as exc:
        notes.append(f"ConPTY flow failed: {type(exc).__name__}: {exc}")
        ok = False
    return ok, "; ".join(notes)


def probe_curses() -> tuple[bool, str]:
    """Report whether a curses implementation (windows-curses) imports."""
    try:
        import curses  # noqa: F401
    except ImportError as exc:
        return False, f"windows-curses not installed: {exc}"
    return True, "curses importable (windows-curses present)"


def main() -> int:
    """Run every probe and print the report."""
    if os.name != "nt":
        print("SKIP: windows_feasibility.py is Windows-only")
        return 0

    print("=== Windows terminal-stack feasibility probe ===")
    probe("console attachment (GetConsoleMode)", probe_console)
    probe("msvcrt primitives", probe_msvcrt)
    probe("pywinpty / ConPTY (spawn cmd.exe, echo, resize)", probe_pywinpty)
    probe("windows-curses", probe_curses)

    print()
    for name, ok, detail in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nsummary: {passed}/{len(RESULTS)} probes reported OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
