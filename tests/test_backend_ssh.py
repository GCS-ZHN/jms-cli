"""Tests for jms.transport.ssh — paramiko/socket fully mocked, no real server."""

import socket
from unittest.mock import MagicMock

import pytest

from jms.core.resources import AssetInfo
from jms.transport.ssh import SSHTerminal, connect_ssh, open_koko_transport
from jms.exceptions import TerminalError

ASSET = AssetInfo(
    id="asset-uuid-1", name="web1", address="10.0.0.1",
    account="@USER", protocol="ssh",
)


def _session() -> MagicMock:
    session = MagicMock()
    session.base_url = "https://jump.example.com"
    session.api_post.return_value = {"id": "tok-id", "value": "tok-val"}
    return session


def _mock_net(monkeypatch: pytest.MonkeyPatch, transports: list) -> None:
    """Mock socket.create_connection + paramiko.Transport."""
    monkeypatch.setattr(
        "jms.transport.ssh.socket.create_connection", MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "jms.transport.ssh.paramiko.Transport", MagicMock(side_effect=transports),
    )


def test_koko_transport_token_credential_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH user is JMS-{token_id}, password is the token value."""
    transport = MagicMock()
    _mock_net(monkeypatch, [transport])

    result = open_koko_transport(_session(), ASSET)

    assert result is transport
    transport.connect.assert_called_once_with(
        username="JMS-tok-id", password="tok-val",
    )
    transport.close.assert_not_called()


def test_koko_transport_retries_with_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handshake failure closes the half-open Transport, retries with a new token."""
    t1, t2 = MagicMock(), MagicMock()
    t1.connect.side_effect = Exception("handshake boom")
    _mock_net(monkeypatch, [t1, t2])
    session = _session()

    result = open_koko_transport(session, ASSET)

    assert result is t2
    assert session.api_post.call_count == 2  # fresh token on retry
    t1.close.assert_called_once()  # failed half-open Transport closed
    t2.close.assert_not_called()


def test_koko_transport_retry_also_fails_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both attempts failing raises TerminalError; both half-open Transports closed."""
    t1, t2 = MagicMock(), MagicMock()
    t1.connect.side_effect = Exception("boom1")
    t2.connect.side_effect = Exception("boom2")
    _mock_net(monkeypatch, [t1, t2])

    with pytest.raises(TerminalError, match="boom2"):
        open_koko_transport(_session(), ASSET)

    t1.close.assert_called_once()
    t2.close.assert_called_once()


def test_connect_ssh_returns_terminal_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MagicMock()
    _mock_net(monkeypatch, [transport])

    with connect_ssh(_session(), ASSET) as term:
        assert isinstance(term, SSHTerminal)
        assert term.backend_name == "ssh"

    transport.close.assert_called_once()  # closed on context exit


def test_execute_reads_stdout_until_channel_close() -> None:
    transport = MagicMock()
    channel = transport.open_session.return_value
    channel.recv.side_effect = [b"hello ", b"world\n", b""]
    channel.recv_stderr_ready.return_value = False
    channel.recv_exit_status.return_value = 0

    term = SSHTerminal(transport)
    out = term.execute("echo hello world")

    assert out == "hello world"
    channel.exec_command.assert_called_once_with("echo hello world")
    channel.close.assert_called_once()


def test_execute_timeout_returns_partial_without_blocking() -> None:
    """Hung command: recv timeout returns partial output, never blocks on exit status."""
    transport = MagicMock()
    channel = transport.open_session.return_value
    channel.recv.side_effect = [b"partial\n", socket.timeout("timed out")]
    channel.recv_stderr_ready.return_value = False

    term = SSHTerminal(transport)
    out = term.execute("sleep 999", timeout=1)

    assert out == "partial"
    channel.recv_exit_status.assert_not_called()  # hung command must not wait
    channel.close.assert_called_once()


def test_execute_overall_deadline_with_chatty_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command emitting output forever still hits the overall deadline."""
    transport = MagicMock()
    channel = transport.open_session.return_value
    channel.recv_stderr_ready.return_value = False

    clock = [0.0]
    monkeypatch.setattr("jms.transport.ssh.time.monotonic", lambda: clock[0])

    def _chatty_recv(_size: int) -> bytes:
        clock[0] += 10.0  # each recv yields output instantly, advancing time
        return b"spam\n"

    channel.recv.side_effect = _chatty_recv

    term = SSHTerminal(transport)
    out = term.execute("yes spam", timeout=1)

    assert out == "spam"
    channel.recv_exit_status.assert_not_called()  # deadline hit, no clean EOF
    channel.close.assert_called_once()


def test_execute_open_session_failure_raises() -> None:
    transport = MagicMock()
    transport.open_session.side_effect = Exception("no channel")

    with pytest.raises(TerminalError, match="open SSH session"):
        SSHTerminal(transport).execute("ls")


def test_execute_check_raises_on_nonzero_exit() -> None:
    """check=True propagates a non-zero remote exit status via TerminalError."""
    transport = MagicMock()
    channel = transport.open_session.return_value
    channel.recv.side_effect = [b"boom\n", b""]
    channel.recv_stderr_ready.return_value = False
    channel.recv_exit_status.return_value = 42

    term = SSHTerminal(transport)
    with pytest.raises(TerminalError, match="status 42") as excinfo:
        term.execute("false", check=True)
    assert excinfo.value.exit_code == 42


def test_execute_check_passes_on_zero_exit() -> None:
    """check=True returns output normally for a successful command."""
    transport = MagicMock()
    channel = transport.open_session.return_value
    channel.recv.side_effect = [b"ok\n", b""]
    channel.recv_stderr_ready.return_value = False
    channel.recv_exit_status.return_value = 0

    term = SSHTerminal(transport)
    assert term.execute("true", check=True) == "ok"


def test_execute_check_raises_on_timeout() -> None:
    """check=True turns a hung command into TerminalError, not partial output."""
    transport = MagicMock()
    channel = transport.open_session.return_value
    channel.recv.side_effect = [b"partial\n", socket.timeout("timed out")]
    channel.recv_stderr_ready.return_value = False

    term = SSHTerminal(transport)
    with pytest.raises(TerminalError, match="timed out"):
        term.execute("sleep 999", timeout=1, check=True)


def test_interactive_requires_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-TTY stdin raises TerminalError instead of a raw termios.error."""
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))

    with pytest.raises(TerminalError, match="requires a TTY"):
        SSHTerminal(MagicMock()).interactive()


class FakeConsole:
    """In-memory LocalConsole stand-in for relay-loop tests."""

    def __init__(self, stdin: list[bytes] | None = None) -> None:
        self.queue = list(stdin or [])
        self.stdout: list[bytes] = []
        self.raw = False
        self.resize_cb = None

    def isatty(self) -> bool:
        return True

    def enter_raw(self) -> None:
        self.raw = True

    def exit_raw(self) -> None:
        self.raw = False

    def wait_stdin(self, timeout: float) -> bool:
        return bool(self.queue)

    def read_stdin(self) -> bytes:
        return self.queue.pop(0) if self.queue else b""

    def write_stdout(self, data: bytes) -> None:
        self.stdout.append(bytes(data))

    def size(self) -> tuple[int, int]:
        return (120, 40)

    def on_resize(self, callback) -> None:
        self.resize_cb = callback


def _interactive_channel() -> MagicMock:
    channel = MagicMock()
    channel.closed = False
    channel.exit_status_ready.return_value = False
    channel.recv_ready.return_value = False
    return channel


def _run_interactive(
    console: FakeConsole, channel: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MagicMock()
    transport.open_session.return_value = channel
    monkeypatch.setattr("jms.transport.ssh.get_local_console", lambda: console)
    SSHTerminal(transport).interactive()


def test_interactive_relays_stdin_and_ctrl_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdin bytes are sent; Ctrl+] disconnects and raw mode is restored."""
    channel = _interactive_channel()
    console = FakeConsole([b"whoami\r", b"\x1d"])
    _run_interactive(console, channel, monkeypatch)

    assert channel.get_pty.call_args.kwargs == {
        "term": "xterm-256color", "width": 120, "height": 40,
    }
    channel.invoke_shell.assert_called_once()
    sent = [call.args[0] for call in channel.sendall.call_args_list]
    assert sent == [b"whoami\r"]
    assert console.raw is False  # exit_raw ran in the finally block
    channel.close.assert_called()


def test_interactive_relays_remote_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote bytes reach the console; channel EOF disconnects."""
    channel = _interactive_channel()
    channel.recv_ready.side_effect = [True, True]
    channel.recv.side_effect = [b"hello\r\n", b""]
    console = FakeConsole([])
    _run_interactive(console, channel, monkeypatch)

    assert console.stdout == [b"hello\r\n"]
    channel.close.assert_called()


def test_interactive_stdin_eof_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty stdin read (EOF) disconnects without sending anything."""
    channel = _interactive_channel()
    console = FakeConsole([b""])
    _run_interactive(console, channel, monkeypatch)

    channel.sendall.assert_not_called()
    channel.close.assert_called()


def test_interactive_remote_close_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote channel closing is detected while idle."""
    channel = _interactive_channel()
    channel.exit_status_ready.return_value = True
    console = FakeConsole([])
    _run_interactive(console, channel, monkeypatch)

    channel.close.assert_called()


def test_interactive_resize_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console resize events resize the remote PTY."""
    channel = _interactive_channel()
    console = FakeConsole([b"x", b"\x1d"])
    _run_interactive(console, channel, monkeypatch)

    assert console.resize_cb is not None
    console.resize_cb()
    channel.resize_pty.assert_called_once_with(width=120, height=40)
