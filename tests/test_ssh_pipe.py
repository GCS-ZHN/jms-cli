"""Tests for jms.io.ssh_pipe.run_bridge — all I/O mocked, no real server.

Argument parsing (classic vs openrsync form) is a CLI-layer concern and
lives in tests/test_cli.py.
"""

import sys
from typing import Any
from unittest import mock

import pytest

from jms.exceptions import AssetError, ConfigError, TerminalError
from jms.io.ssh_pipe import run_bridge


@pytest.fixture()
def patched() -> Any:
    """Mock every external dependency of run_bridge; yield the mocks."""
    chan = mock.MagicMock()
    chan.recv.return_value = b""        # immediate EOF → relay threads exit
    chan.recv_stderr.return_value = b""
    chan.recv_exit_status.return_value = 0

    transport = mock.MagicMock()
    transport.open_session.return_value = chan

    session = mock.MagicMock()
    session.base_url = "https://jump.example.com"

    asset = mock.MagicMock()
    cfg = mock.MagicMock()

    with mock.patch("jms.io.ssh_pipe.load_config", return_value=cfg) as m_cfg, \
            mock.patch("jms.io.ssh_pipe.JMSSession", return_value=session) as m_sess, \
            mock.patch("jms.io.ssh_pipe.resolve_asset", return_value=asset) as m_res, \
            mock.patch("jms.io.ssh_pipe.open_koko_transport",
                       return_value=transport) as m_tp, \
            mock.patch("jms.io.ssh_pipe.os.read", return_value=b""), \
            mock.patch("jms.io.ssh_pipe.os.write"), \
            mock.patch.object(sys, "stdin", mock.MagicMock(fileno=lambda: 0)), \
            mock.patch.object(sys, "stdout", mock.MagicMock(fileno=lambda: 1)), \
            mock.patch.object(sys, "stderr",
                              mock.MagicMock(fileno=lambda: 2, write=sys.stderr.write)):
        yield {
            "cfg": m_cfg, "session": m_sess, "resolve": m_res,
            "open_transport": m_tp, "tp_inst": transport,
            "chan": chan, "session_inst": session, "asset": asset,
        }


class TestHappyPath:
    def test_transport_and_remote_command(self, patched: Any) -> None:
        code = run_bridge("web-01", "prod", "rsync --server .")
        assert code == 0
        # Transport comes from the shared backend helper with the resolved asset
        patched["open_transport"].assert_called_once_with(
            patched["session_inst"], patched["asset"],
        )
        patched["chan"].exec_command.assert_called_once_with("rsync --server .")
        patched["tp_inst"].close.assert_called_once()

    def test_exit_status_propagated(self, patched: Any) -> None:
        patched["chan"].recv_exit_status.return_value = 23
        assert run_bridge("web-01", "prod", "rsync --server .") == 23

    def test_config_path_forwarded(self, patched: Any) -> None:
        run_bridge("web-01", "prod", "true", config_path="/tmp/cfg.yaml")
        patched["cfg"].assert_called_once_with("/tmp/cfg.yaml")


class TestErrorPaths:
    def test_config_missing_raises(self) -> None:
        with mock.patch("jms.io.ssh_pipe.load_config",
                        side_effect=ConfigError("no config")):
            with pytest.raises(ConfigError):
                run_bridge("web-01", "prod", "true")

    def test_asset_resolve_failure_raises(self, patched: Any) -> None:
        patched["resolve"].side_effect = AssetError("not found")
        with pytest.raises(AssetError):
            run_bridge("ghost", "prod", "true")

    def test_unknown_host_returns_1(self, patched: Any, capsys: Any) -> None:
        patched["session_inst"].base_url = ""
        assert run_bridge("web-01", "prod", "true") == 1
        assert "cannot derive host" in capsys.readouterr().err

    def test_transport_failure_raises(self, patched: Any) -> None:
        patched["open_transport"].side_effect = TerminalError("connect failed")
        with pytest.raises(TerminalError):
            run_bridge("web-01", "prod", "true")


def test_windows_binary_stdio_and_no_sigpipe(
    patched: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SIGPIPE on Windows; binary stdio hook is invoked for all fds."""
    binary_stdio = mock.MagicMock()
    monkeypatch.setattr(
        "jms.io.ssh_pipe._set_windows_binary_stdio", binary_stdio,
    )
    monkeypatch.delattr("signal.SIGPIPE", raising=False)
    # pytest capture re-wraps sys.stdout/stderr after fixture setup, so
    # patch stdio here (inside the test body) to control the fds.
    monkeypatch.setattr(sys, "stdin", mock.MagicMock(fileno=lambda: 0))
    monkeypatch.setattr(sys, "stdout", mock.MagicMock(fileno=lambda: 1))
    monkeypatch.setattr(sys, "stderr", mock.MagicMock(fileno=lambda: 2))

    assert run_bridge("web-01", "prod", "rsync --server .") == 0
    binary_stdio.assert_called_once_with((0, 1, 2))
