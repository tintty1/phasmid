"""Unit tests for the fingerprint inspector collector (no browser)."""

from __future__ import annotations

import io
import json

from inspector.cdp import decode_frame, encode_frame
from inspector.trace import CATEGORY, diff, markdown, parse, short, thread_kind


def _reader(data: bytes):
    buf = io.BytesIO(data)
    return lambda n: buf.read(n)


def test_websocket_frames_round_trip_at_every_length_encoding():
    for size in (0, 125, 126, 65535, 65536):
        payload = bytes(i % 251 for i in range(size))
        fin, opcode, out = decode_frame(_reader(encode_frame(payload, mask=b"\x01\x02\x03\x04")))
        assert (fin, opcode, out) == (True, 1, payload)


def _event(name, args, ts, tid=2):
    return {"cat": CATEGORY, "name": name, "ph": "I", "pid": 1, "tid": tid, "ts": ts, "args": args}


def _trace(port: int, quota: float = 10.0) -> dict:
    caller = [{"url": f"http://127.0.0.1:{port}/main.js", "line": 3, "column": 7}]
    return {"binary": "chrome", "browser": "Chrome/155", "profile": None, "headless": True, "data_loss": False,
            "pages": [{"url": f"http://127.0.0.1:{port}/"}],
            "events": [
                {"ph": "M", "name": "thread_name", "pid": 1, "tid": 2, "args": {"name": "CrRendererMain"}},
                {"ph": "M", "name": "thread_name", "pid": 1, "tid": 3, "args": {"name": "DedicatedWorker thread 7"}},
                {"ph": "M", "name": "process_name", "pid": 1, "args": {"name": "Renderer"}},
                _event("Navigator.hardwareConcurrency.get", {"args": [], "threw": False, "ret": {"type": "number", "value": 8},
                                                             "caller": caller, "world": 0, "main_world": True}, 1),
                _event("StorageManager.estimate", {"args": [], "threw": False, "promise": 42,
                                                   "ret": {"type": "Promise", "id": 42, "state": "pending"},
                                                   "caller": caller, "world": 0, "main_world": True}, 2),
                _event("Promise.settle", {"promise": 42, "state": "fulfilled",
                                          "value": {"type": "object", "constructor": "Object", "size": 1,
                                                    "entries": {"quota": {"type": "number", "value": quota}}}}, 3),
                _event("CSS.mediaFeature", {"feature": "(min-resolution: 1dppx)", "result": "true"}, 4),
                _event("Intl.DateTimeFormat.prototype.resolvedOptions", {"locale": "en-US", "timeZone": "UTC"}, 5, tid=3),
                {"cat": "other", "name": "noise", "ph": "X", "pid": 1, "tid": 2, "ts": 6},
            ]}


def test_parse_joins_promise_settlements_and_sorts_out_tier_3():
    p = parse(_trace(1111))
    assert [c["api"] for c in p["calls"]] == ["Navigator.hardwareConcurrency.get", "StorageManager.estimate"]
    estimate = p["calls"][1]
    assert estimate["settled"]["value"]["entries"]["quota"]["value"] == 10.0
    assert p["calls"][0]["thread"] == "CrRendererMain" and p["calls"][0]["process"] == "Renderer"
    assert p["css"][0]["result"] == "true" and p["intl"][0]["fields"]["timeZone"] == "UTC"
    assert p["event_count"] == 5


def test_markdown_lists_api_result_and_caller():
    trace = _trace(1111)
    md = markdown(trace, parse(trace))
    assert "`Navigator.hardwareConcurrency.get`" in md and "| 8 |" in md
    assert "fulfilled: {quota: 10.0}" in md and "main.js:3:7" in md
    assert "Intl.DateTimeFormat.prototype.resolvedOptions" in md


def test_diff_ignores_ports_and_reports_changed_results():
    assert diff(parse(_trace(1111)), parse(_trace(2222))) == []
    lines = diff(parse(_trace(1111)), parse(_trace(2222, quota=20.0)))
    assert len(lines) == 1 and "StorageManager.estimate" in lines[0] and "/main.js" in lines[0]


def test_short_values():
    assert short({"type": "string", "value": "x" * 50, "length": 5000, "hash": "ab"}).endswith("(len 5000, fnv ab)")
    assert short({"type": "wrapper", "interface": "WebGLRenderingContext"}) == "<WebGLRenderingContext>"
    assert short({"type": "Float32Array", "byteLength": 8, "length": 2, "hash": "cd", "items": [1, 1024]}) == "Float32Array[2](1, 1024) fnv cd"
    assert thread_kind("DedicatedWorker thread 7") == "DedicatedWorker thread"
    assert json.loads(json.dumps(short({"type": "undefined"}))) == "undefined"
