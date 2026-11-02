"""Serve collect.html to machines that reach this one through a tunnel, and save
the dump of every visit.

Two loopback ports, as in the harness: A serves the page, B the cross-origin
iframe. Expose both over https (Tailscale serve/funnel, a Cloudflare tunnel)
and pass B's public origin as ``cross``. A visit to A's ``/?k=<token>`` gets a
run id and is redirected to collect.html, which runs every context on load and
posts the dump back; the server adds the request headers it recorded for that
run and writes one JSON file. Only runs started with the token can submit, once.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .dump import DUMP_VERSION, HEADER_SLOTS, _select_headers
from .server import Origin, Recorder, RecordedRequest, _Handler

MAX_DUMP_BYTES = 16 * 1024 * 1024
RUN_TTL_S = 15 * 60


class Collector:
    def __init__(self, recorder: Recorder, out_dir: Path, token: str, cross: str):
        self.recorder = recorder
        self.out_dir = out_dir
        self.token = token
        self.cross = cross
        self.origin_a = ""
        self.origin_b = ""
        self.runs: dict[str, float] = {}
        self.lock = threading.Lock()

    def start_run(self, token: str) -> str | None:
        if not secrets.compare_digest(token, self.token):
            return None
        now = time.time()
        with self.lock:
            self.runs = {r: t for r, t in self.runs.items() if now - t < RUN_TTL_S}
            run = secrets.token_hex(8)
            self.runs[run] = now
        # Requests outside any live run are only kept long enough to be claimed.
        with self.recorder.lock:
            self.recorder.requests = [r for r in self.recorder.requests if now - r.time < RUN_TTL_S]
        return run

    def _requests(self, run: str) -> list[RecordedRequest]:
        with self.recorder.lock:
            return [r for r in self.recorder.requests if parse_qs(r.query).get("run") == [run]]

    def headers(self, run: str) -> dict[str, dict]:
        """The harness dump's headers section, from this run's requests."""
        reqs = self._requests(run)

        def last(path: str, origin: str) -> dict:
            found = [r for r in reqs if r.path == path and r.origin == origin]
            return _select_headers(found[-1].headers) if found else {"error": "not recorded"}

        # Chrome retries a navigation that lacked a Critical-CH hint, so the
        # first request is the one without hints and the last the one with.
        navs = [r for r in reqs if r.path == "/collect.html" and r.origin == self.origin_a]
        out = {
            "navigation": _select_headers(navs[0].headers) if navs else {"error": "not recorded"},
            "navigation_with_hints": _select_headers(navs[-1].headers) if navs else {"error": "not recorded"},
        }
        for slot, path in HEADER_SLOTS.items():
            if not slot.startswith("navigation"):
                out[slot] = last(path, self.origin_b if slot.endswith("cross") else self.origin_a)
        return out

    def save(self, run: str, body: bytes) -> str:
        with self.lock:
            if self.runs.pop(run, None) is None:
                raise PermissionError("unknown or already submitted run")
        dump = json.loads(body)
        if not isinstance(dump, dict) or dump.get("dump_version") != DUMP_VERSION or not isinstance(dump.get("contexts"), dict):
            raise ValueError("not a dump")
        dump["headers"] = self.headers(run)
        dump["harness"]["run"] = run
        dump["harness"]["received"] = datetime.now(timezone.utc).isoformat()
        h = dump["harness"]
        m = re.search(r" (\d+)", str(h.get("binary_version", "")))
        major = m.group(1) if m else ""
        label = re.sub(r"[^a-z0-9]+", "-", f"{h.get('os_family', 'unknown')}-{h.get('browser', 'unknown')}{major}".lower())
        name = f"{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{run[:6]}.json"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / name).write_text(json.dumps(dump, indent=1, sort_keys=True) + "\n")
        with self.recorder.lock:
            self.recorder.requests = [r for r in self.recorder.requests if parse_qs(r.query).get("run") != [run]]
        return name


class _CollectHandler(_Handler):
    collector: Collector

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        if url.path != "/":
            super().do_GET()
            return
        run = self.collector.start_run(parse_qs(url.query).get("k", [""])[0])
        if run is None:
            self._reply(404, {"error": "not found"})
            return
        self.send_response(302)
        self.send_header("Location", f"/collect.html?run={run}&cross={quote(self.collector.cross, safe=':/')}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if url.path != "/submit" or not 0 < length <= MAX_DUMP_BYTES:
            self._reply(400, {"error": "bad request"})
            return
        body = self.rfile.read(length)
        try:
            name = self.collector.save(parse_qs(url.query).get("run", [""])[0], body)
        except PermissionError as exc:
            self._reply(403, {"error": str(exc)})
            return
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return
        print(f"saved {self.collector.out_dir / name}", flush=True)
        self._reply(200, {"file": name})


def serve(port: int, cross_port: int, cross: str | None, out_dir: Path, token: str | None) -> None:
    recorder = Recorder()
    collector = Collector(recorder, out_dir, token or secrets.token_urlsafe(12), cross or f"http://localhost:{cross_port}")
    a = Origin(recorder, port, _CollectHandler, collector=collector)
    b = Origin(recorder, cross_port, _CollectHandler, collector=collector)
    collector.origin_a, collector.origin_b = a.url, b.url
    a.start()
    b.start()
    print(f"page on {a.url}, cross-origin frame on {b.url} (served to visitors as {collector.cross})")
    print(f"open <public https URL of port {port}>/?k={collector.token} on each machine; dumps go to {out_dir}/")
    print(f"locally: http://127.0.0.1:{port}/?k={collector.token}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        a.stop()
        b.stop()
