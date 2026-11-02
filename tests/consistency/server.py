"""Two loopback origins that serve the probe pages and record request headers.

Origin A hosts the main document, its workers and the same-origin iframe.
Origin B hosts the cross-origin iframe. Two ports on 127.0.0.1 are two
origins, so no DNS tricks are needed, and 127.0.0.1 is a potentially
trustworthy origin, so secure-context APIs (crypto.subtle, userAgentData,
deviceMemory, service workers, Client Hints) all work.

Every request is recorded with its lowercased headers so the harness can
compare what JavaScript reports with what the network layer sent, per context.
"""

from __future__ import annotations

import http.server
import mimetypes
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

WEB_ROOT = Path(__file__).parent / "web"

# High-entropy Client Hints the harness asks for. Chrome stores the Accept-CH
# grant per origin after a top-level navigation; the harness navigates twice
# so the second navigation carries the full set.
ACCEPT_CH = ", ".join(
    [
        "Sec-CH-UA-Arch",
        "Sec-CH-UA-Bitness",
        "Sec-CH-UA-Full-Version",
        "Sec-CH-UA-Full-Version-List",
        "Sec-CH-UA-Model",
        "Sec-CH-UA-Platform-Version",
        "Sec-CH-UA-WoW64",
        "Sec-CH-UA-Form-Factors",
        "Sec-CH-Device-Memory",
        "Device-Memory",
        "Sec-CH-DPR",
        "DPR",
        "Sec-CH-Viewport-Width",
        "Viewport-Width",
        "Sec-CH-Viewport-Height",
        "Sec-CH-Prefers-Color-Scheme",
        "Sec-CH-Prefers-Reduced-Motion",
        "Sec-CH-Prefers-Reduced-Transparency",
        "Downlink",
        "RTT",
        "ECT",
        "Save-Data",
    ]
)

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/html", ".html")


@dataclass
class RecordedRequest:
    origin: str
    path: str
    headers: dict[str, str]
    time: float
    query: str = ""


@dataclass
class Recorder:
    requests: list[RecordedRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, origin: str, path: str, headers: dict[str, str], query: str = "") -> None:
        with self.lock:
            self.requests.append(RecordedRequest(origin, path, headers, time.time(), query))

    def find(self, path: str, origin: str | None = None) -> RecordedRequest | None:
        """Most recent request for a path, optionally restricted to an origin."""
        with self.lock:
            for req in reversed(self.requests):
                if req.path == path and (origin is None or req.origin == origin):
                    return req
        return None


class _Handler(http.server.SimpleHTTPRequestHandler):
    recorder: Recorder
    origin: str

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format, *args):  # noqa: A002 (silence per-request logging)
        pass

    def do_GET(self):  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        path = url.path
        self.recorder.add(self.origin, path, {k.lower(): v for k, v in self.headers.items()}, url.query)
        if path.startswith("/h/"):
            # Header-capture endpoints: the body is irrelevant, only the request matters.
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # SimpleHTTPRequestHandler calls end_headers() itself for files; add our
        # headers here so static files get them too.
        if not getattr(self, "_phasmid_headers_sent", False):
            self._common_headers()
        super().end_headers()

    def _common_headers(self):
        self._phasmid_headers_sent = True
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-CH", ACCEPT_CH)
        self.send_header("Critical-CH", "Sec-CH-UA-Platform-Version")
        self.send_header("Service-Worker-Allowed", "/")
        # Let the cross-origin frame fetch from its own origin without CORS noise;
        # nothing here is cross-origin-fetched, but keep the probe simple.
        self.send_header("Access-Control-Allow-Origin", "*")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Origin:
    """One HTTP origin on 127.0.0.1 serving WEB_ROOT, recording every request."""

    def __init__(self, recorder: Recorder, port: int | None = None, handler_base: type = _Handler, **handler_attrs):
        self.port = port or _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        handler = type("Handler", (handler_base,), {"recorder": recorder, "origin": self.url, **handler_attrs})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "Origin":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class Servers:
    """Origin A (main) and origin B (cross-origin frame) sharing one recorder."""

    def __init__(self):
        self.recorder = Recorder()
        self.a = Origin(self.recorder)
        self.b = Origin(self.recorder)

    def __enter__(self) -> "Servers":
        self.a.start()
        self.b.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.a.stop()
        self.b.stop()
