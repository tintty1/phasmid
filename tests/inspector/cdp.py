"""A browser launched for the inspector and a CDP client on its browser target.

The client speaks the DevTools protocol over a plain WebSocket (RFC 6455,
stdlib only) to the browser endpoint and never attaches to a page, a worker
or any other target: no session, no Runtime.enable, so a page sees no client.
Pages are opened with Target.createTarget and watched through
Target.getTargets. The launch matches consistency.browser.Launch: its own
process and a fixed non-zero --remote-debugging-port, which, unlike port 0,
a pipe or --enable-automation, does not turn on navigator.webdriver.
"""

from __future__ import annotations

import base64
import collections
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class CdpError(Exception):
    pass


def encode_frame(payload: bytes, opcode: int = 0x1, mask: bytes | None = None) -> bytes:
    """One final, masked client frame (clients must mask, RFC 6455 5.3)."""
    mask = mask if mask is not None else os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def decode_frame(read) -> tuple[bool, int, bytes]:
    """(fin, opcode, payload) of the next frame; `read(n)` returns exactly n bytes."""
    b0, b1 = read(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", read(8))[0]
    mask = read(4) if b1 & 0x80 else None
    payload = read(n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bool(b0 & 0x80), b0 & 0x0F, payload


class WebSocket:
    def __init__(self, url: str, timeout: float = 30):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("connection closed during the WebSocket handshake")
            response += chunk
        head, self._buffer = response.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CdpError(f"WebSocket handshake refused: {head[:200]!r}")

    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                raise CdpError("connection closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def send(self, text: str) -> None:
        self.sock.sendall(encode_frame(text.encode()))

    def recv(self) -> str:
        parts = []
        while True:
            fin, opcode, payload = decode_frame(self._read)
            if opcode == 0x8:
                raise CdpError("browser closed the DevTools connection")
            if opcode == 0x9:
                self.sock.sendall(encode_frame(payload, opcode=0xA))
                continue
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode()

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(b"", opcode=0x8))
        except OSError:
            pass
        self.sock.close()


class BrowserClient:
    """CDP on the browser target: commands and the events they cause."""

    def __init__(self, ws_url: str, timeout: float = 60):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self._id = 0
        self.events: collections.deque = collections.deque()

    def send(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._id += 1
        my_id = self._id
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise CdpError(f"{method}: no answer in {timeout or self.timeout}s")

    def wait_event(self, method: str, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            for msg in list(self.events):
                if msg["method"] == method:
                    self.events.remove(msg)
                    return msg.get("params", {})
            if time.monotonic() > deadline:
                raise CdpError(f"no {method} event in {timeout or self.timeout}s")
            msg = json.loads(self.ws.recv())
            if "method" in msg:
                self.events.append(msg)

    def close(self) -> None:
        self.ws.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class Browser:
    binary: Path
    headless: bool = True
    profile: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    startup_timeout: float = 60.0

    process: subprocess.Popen | None = None
    user_data_dir: Path | None = None
    client: BrowserClient | None = None
    version: dict = field(default_factory=dict)
    _stderr: object = None

    def start(self) -> BrowserClient:
        port = _free_port()
        self.user_data_dir = Path(tempfile.mkdtemp(prefix="phasmid-inspect-"))
        args = [str(self.binary), f"--remote-debugging-port={port}", f"--user-data-dir={self.user_data_dir}",
                "--no-first-run", "--no-default-browser-check"]
        if self.headless:
            args.append("--headless=new")
        if self.profile is not None:
            args.append(f"--phasmid-profile={Path(self.profile).resolve()}")
        args += self.extra_args
        args.append("about:blank")
        self._stderr = open(self.user_data_dir / "stderr.log", "w")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=self._stderr)
        deadline = time.monotonic() + self.startup_timeout
        while True:
            if self.process.poll() is not None:
                raise CdpError(f"browser exited with {self.process.returncode}: {self.stderr_tail()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
                    self.version = json.loads(resp.read().decode())
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            if time.monotonic() > deadline:
                self.stop()
                raise CdpError("DevTools endpoint never answered")
            time.sleep(0.05)
        self.client = BrowserClient(self.version["webSocketDebuggerUrl"])
        return self.client

    def stderr_tail(self, limit: int = 3000) -> str:
        try:
            self._stderr.flush()
            return (self.user_data_dir / "stderr.log").read_text(errors="replace")[-limit:]
        except (OSError, ValueError, AttributeError):
            return ""

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        if self.user_data_dir is not None:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            self.user_data_dir = None
