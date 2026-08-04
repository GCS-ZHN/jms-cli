"""Local console abstraction for interactive terminal relays.

Interactive mode needs raw local terminal I/O: no line buffering/echo on
stdin, byte-exact output on stdout, and window-resize notifications.
Those primitives are platform-specific:

- POSIX: termios/tty + select on the stdin fd + SIGWINCH
- Windows: SetConsoleMode (raw-ish input, VT mode) + a ReadConsoleInputW
  reader thread + msvcrt binary stdio

``get_local_console()`` returns the right implementation; backends only
talk to ``LocalConsole``, so the relay loop in ``transport/ssh.py`` and
``transport/ws.py`` stays platform-agnostic.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from abc import ABC, abstractmethod
from ctypes import wintypes
from typing import Callable

from jms.exceptions import TerminalError
from jms.log import logger
from jms.transport.base import local_tty_size

# Windows console mode flags (wincon.h)
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_WINDOW_INPUT = 0x0008
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_LEFT_CTRL_PRESSED = 0x0008
_RIGHT_CTRL_PRESSED = 0x0004
_CTRL_MASK = _LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED

# ReadConsoleInputW event types
_KEY_EVENT = 0x0001
_WINDOW_BUFFER_SIZE_EVENT = 0x0004


class _COORD(ctypes.Structure):
    """Native COORD (console coordinate)."""

    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    """Native KEY_EVENT_RECORD."""

    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    """Native WINDOW_BUFFER_SIZE_RECORD."""

    _fields_ = [("dwSize", _COORD)]


class _INPUT_RECORD_UNION(ctypes.Union):
    """Native INPUT_RECORD event union."""

    _fields_ = [
        ("KeyEvent", _KEY_EVENT_RECORD),
        ("WindowBufferSizeEvent", _WINDOW_BUFFER_SIZE_RECORD),
    ]


class _INPUT_RECORD(ctypes.Structure):
    """Native INPUT_RECORD with anonymous union for direct field access."""

    _anonymous_ = ("Event",)
    _fields_ = [("EventType", wintypes.WORD), ("Event", _INPUT_RECORD_UNION)]


_kernel32: "ctypes.WinDLL | None" = None


def _get_kernel32() -> "ctypes.WinDLL":
    """Load kernel32 and declare the console API signatures (Windows only)."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.GetConsoleMode.restype = wintypes.BOOL
    k32.GetConsoleMode.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    k32.SetConsoleMode.restype = wintypes.BOOL
    k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.ReadConsoleInputW.restype = wintypes.BOOL
    k32.ReadConsoleInputW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_INPUT_RECORD), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32 = k32
    return k32


def _key_to_bytes(char: str, control_key_state: int) -> bytes:
    """Map a console KEY_EVENT char to raw relay bytes.

    Ctrl+letter/punctuation maps to the ASCII control byte, so Ctrl+] is
    0x1D (the relay's disconnect key) and Ctrl+C is 0x03, matching POSIX
    raw-mode semantics. Everything else passes through as UTF-8.
    """
    if not char:
        return b""
    if control_key_state & _CTRL_MASK and 0x40 <= ord(char) <= 0x5F:
        return bytes([ord(char) & 0x1F])
    return char.encode("utf-8", errors="replace")


class LocalConsole(ABC):
    """Raw local terminal I/O used by interactive relay loops."""

    @abstractmethod
    def isatty(self) -> bool:
        """True if stdin is attached to an interactive terminal."""

    @abstractmethod
    def enter_raw(self) -> None:
        """Switch the terminal to raw mode (previous state is saved)."""

    @abstractmethod
    def exit_raw(self) -> None:
        """Restore the terminal state (idempotent, safe before enter_raw)."""

    @abstractmethod
    def wait_stdin(self, timeout: float) -> bool:
        """True if stdin has pending input within ``timeout`` seconds."""

    @abstractmethod
    def read_stdin(self) -> bytes:
        """Return all pending stdin bytes (b'' if none)."""

    @abstractmethod
    def write_stdout(self, data: bytes) -> None:
        """Write raw bytes to stdout unchanged."""

    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Return the local terminal size as ``(cols, rows)``."""

    @abstractmethod
    def on_resize(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the terminal is resized."""


class PosixConsole(LocalConsole):
    """termios/tty based raw console (macOS, Linux)."""

    def __init__(self) -> None:
        self._stdin_fd: int | None = None
        self._old_tty: list | None = None
        self._old_winch: object = None

    def _fd(self) -> int:
        """Resolve the stdin fd lazily (no fileno on pseudo-files)."""
        if self._stdin_fd is None:
            self._stdin_fd = sys.stdin.fileno()
        return self._stdin_fd

    def isatty(self) -> bool:
        return sys.stdin.isatty()

    def enter_raw(self) -> None:
        import termios
        import tty
        fd = self._fd()
        self._old_tty = termios.tcgetattr(fd)
        tty.setraw(fd)

    def exit_raw(self) -> None:
        import signal
        if self._old_tty is not None:
            import termios
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_tty)
            self._old_tty = None
        if self._old_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._old_winch)
            except (ValueError, OSError):
                pass
            self._old_winch = None

    def wait_stdin(self, timeout: float) -> bool:
        import select
        rlist, _, _ = select.select([self._fd()], [], [], timeout)
        return bool(rlist)

    def read_stdin(self) -> bytes:
        return os.read(self._fd(), 4096)

    def write_stdout(self, data: bytes) -> None:
        os.write(sys.stdout.fileno(), data)

    def size(self) -> tuple[int, int]:
        return local_tty_size()

    def on_resize(self, callback: Callable[[], None]) -> None:
        import signal
        self._old_winch = signal.signal(signal.SIGWINCH, lambda *_: callback())


class WindowsConsole(LocalConsole):
    """Raw console over the Windows console API (ctypes + msvcrt).

    Raw-ish input via SetConsoleMode (VT input mode, no line buffering/
    echo, window input for resize events, QuickEdit disabled); keys are
    read by a background ReadConsoleInputW thread into a byte buffer.
    """

    def __init__(self) -> None:
        console = _console()
        self._h_in = console[0] if console else None
        self._h_out = console[1] if console else None
        self._in_mode: int | None = console[2] if console else None
        self._out_mode: int | None = console[3] if console else None
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._resize_callback: Callable[[], None] | None = None
        self._running = False
        self._reader: threading.Thread | None = None
        self._old_stdin_mode: int | None = None
        self._old_stdout_mode: int | None = None

    def isatty(self) -> bool:
        return self._in_mode is not None and self._out_mode is not None

    def enter_raw(self) -> None:
        if not self.isatty():
            raise TerminalError("Interactive mode requires a TTY on stdin")
        import msvcrt

        in_mode = (
            (self._in_mode or 0)
            | _ENABLE_VIRTUAL_TERMINAL_INPUT
            | _ENABLE_WINDOW_INPUT
            | _ENABLE_EXTENDED_FLAGS
        ) & ~(
            _ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT
            | _ENABLE_PROCESSED_INPUT | _ENABLE_QUICK_EDIT_MODE
        )
        out_mode = (
            (self._out_mode or 0)
            | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
            | _ENABLE_WRAP_AT_EOL_OUTPUT
        ) & ~_ENABLE_PROCESSED_OUTPUT
        k32 = _get_kernel32()
        if not k32.SetConsoleMode(self._h_in, in_mode) or \
                not k32.SetConsoleMode(self._h_out, out_mode):
            raise TerminalError("Failed to switch the Windows console to raw mode")
        self._old_stdin_mode = msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        self._old_stdout_mode = msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        self._running = True
        self._reader = threading.Thread(
            target=self._reader_loop, name="jms-console-reader", daemon=True,
        )
        self._reader.start()

    def exit_raw(self) -> None:
        import msvcrt

        self._running = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._in_mode is not None:
            _get_kernel32().SetConsoleMode(self._h_in, self._in_mode)
        if self._out_mode is not None:
            _get_kernel32().SetConsoleMode(self._h_out, self._out_mode)
        if self._old_stdin_mode is not None:
            try:
                msvcrt.setmode(sys.stdin.fileno(), self._old_stdin_mode)
            except OSError:
                pass
        if self._old_stdout_mode is not None:
            try:
                msvcrt.setmode(sys.stdout.fileno(), self._old_stdout_mode)
            except OSError:
                pass
        self._old_stdin_mode = None
        self._old_stdout_mode = None

    def wait_stdin(self, timeout: float) -> bool:
        with self._cond:
            if self._buffer:
                return True
            self._cond.wait(timeout)
            return bool(self._buffer)

    def read_stdin(self) -> bytes:
        with self._cond:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data

    def write_stdout(self, data: bytes) -> None:
        fd = sys.stdout.fileno()
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + 32 * 1024]
            offset += os.write(fd, chunk)

    def size(self) -> tuple[int, int]:
        return local_tty_size()

    def on_resize(self, callback: Callable[[], None]) -> None:
        self._resize_callback = callback

    def _reader_loop(self) -> None:
        k32 = _get_kernel32()
        records = (_INPUT_RECORD * 64)()
        n_read = wintypes.DWORD()
        while self._running:
            if not k32.ReadConsoleInputW(
                self._h_in, records, 64, ctypes.byref(n_read),
            ):
                break
            for idx in range(n_read.value):
                record = records[idx]
                if record.EventType == _KEY_EVENT:
                    self._handle_key(record.KeyEvent)
                elif record.EventType == _WINDOW_BUFFER_SIZE_EVENT:
                    self._notify_resize()

    def _handle_key(self, key: _KEY_EVENT_RECORD) -> None:
        if not key.bKeyDown:
            return
        data = _key_to_bytes(key.uChar, key.dwControlKeyState)
        if not data:
            return
        with self._cond:
            self._buffer.extend(data)
            self._cond.notify()

    def _notify_resize(self) -> None:
        callback = self._resize_callback
        if callback is None:
            return
        try:
            callback()
        except Exception as e:  # a resize handler must never kill the reader
            logger.debug("console resize handler error: %s", e)


def _console() -> tuple | None:
    """Return (h_in, h_out, in_mode, out_mode), or None without a console."""
    k32 = _get_kernel32()
    handles_modes: list[tuple] = []
    for handle_id in (-10, -11):  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE
        handle = k32.GetStdHandle(wintypes.DWORD(handle_id))
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        handles_modes.append((handle, mode.value))
    (h_in, in_mode), (h_out, out_mode) = handles_modes
    return h_in, h_out, in_mode, out_mode


def get_local_console() -> LocalConsole:
    """Return the platform's local console implementation."""
    if os.name == "nt":
        return WindowsConsole()
    return PosixConsole()
