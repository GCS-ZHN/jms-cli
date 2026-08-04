"""WebSocket terminal backend via the KoKo ``/koko/ws/terminal/`` endpoint.

Notes (from reverse engineering — do not change):
    - Must use ``/koko/ws/terminal/``; ``/koko/ws/token/`` is 404 on
      some JumpServer versions
    - The handshake must carry the ``jms_sessionid`` cookie obtained
      from the form login
    - Terminal output arrives as **binary frames** (opcode 2); command
      input is a text-frame JSON
      ``{"type": "TERMINAL_DATA", "data": "cmd\\r"}``
    - Keepalive uses **application-level PING text frames** (the Nginx
      WS reverse proxy is a transparent TCP tunnel, so WS opcode 0x9
      pings never reach KoKo)
"""

import json
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import urlparse

import websocket

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.transport.base import AbstractTerminal, TerminalCapability, strip_ansi
from jms.transport.console import get_local_console
from jms.transport.registry import register_backend
from jms.transport.token import create_connection_token
from jms.exceptions import TerminalError
from jms.log import logger

# Timeouts (seconds)
SHELL_READY_TIMEOUT: int = 15
COMMAND_TIMEOUT: int = 30
WS_CONNECT_TIMEOUT: int = 15

# Heartbeat interval: must be well under KoKo's 5-minute read timeout
# and under Nginx's default proxy_read_timeout (60s)
HEARTBEAT_INTERVAL: int = 30


_RC_RE = re.compile(rb"__JMSRC:(\d+)__")


def _has_rc(data: bytes) -> bool:
    """True when the raw stream contains a captured exit-code marker."""
    return bool(_RC_RE.search(data))


def _parse_rc(clean: str, marker: str) -> int | None:
    """Extract the ``__JMSRC:N__`` exit marker after the done-marker.

    Returns the exit code, or ``None`` if the marker is missing.
    """
    second = clean.find(marker, clean.find(marker) + len(marker))
    if second == -1:
        return None
    tail = clean[second + len(marker):]
    rc_match = re.search(r"__JMSRC:(\d+)__", tail)
    return int(rc_match.group(1)) if rc_match else None


class WSTerminal(AbstractTerminal):
    """WebSocket-based terminal session to a JumpServer asset.

    Provides command execution with marker-based output detection, an
    interactive PTY relay with heartbeat, and low-level WebSocket
    access (used by the transfer module).

    Args:
        ws: Connected WebSocket instance.
        ws_id: Session UUID from the CONNECT message.
    """

    capabilities = frozenset({TerminalCapability.EXEC, TerminalCapability.INTERACTIVE})

    def __init__(self, ws: websocket.WebSocket, ws_id: str) -> None:
        self._ws: websocket.WebSocket = ws
        self._ws_id: str = ws_id
        self._closed: bool = False
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop: threading.Event = threading.Event()

    @property
    def ws(self) -> websocket.WebSocket:
        """Underlying WebSocket (used by the transfer module)."""
        return self._ws

    @property
    def ws_id(self) -> str:
        """Session UUID."""
        return self._ws_id

    @property
    def backend_name(self) -> str:
        """Backend name."""
        return "websocket"

    def wait_for_prompt(self, timeout: int = SHELL_READY_TIMEOUT) -> bool:
        """Drain the SSH banner/MOTD and wait for the shell prompt.

        Readiness is decided by matching a common prompt suffix, or by
        the remote going silent (3 consecutive recv timeouts after data
        was received) — the latter copes with custom prompts
        (Powerline, Nerd Font, etc.).

        Returns:
            True when a prompt is detected, False on timeout.
        """
        start = time.time()
        buf = b""
        idle_count = 0
        while time.time() - start < timeout:
            try:
                self._ws.settimeout(2)
                opcode, data = self._ws.recv_data()
                idle_count = 0
                if opcode == 2:
                    buf += data
                    clean = strip_ansi(buf.decode("utf-8", errors="replace"))
                    if clean.rstrip().endswith(("$ ", "# ", "$", "#")):
                        return True
                elif opcode == 1:
                    self._handle_control_message(data)
            except websocket.WebSocketTimeoutException:
                clean = strip_ansi(buf.decode("utf-8", errors="replace"))
                if clean.rstrip().endswith(("$ ", "# ", "$", "#")):
                    return True
                if buf:
                    idle_count += 1
                    if idle_count >= 3:
                        logger.debug(
                            "Shell idle detected (custom prompt, "
                            "remote stopped sending)."
                        )
                        return True
            except Exception as e:
                # Connection dead (not a timeout): check then exit, to
                # avoid busy-spinning until the timeout
                logger.debug("wait_for_prompt recv error: %s", e)
                clean = strip_ansi(buf.decode("utf-8", errors="replace"))
                if clean.rstrip().endswith(("$ ", "# ", "$", "#")):
                    return True
                break
        logger.warning(
            "Shell prompt not detected within %ds. "
            "The remote shell may be slow to initialise.",
            timeout,
        )
        return False

    def execute(
        self, command: str, timeout: int = COMMAND_TIMEOUT, check: bool = False,
    ) -> str:
        """Execute a command and return its stdout output.

        A unique marker is echoed after the command; the marker appears
        twice in the stream (once as command echo, once as echo output).
        The content between the first and second marker is the command
        output. The remote exit status is captured as ``__JMSRC:N__``
        after the second marker. Residual data is drained before
        returning so it cannot pollute the next execute.

        Args:
            command: Shell command to execute.
            timeout: Maximum seconds to wait for output.
            check: When true, raise ``TerminalError`` (carrying the remote
                exit code) if the command exits non-zero or times out.

        Returns:
            Command output string (stripped).

        Raises:
            TerminalError: Non-zero remote exit or timeout when ``check``
                is set.

        Note:
            Commands ending in ``#``, ``\\``, or with unbalanced quotes
            swallow the appended rc-capture chain and hang until timeout.
        """
        marker = f"__JMSDONE_{int(time.time() * 1000)}__"
        full_cmd = f"{command}; __rc=$?; echo {marker}; echo __JMSRC:${{__rc}}__"

        try:
            self._ws.send(json.dumps({
                "id": self._ws_id,
                "type": "TERMINAL_DATA",
                "data": full_cmd + "\r",
            }))
        except Exception as e:
            raise TerminalError(f"WS send failed: {e}") from e

        output = b""
        recv_failed = False
        start = time.time()
        while time.time() - start < timeout:
            try:
                self._ws.settimeout(3)
                opcode, data = self._ws.recv_data()
                if opcode == 2:
                    output += data
                    if output.count(marker.encode()) >= 2 and _has_rc(output):
                        break
                elif opcode == 1:
                    self._handle_control_message(data)
            except websocket.WebSocketTimeoutException:
                if output.count(marker.encode()) >= 2 and _has_rc(output):
                    break
            except Exception as e:
                # Connection dead (not a timeout): exit the loop now, to
                # avoid busy-spinning until the timeout
                logger.debug("execute recv error: %s", e)
                recv_failed = True
                break

        clean = strip_ansi(output.decode("utf-8", errors="replace"))
        clean = clean.replace("\r\n", "\n").replace("\r", "\n")

        timed_out = time.time() - start >= timeout

        first = clean.find(marker)
        after_first = first + len(marker) if first != -1 else -1
        output_start = clean.find("\n", after_first) if after_first != -1 else -1
        second = clean.find(marker, after_first) if after_first != -1 else -1
        result = ""
        if output_start != -1 and second != -1 and output_start < second:
            result = clean[output_start:second].strip()

        rc = _parse_rc(clean, marker)
        if check and (timed_out or recv_failed or rc not in (None, 0)):
            self._drain()
            if timed_out:
                raise TerminalError(f"Command timed out after {timeout}s")
            if recv_failed:
                raise TerminalError("Connection lost while executing command")
            raise TerminalError(
                f"Command exited with status {rc}", exit_code=rc, output=result,
            )

        if first == -1:
            self._drain()
            return clean.strip()

        if second == -1:
            self._drain()
            if output_start == -1:
                return ""
            return clean[output_start:].strip()

        if output_start == -1 or output_start >= second:
            self._drain()
            return ""
        self._drain()
        return result

    def interactive(self) -> None:
        """Start an interactive PTY relay (Ctrl+] to disconnect), with heartbeat.

        Enters raw terminal mode and relays I/O bidirectionally between
        local stdin/stdout and the remote WebSocket; a background thread
        sends an application-level PING text frame every 30s to defeat
        the KoKo read timeout and the Nginx reverse-proxy timeout.

        Raises:
            TerminalError: stdin is not a TTY.
        """
        console = get_local_console()
        if not console.isatty():
            raise TerminalError("Interactive mode requires a TTY on stdin")

        console.enter_raw()

        def _resize() -> None:
            cols, rows = console.size()
            try:
                self._ws.send(json.dumps({
                    "id": self._ws_id,
                    "type": "TERMINAL_RESIZE",
                    "data": json.dumps({"cols": cols, "rows": rows}),
                }))
            except Exception as e:
                logger.debug("resize send error: %s", e)

        _resize()
        console.on_resize(_resize)

        self._start_heartbeat()

        disconnect_reason = "user disconnect (Ctrl+])"

        try:
            self._ws.settimeout(0)

            running = True
            while running:
                try:
                    stdin_ready = console.wait_stdin(0.05)
                except (ValueError, OSError) as e:
                    disconnect_reason = f"console read error: {e}"
                    logger.debug(disconnect_reason)
                    break

                if stdin_ready:
                    try:
                        data = console.read_stdin()
                    except OSError as e:
                        disconnect_reason = f"stdin read error: {e}"
                        logger.debug(disconnect_reason)
                        running = False
                        break
                    if not data:
                        disconnect_reason = "stdin EOF"
                        running = False
                        break
                    if b"\x1d" in data:  # Ctrl+]
                        disconnect_reason = "user disconnect (Ctrl+])"
                        running = False
                        break
                    try:
                        self._ws.send(json.dumps({
                            "id": self._ws_id,
                            "type": "TERMINAL_DATA",
                            "data": data.decode("utf-8", errors="replace"),
                        }))
                    except Exception as e:
                        disconnect_reason = f"ws send error: {e}"
                        logger.warning("WebSocket send failed: %s", e)
                        running = False
                        break

                try:
                    opcode, ws_data = self._ws.recv_data()
                    if opcode == 2:
                        console.write_stdout(ws_data)
                    elif opcode == 1:
                        msg = json.loads(ws_data.decode("utf-8"))
                        if msg.get("type") == "CLOSE":
                            disconnect_reason = "server sent CLOSE"
                            logger.info("Server sent CLOSE message")
                            running = False
                        elif msg.get("type") == "PING":
                            self._send_pong()
                        else:
                            logger.debug(
                                "control message: type=%s",
                                msg.get("type"),
                            )
                    elif opcode == 8:
                        disconnect_reason = "WebSocket CLOSE frame received"
                        logger.info(disconnect_reason)
                        running = False
                except websocket.WebSocketTimeoutException:
                    continue  # nothing from the remote this round
                except websocket.WebSocketConnectionClosedException as e:
                    disconnect_reason = f"connection closed: {e}"
                    logger.info("WebSocket connection closed: %s", e)
                    running = False
                    break
                except Exception as e:
                    disconnect_reason = f"ws recv error: {e}"
                    logger.warning("WebSocket recv error: %s", e)
                    running = False
                    break
        finally:
            self._stop_heartbeat()
            console.exit_raw()
            logger.info("Interactive session ended: %s", disconnect_reason)

    def close(self) -> None:
        """Close the terminal WebSocket connection."""
        if self._closed:
            return
        self._closed = True
        self._stop_heartbeat()
        try:
            self._ws.send(json.dumps({"type": "CLOSE"}))
            self._ws.close()
        except Exception as e:
            logger.debug("close error (expected): %s", e)

    def _handle_control_message(self, data: bytes) -> None:
        """Handle a text-frame control message from KoKo."""
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        msg_type = msg.get("type", "")
        if msg_type == "PING":
            self._send_pong()
        elif msg_type == "CLOSE":
            logger.info("Server sent CLOSE message")
        else:
            logger.debug("control message: type=%s", msg_type)

    def _send_pong(self) -> None:
        """Reply with an application-level PONG (to KoKo's app-level PING)."""
        try:
            self._ws.send(json.dumps({
                "id": self._ws_id,
                "type": "PONG",
            }))
            logger.debug("sent app-level PONG")
        except Exception as e:
            logger.debug("PONG send failed: %s", e)

    def _start_heartbeat(self) -> None:
        """Start the background heartbeat: app-level PING every HEARTBEAT_INTERVAL.

        Must be a text-frame JSON PING, not a WS protocol-level ping
        (opcode 0x9): the Nginx WS reverse proxy is a transparent TCP
        tunnel, so protocol-level pings never reach KoKo.

        Only called for interactive(); short execute()-only sessions
        are not covered (the KoKo/Nginx idle-kill threshold is >=60s,
        which short commands never hit).
        """
        self._heartbeat_stop.clear()

        def _heartbeat_loop() -> None:
            while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL):
                try:
                    self._ws.send(json.dumps({
                        "id": self._ws_id,
                        "type": "PING",
                    }))
                    logger.debug("sent app-level PING (heartbeat)")
                except Exception as e:
                    logger.debug("heartbeat ping failed: %s", e)
                    break

        self._heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, daemon=True, name="ws-heartbeat",
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        """Stop the heartbeat thread."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)

    def _drain(self, max_seconds: float = 2.0) -> None:
        """Drain residual data from the WebSocket buffer.

        Bounded so a chatty remote can never block the caller forever
        (e.g. the timeout/error paths that call this before raising).
        """
        try:
            self._ws.settimeout(0.3)
            deadline = time.time() + max_seconds
            while time.time() < deadline:
                self._ws.recv_data()
        except Exception:
            pass


def open_ws_terminal(session: JMSSession, asset: AssetInfo) -> WSTerminal:
    """Connect to an asset over WebSocket and return a ready WSTerminal.

    Flow: create connection token -> WebSocket handshake (with session
    cookie) -> read CONNECT message -> TERMINAL_INIT -> wait for the
    shell prompt. On handshake failure, automatically retries once with
    a fresh token. The caller is responsible for close().

    Args:
        session: Authenticated session.
        asset: Resolved asset.

    Returns:
        A ready WSTerminal.

    Raises:
        TerminalError: Connection failed (including after the retry).
    """
    hostname = urlparse(session.base_url).hostname or ""
    # WS scheme follows the HTTP scheme: https -> wss, http -> ws
    ws_scheme = "wss" if session.base_url.startswith("https") else "ws"

    def _create_and_connect() -> websocket.WebSocket:
        """Create a fresh token and open the WebSocket."""
        token = create_connection_token(session, asset)

        ts = int(time.time() * 1000)
        ws_url = (
            f"{ws_scheme}://{hostname}/koko/ws/terminal/"
            f"?disableautohash=false&token={token['id']}&_={ts}"
        )
        # Don't log the full URL (contains token_id) — log hygiene
        logger.debug(
            "WebSocket URL: %s://%s/koko/ws/terminal/", ws_scheme, hostname,
        )

        return websocket.create_connection(
            ws_url,
            header=[
                f"Cookie: jms_sessionid={session.session_id}; "
                f"SESSION_COOKIE_NAME_PREFIX=jms_",
            ],
            origin=session.base_url,
            host=hostname,
            subprotocols=["JMS-KOKO"],
            timeout=WS_CONNECT_TIMEOUT,
        )

    try:
        ws = _create_and_connect()
    except Exception as first_err:
        logger.debug(
            "WebSocket connect failed: %s. "
            "Retrying with a fresh connection token ...",
            first_err,
        )
        try:
            ws = _create_and_connect()
        except Exception as second_err:
            raise TerminalError(
                f"WebSocket connection to {hostname} failed: {second_err}"
            ) from second_err

    logger.debug("WebSocket connected")

    try:
        opcode, data = ws.recv_data()
        connect_msg = json.loads(data.decode("utf-8"))
        ws_id = connect_msg["id"]
        logger.debug("CONNECT message received, session_id=%s", ws_id)

        ws.send(json.dumps({
            "id": ws_id,
            "type": "TERMINAL_INIT",
            "data": json.dumps({"cols": 200, "rows": 50}),
        }))

        terminal = WSTerminal(ws, ws_id)
        terminal.wait_for_prompt()
        return terminal
    except Exception as e:
        ws.close()
        raise TerminalError(f"Failed to initialise WS terminal: {e}") from e


@contextmanager
def connect_ws(session: JMSSession, asset: AssetInfo) -> Iterator[WSTerminal]:
    """Context-manager wrapper of ``open_ws_terminal``: close after yield."""
    terminal = open_ws_terminal(session, asset)
    try:
        yield terminal
    finally:
        terminal.close()


register_backend(
    "ws",
    open_ws_terminal,
    frozenset({TerminalCapability.EXEC, TerminalCapability.INTERACTIVE}),
)
