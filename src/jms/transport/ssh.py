"""SSH terminal backend via the KoKo SSH service (port 2222), token auth.

Compared to the WebSocket backend:
    - no Nginx reverse proxy on the path (no ``proxy_read_timeout`` limit)
    - no JSON frame encoding overhead
    - native PTY (``get_pty()`` + ``invoke_shell()``)
    - direct exit codes (``recv_exit_status()``)
    - SSH transport-level keepalive, no application heartbeat needed
"""

import socket
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

import paramiko

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.transport.base import AbstractTerminal, TerminalCapability
from jms.transport.console import get_local_console
from jms.transport.registry import register_backend
from jms.transport.token import KOKO_SSH_PORT, create_connection_token
from jms.exceptions import TerminalError
from jms.log import logger

# Command exec timeout (seconds)
COMMAND_TIMEOUT: int = 30

# SSH keepalive interval (seconds)
SSH_KEEPALIVE_INTERVAL: int = 30

# TCP connect timeout (seconds) — a DROP-type firewall can hang the OS
# default for ~75s, which would stall the AUTO backend fallback
SSH_CONNECT_TIMEOUT: int = 15


class SSHTerminal(AbstractTerminal):
    """SSH-based terminal session to a JumpServer asset.

    Connects to KoKo's SSH service with paramiko (connection-token auth);
    supports headless command execution (``exec_command``) and interactive
    shells (``invoke_shell``).

    Args:
        transport: An authenticated paramiko Transport.
    """

    capabilities = frozenset({TerminalCapability.EXEC, TerminalCapability.INTERACTIVE})

    def __init__(self, transport: paramiko.Transport) -> None:
        self._transport: paramiko.Transport = transport
        self._closed: bool = False

    @property
    def backend_name(self) -> str:
        """Backend name."""
        return "ssh"

    @property
    def transport(self) -> paramiko.Transport:
        """Underlying Transport (shared with transfer; one token, one conn)."""
        return self._transport

    def execute(
        self, command: str, timeout: int = COMMAND_TIMEOUT, check: bool = False,
    ) -> str:
        """Execute a command over an SSH exec channel.

        Opens a fresh session channel running ``exec_command()`` and reads
        until the channel closes. SSH exec channels have clean start/end
        semantics, so none of the WebSocket marker tricks are needed.

        Args:
            command: Shell command to run.
            timeout: Maximum seconds to wait for output (overall deadline,
                not per-recv).
            check: When true, raise ``TerminalError`` (carrying the remote
                exit code) if the command exits non-zero or times out.

        Returns:
            Command stdout (stripped).

        Raises:
            TerminalError: Session channel could not be opened, or the
                command exited non-zero / timed out when ``check`` is set.
        """
        try:
            channel = self._transport.open_session()
        except Exception as e:
            raise TerminalError(f"Failed to open SSH session: {e}") from e

        try:
            channel.settimeout(timeout)
            channel.exec_command(command)

            stdout_data = b""
            finished = False
            start = time.monotonic()
            while True:
                # channel.settimeout only caps a single recv; a hung command
                # that keeps emitting output would otherwise never time out.
                if time.monotonic() - start > timeout:
                    break
                try:
                    chunk = channel.recv(65536)
                    if not chunk:
                        finished = True
                        break
                    stdout_data += chunk
                except Exception:
                    # recv timeout (hung command) or channel error: return
                    # what we have. recv_exit_status() must NOT be called
                    # here — it blocks forever on a hung command.
                    break

            # stderr is logged only, never mixed into the return value
            stderr_data = b""
            while channel.recv_stderr_ready():
                try:
                    stderr_data += channel.recv_stderr(65536)
                except Exception:
                    break

            if stderr_data:
                logger.debug(
                    "SSH exec stderr: %s",
                    stderr_data.decode("utf-8", errors="replace").strip(),
                )
            if finished:
                exit_code = channel.recv_exit_status()
                if exit_code != 0:
                    logger.debug("SSH exec exit code: %d", exit_code)
                    if check:
                        raise TerminalError(
                            f"Command exited with status {exit_code}",
                            exit_code=exit_code,
                            output=stdout_data.decode(
                                "utf-8", errors="replace",
                            ).strip(),
                        )
            else:
                logger.debug("SSH exec timed out; returning partial output")
                if check:
                    raise TerminalError(
                        f"Command timed out after {timeout}s",
                    )

            return stdout_data.decode("utf-8", errors="replace").strip()
        finally:
            channel.close()

    def interactive(self) -> None:
        """Start an interactive PTY relay (Ctrl+] to disconnect).

        Opens a session channel with a PTY and invokes a shell, then relays
        I/O both ways between the local console and the SSH channel,
        syncing the remote terminal size on resize events.

        Raises:
            TerminalError: stdin is not a TTY, or the PTY shell failed.
        """
        console = get_local_console()
        if not console.isatty():
            raise TerminalError("Interactive mode requires a TTY on stdin")

        console.enter_raw()
        try:
            channel = self._transport.open_session()
        except Exception as e:
            console.exit_raw()
            raise TerminalError(f"Failed to open SSH session: {e}") from e

        cols, rows = console.size()
        try:
            channel.get_pty(term="xterm-256color", width=cols, height=rows)
            channel.invoke_shell()
            channel.settimeout(0)
        except Exception as e:
            channel.close()
            console.exit_raw()
            raise TerminalError(f"Failed to start PTY shell: {e}") from e

        def _resize() -> None:
            c, r = console.size()
            try:
                channel.resize_pty(width=c, height=r)
            except Exception as e:
                logger.debug("pty resize error: %s", e)

        console.on_resize(_resize)
        disconnect_reason = "user disconnect (Ctrl+])"

        try:
            running = True
            while running:
                if channel.closed or channel.exit_status_ready():
                    disconnect_reason = "remote channel closed"
                    break

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
                        channel.sendall(data)
                    except Exception as e:
                        disconnect_reason = f"channel send error: {e}"
                        logger.warning("SSH send failed: %s", e)
                        running = False
                        break

                if channel.recv_ready():
                    try:
                        data = channel.recv(65536)
                        if not data:
                            disconnect_reason = "remote channel closed"
                            running = False
                            break
                        console.write_stdout(data)
                    except Exception as e:
                        disconnect_reason = f"channel recv error: {e}"
                        logger.debug(disconnect_reason)
                        running = False
                        break
        finally:
            channel.close()
            console.exit_raw()
            logger.info("Interactive session ended: %s", disconnect_reason)

    def close(self) -> None:
        """Close the SSH transport."""
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.close()
        except Exception as e:
            logger.debug("SSH close error (expected): %s", e)


def open_koko_transport(
    session: JMSSession,
    asset: AssetInfo,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
) -> paramiko.Transport:
    """Open an authenticated SSH transport to KoKo for the given asset.

    Flow: create a connection token -> TCP connect with timeout -> SSH
    handshake as ``JMS-{token_id}`` / ``token_value`` -> enable keepalive.
    On failure the half-open Transport is closed and the whole flow is
    retried once with a fresh token.

    Shared by the SSH terminal, SFTP transfers and the ssh-pipe bridge.
    SFTP must pass ``protocol="sftp"``, ``connect_method="web_sftp"``
    (see ``create_connection_token`` for why).

    Args:
        session: Authenticated JumpServer session.
        asset: Resolved asset.
        protocol: Token protocol (default ``"ssh"``).
        connect_method: Token connect method (default ``"web_cli"``).

    Returns:
        An authenticated ``paramiko.Transport`` (caller owns its lifetime).

    Raises:
        TerminalError: Connection failed (after one retry).
    """
    hostname = urlparse(session.base_url).hostname or ""

    def _create_and_connect() -> paramiko.Transport:
        """Create a fresh token and open an SSH transport with it."""
        token = create_connection_token(
            session, asset, protocol=protocol, connect_method=connect_method,
        )
        ssh_user = f"JMS-{token['id']}"

        logger.debug(
            "SSH connecting to %s:%d as %s",
            hostname, KOKO_SSH_PORT, ssh_user,
        )

        sock = socket.create_connection(
            (hostname, KOKO_SSH_PORT), timeout=SSH_CONNECT_TIMEOUT,
        )
        transport = paramiko.Transport(sock)
        try:
            transport.connect(username=ssh_user, password=token["value"])
        except Exception:
            transport.close()  # no half-open Transport (fd/packetizer thread)
            raise
        return transport

    try:
        transport = _create_and_connect()
    except Exception as first_err:
        logger.debug(
            "SSH connect failed: %s. Retrying with a fresh token ...",
            first_err,
        )
        try:
            transport = _create_and_connect()
        except Exception as second_err:
            raise TerminalError(
                f"SSH connection to {hostname}:{KOKO_SSH_PORT} "
                f"failed: {second_err}"
            ) from second_err

    logger.debug("SSH authenticated")

    # Transport-level keepalive so idle links survive middleboxes
    transport.set_keepalive(SSH_KEEPALIVE_INTERVAL)

    return transport


def open_ssh_terminal(session: JMSSession, asset: AssetInfo) -> SSHTerminal:
    """Connect to an asset over SSH and return a ready SSHTerminal.

    The caller owns the terminal and must ``close()`` it.

    Args:
        session: Authenticated JumpServer session.
        asset: Resolved asset.

    Returns:
        A ready SSHTerminal.

    Raises:
        TerminalError: Connection failed (after one retry).
    """
    return SSHTerminal(open_koko_transport(session, asset))


@contextmanager
def connect_ssh(session: JMSSession, asset: AssetInfo) -> Iterator[SSHTerminal]:
    """Context-manager wrapper of ``open_ssh_terminal``: auto-close on exit."""
    terminal = open_ssh_terminal(session, asset)
    try:
        yield terminal
    finally:
        terminal.close()


register_backend(
    "ssh",
    open_ssh_terminal,
    frozenset({TerminalCapability.EXEC, TerminalCapability.INTERACTIVE}),
)
