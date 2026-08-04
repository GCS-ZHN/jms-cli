# -*- coding: utf-8 -*-
"""Tests for jms.transport.ws — websocket.create_connection 全部 mock。"""

import json
import os
import re
import shutil
import subprocess
import time
from unittest.mock import MagicMock

import pytest
import websocket

from jms.core.resources import AssetInfo
from jms.transport.ws import WSTerminal, connect_ws
from jms.exceptions import TerminalError

ASSET = AssetInfo(
    id="asset-uuid-1", name="web1", address="10.0.0.1",
    account="@USER", protocol="ssh",
)


def _usable_bash() -> str | None:
    """Return a working POSIX bash, preferring Git Bash on Windows.

    ``bash`` on the Windows PATH may be the WSL launcher, which fails
    without an installed distribution, so candidates are validated by
    actually running them.
    """
    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ])
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for cand in candidates:
        try:
            proc = subprocess.run(
                [cand, "-c", "exit 0"], capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return cand
    return None


_USABLE_BASH = _usable_bash()


class FakeWebSocket:
    """内存版 WebSocket：帧队列驱动，记录所有 send。"""

    def __init__(self, frames: list | None = None) -> None:
        self.sent: list[str] = []
        self.frames: list = list(frames or [])
        self.closed = False
        self.sock = None

    def send(self, data: str) -> None:
        self.sent.append(data)

    def settimeout(self, _t: float) -> None:
        pass

    def recv_data(self):
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        raise websocket.WebSocketTimeoutException("timeout")

    def close(self) -> None:
        self.closed = True


def _session(base_url: str = "https://jump.example.com") -> MagicMock:
    session = MagicMock()
    session.base_url = base_url
    session.session_id = "sid-abc"
    session.api_post.return_value = {"id": "tok-id", "value": "tok-val"}
    return session


def _connect_frames() -> list:
    """CONNECT 文本帧 + 一个带提示符的二进制帧（wait_for_prompt 立即就绪）。"""
    return [
        (1, json.dumps({"id": "ws-uuid-1"}).encode()),
        (2, b"user@web1:~$ "),
    ]


def _patch_create(monkeypatch: pytest.MonkeyPatch, ws) -> MagicMock:
    factory = MagicMock(return_value=ws)
    monkeypatch.setattr("jms.transport.ws.websocket.create_connection", factory)
    return factory


def test_connect_ws_url_scheme_wss(monkeypatch: pytest.MonkeyPatch) -> None:
    """https -> wss，路径必须 /koko/ws/terminal/，带 session cookie。"""
    ws = FakeWebSocket(_connect_frames())
    factory = _patch_create(monkeypatch, ws)

    with connect_ws(_session("https://jump.example.com"), ASSET) as term:
        assert isinstance(term, WSTerminal)
        assert term.ws_id == "ws-uuid-1"
        assert term.backend_name == "websocket"

    url = factory.call_args.args[0]
    assert url.startswith("wss://jump.example.com/koko/ws/terminal/")
    assert "token=tok-id" in url

    kwargs = factory.call_args.kwargs
    assert any("jms_sessionid=sid-abc" in h for h in kwargs["header"])
    assert kwargs["origin"] == "https://jump.example.com"
    assert kwargs["subprotocols"] == ["JMS-KOKO"]

    # CONNECT 后应发送 TERMINAL_INIT
    init = json.loads(ws.sent[0])
    assert init["type"] == "TERMINAL_INIT"
    assert init["id"] == "ws-uuid-1"

    # 退出上下文：发送 CLOSE 并关闭
    assert json.loads(ws.sent[-1])["type"] == "CLOSE"
    assert ws.closed


def test_connect_ws_url_scheme_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    """http -> ws（scheme 随 HTTP 降级）。"""
    ws = FakeWebSocket(_connect_frames())
    factory = _patch_create(monkeypatch, ws)

    with connect_ws(_session("http://jump.internal"), ASSET):
        pass

    url = factory.call_args.args[0]
    assert url.startswith("ws://jump.internal/koko/ws/terminal/")
    assert factory.call_args.kwargs["origin"] == "http://jump.internal"


def test_connect_ws_retries_with_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """握手失败自动重建令牌重试一次。"""
    ws = FakeWebSocket(_connect_frames())
    factory = MagicMock(side_effect=[Exception("handshake boom"), ws])
    monkeypatch.setattr("jms.transport.ws.websocket.create_connection", factory)
    session = _session()

    with connect_ws(session, ASSET):
        pass

    assert factory.call_count == 2
    assert session.api_post.call_count == 2


def test_connect_ws_retry_also_fails_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(side_effect=Exception("boom"))
    monkeypatch.setattr("jms.transport.ws.websocket.create_connection", factory)

    with pytest.raises(TerminalError, match="WebSocket connection"):
        with connect_ws(_session(), ASSET):
            pass


class _MarkerWebSocket(FakeWebSocket):
    """收到 TERMINAL_DATA 后自动按终端行为回放帧（回显 + 输出）。"""

    def __init__(
        self, output: str, echo_marker_twice: bool = True, rc: int = 0,
    ) -> None:
        super().__init__()
        self._output = output
        self._twice = echo_marker_twice
        self._rc = rc

    def send(self, data: str) -> None:
        super().send(data)
        msg = json.loads(data)
        if msg.get("type") != "TERMINAL_DATA":
            return
        m = re.search(r"__JMSDONE_\d+__", msg["data"])
        assert m is not None
        marker = m.group(0)
        # 帧 1：命令回显（含 marker 第 1 次）
        self.frames.append((2, msg["data"].encode() + b"\r\n"))
        # 帧 2：命令输出；echo_marker_twice 时再带 echo 输出（marker 第 2 次）
        frame2 = self._output.encode() + b"\r\n"
        if self._twice:
            frame2 += marker.encode() + b"\r\n"
            frame2 += f"__JMSRC:{self._rc}__\r\n".encode()
        self.frames.append((2, frame2))
        # 帧 3：提示符残余，execute 返回后应被 _drain 排空
        self.frames.append((2, b"user@web1:~$ "))


def test_execute_marker_parsing_and_drain() -> None:
    """marker 出现 2 次（回显 + echo）才返回，输出取两次之间，返回后清残余。"""
    ws = _MarkerWebSocket("gcszhn")
    term = WSTerminal(ws, "ws-uuid-1")

    out = term.execute("whoami")

    assert out == "gcszhn"
    # 命令以文本帧 JSON 发送，data 以 \r 结尾
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "TERMINAL_DATA"
    assert sent["id"] == "ws-uuid-1"
    assert sent["data"].startswith("whoami; __rc=$?; echo __JMSDONE_")
    assert sent["data"].endswith("echo __JMSRC:${__rc}__\r")
    # execute 返回后残余帧（提示符）已被 _drain 排空
    assert ws.frames == []


def test_execute_waits_for_second_marker() -> None:
    """marker 只出现 1 次时不提前返回，等到超时并返回已有输出。"""
    ws = _MarkerWebSocket("partial-out", echo_marker_twice=False)
    term = WSTerminal(ws, "ws-uuid-1")

    start = time.time()
    out = term.execute("whoami", timeout=1)
    elapsed = time.time() - start

    assert elapsed >= 1.0  # 等到超时而不是看到第一个 marker 就返回
    assert "partial-out" in out
    assert ws.frames == []


def test_execute_skips_control_frames() -> None:
    """execute 期间收到 PING 控制帧（opcode 1）应回 PONG 且不算输出。"""
    ws = _MarkerWebSocket("ok")
    orig_send = ws.send

    def send(data: str) -> None:
        was_empty = not any(
            json.loads(s).get("type") == "TERMINAL_DATA" for s in ws.sent
        )
        orig_send(data)
        msg = json.loads(data)
        if msg.get("type") == "TERMINAL_DATA" and was_empty:
            ws.frames.insert(1, (1, b'{"type": "PING"}'))

    ws.send = send
    term = WSTerminal(ws, "ws-uuid-1")

    assert term.execute("echo ok") == "ok"
    pong = [json.loads(s) for s in ws.sent if json.loads(s).get("type") == "PONG"]
    assert pong and pong[0]["id"] == "ws-uuid-1"


def test_execute_check_raises_on_nonzero_exit() -> None:
    """check=True 从 __JMSRC 标记解析非零退出码并抛 TerminalError。"""
    ws = _MarkerWebSocket("boom", rc=42)
    term = WSTerminal(ws, "ws-uuid-1")

    with pytest.raises(TerminalError, match="status 42") as excinfo:
        term.execute("false", check=True)
    assert excinfo.value.exit_code == 42


def test_execute_check_passes_on_zero_exit() -> None:
    """check=True 正常返回零退出码命令的输出。"""
    ws = _MarkerWebSocket("ok")
    term = WSTerminal(ws, "ws-uuid-1")

    assert term.execute("whoami", check=True) == "ok"
    assert ws.frames == []


def test_heartbeat_sends_app_level_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳是文本帧 JSON PING（不是 WS opcode 0x9）。"""
    monkeypatch.setattr("jms.transport.ws.HEARTBEAT_INTERVAL", 0.05)
    ws = FakeWebSocket()
    term = WSTerminal(ws, "ws-uuid-1")

    term._start_heartbeat()
    time.sleep(0.2)
    term._stop_heartbeat()

    pings = [json.loads(s) for s in ws.sent if json.loads(s).get("type") == "PING"]
    assert len(pings) >= 2
    assert pings[0]["id"] == "ws-uuid-1"


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


def _ws_interactive(
    ws: FakeWebSocket, console: FakeConsole,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jms.transport.ws.get_local_console", lambda: console)
    WSTerminal(ws, "ws-uuid-1").interactive()


def test_interactive_relays_stdin_and_ctrl_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdin bytes become TERMINAL_DATA; Ctrl+] disconnects."""
    ws = FakeWebSocket()
    console = FakeConsole([b"ls\r", b"\x1d"])
    _ws_interactive(ws, console, monkeypatch)

    sent = [json.loads(s) for s in ws.sent]
    data_msgs = [m for m in sent if m.get("type") == "TERMINAL_DATA"]
    assert [m["data"] for m in data_msgs] == ["ls\r"]
    assert console.raw is False  # exit_raw ran in the finally block


def test_interactive_relays_remote_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary frames reach the console; a CLOSE frame ends the relay."""
    ws = FakeWebSocket([(2, b"hello\r\n"), (8, b"")])
    console = FakeConsole([])
    _ws_interactive(ws, console, monkeypatch)

    assert console.stdout == [b"hello\r\n"]


def test_interactive_server_close_message_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server-sent CLOSE text message ends the relay."""
    ws = FakeWebSocket([(1, json.dumps({"type": "CLOSE"}).encode())])
    console = FakeConsole([])
    _ws_interactive(ws, console, monkeypatch)

    assert console.stdout == []


def test_interactive_resize_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console resize events send TERMINAL_RESIZE with the new size."""
    ws = FakeWebSocket()
    console = FakeConsole([b"x", b"\x1d"])
    _ws_interactive(ws, console, monkeypatch)

    assert console.resize_cb is not None
    console.resize_cb()
    sent = [json.loads(s) for s in ws.sent]
    resizes = [m for m in sent if m.get("type") == "TERMINAL_RESIZE"]
    assert len(resizes) == 2  # initial + callback
    assert json.loads(resizes[-1]["data"]) == {"cols": 120, "rows": 40}


def test_close_sends_close_frame_and_is_idempotent() -> None:
    ws = FakeWebSocket()
    term = WSTerminal(ws, "ws-uuid-1")

    term.close()
    term.close()

    assert ws.closed
    close_msgs = [
        json.loads(s) for s in ws.sent if json.loads(s).get("type") == "CLOSE"
    ]
    assert len(close_msgs) == 1


def test_execute_check_raises_on_connection_lost() -> None:
    """连接中途断开 + check=True → TerminalError("Connection lost")。"""
    ws = FakeWebSocket([(2, b"partial\r\n"), Exception("boom")])
    term = WSTerminal(ws, "ws-uuid-1")

    with pytest.raises(TerminalError, match="Connection lost"):
        term.execute("whoami", timeout=5, check=True)


def test_execute_check_raises_on_timeout() -> None:
    """marker 一直没到 + check=True → TerminalError("timed out")。"""
    ws = _MarkerWebSocket("partial-out", echo_marker_twice=False)
    term = WSTerminal(ws, "ws-uuid-1")

    with pytest.raises(TerminalError, match="timed out"):
        term.execute("whoami", timeout=1, check=True)


@pytest.mark.skipif(_USABLE_BASH is None, reason="requires a working bash")
def test_execute_rc_marker_expands_in_real_shell() -> None:
    """生成的 full_cmd 在真实 shell 里必须产出 __JMSRC 数字标记（回归 $__rc__ bug）。"""
    marker = "__JMSDONE_123__"
    full_cmd = f"false; __rc=$?; echo {marker}; echo __JMSRC:${{__rc}}__"
    proc = subprocess.run(
        [_USABLE_BASH, "-c", full_cmd], capture_output=True, text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert re.search(r"__JMSRC:\d+__", proc.stdout) is not None
    assert "__JMSRC:1__" in proc.stdout  # false → rc=1
