"""Collect, read, summarize and diff fingerprint traces.

devtools/fingerprint-trace.patch emits, in category CATEGORY, one instant
event per call of an allowlisted Web API (named "Interface.member",
"Interface.member.get" or ".set", "Interface.constructor") and the events
"Promise.settle", "CSS.mediaFeature" and "CSS.fontFamily";
devtools/fingerprint-trace-intl.patch emits "Intl.*.prototype.resolvedOptions".
Tracing starts on the browser target before the first page opens, so every
renderer, worker and service-worker process launched afterwards traces from
its first instruction.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from .cdp import Browser, BrowserClient

CATEGORY = "disabled-by-default-phasmid.fingerprint"
BUFFER_KB = 512 * 1024


def start_tracing(client: BrowserClient) -> None:
    client.send("Tracing.start", {
        "traceConfig": {"recordMode": "recordAsMuchAsPossible", "traceBufferSizeInKb": BUFFER_KB,
                        "includedCategories": [CATEGORY, "__metadata"]},
        "transferMode": "ReturnAsStream", "streamFormat": "json",
    })


def stop_tracing(client: BrowserClient, timeout: float = 120) -> tuple[list[dict], bool]:
    """Every event of the session, and whether the browser reported data loss."""
    client.send("Tracing.end")
    done = client.wait_event("Tracing.tracingComplete", timeout=timeout)
    chunks = []
    while True:
        r = client.send("IO.read", {"handle": done["stream"], "size": 1 << 20})
        chunks.append(base64.b64decode(r["data"]).decode() if r.get("base64Encoded") else r["data"])
        if r.get("eof"):
            break
    client.send("IO.close", {"handle": done["stream"]})
    text = "".join(chunks)
    data = json.loads(text) if text.strip() else {}
    events = data.get("traceEvents", data if isinstance(data, list) else [])
    return events, bool(done.get("dataLossOccurred"))


def open_page(client: BrowserClient, url: str, until_title: str | None, wait: float) -> dict:
    """Open `url` in a new tab without attaching to it; wait until its title is
    `until_title` (then a short grace for late events) or `wait` seconds."""
    target = client.send("Target.createTarget", {"url": url})["targetId"]
    deadline = time.monotonic() + wait
    title = ""
    while time.monotonic() < deadline:
        for info in client.send("Target.getTargets")["targetInfos"]:
            if info["targetId"] == target:
                title = info.get("title", "")
        if until_title and title == until_title:
            time.sleep(1.0)
            return {"url": url, "title": title, "finished": True}
        time.sleep(0.25)
    return {"url": url, "title": title, "finished": not until_title}


def collect(binary: Path, urls: list[str], *, profile: Path | None = None, headless: bool = True,
            until_title: str | None = None, wait: float = 20, trace: bool = True, extra_args: list[str] | None = None) -> dict:
    """Launch, trace (unless trace=False), open each URL, collect."""
    browser = Browser(binary=binary, headless=headless, profile=profile, extra_args=extra_args or [])
    client = browser.start()
    try:
        if trace:
            start_tracing(client)
        pages = [open_page(client, url, until_title, wait) for url in urls]
        events, data_loss = stop_tracing(client) if trace else ([], False)
    finally:
        version = browser.version.get("Browser", "")
        browser.stop()
    return {"binary": str(binary), "browser": version, "profile": str(profile) if profile else None,
            "headless": headless, "pages": pages, "data_loss": data_loss, "events": events}


# ---------------------------------------------------------------- reading

def thread_names(events: list[dict]) -> dict[tuple[int, int], str]:
    out = {}
    for e in events:
        if e.get("ph") == "M" and e.get("name") == "thread_name":
            out[(e.get("pid"), e.get("tid"))] = e.get("args", {}).get("name", "")
    return out


def process_names(events: list[dict]) -> dict[int, str]:
    """Process name, with Chrome's process labels (the site) appended."""
    names, labels = {}, {}
    for e in events:
        if e.get("ph") != "M":
            continue
        if e.get("name") == "process_name":
            names[e.get("pid")] = e.get("args", {}).get("name", "")
        elif e.get("name") == "process_labels":
            labels[e.get("pid")] = e.get("args", {}).get("labels", "")
    return {pid: f"{names.get(pid, '')} {labels[pid]}".strip() if labels.get(pid) else names.get(pid, "")
            for pid in set(names) | set(labels)}


def thread_kind(name: str) -> str:
    """CrRendererMain, DedicatedWorker thread, ... without per-thread numbers."""
    return re.sub(r"[\s/#-]*\d+$", "", name or "?").strip() or "?"


def parse(trace: dict) -> dict:
    """Calls with their promise settlements joined, plus the tier-3 events."""
    events = [e for e in trace["events"] if e.get("cat") == CATEGORY]
    threads = thread_names(trace["events"])
    processes = process_names(trace["events"])
    settles = {}
    for e in events:
        if e.get("name") == "Promise.settle":
            settles[(e["pid"], e["tid"], e["args"].get("promise"))] = e["args"]
    calls, css, fonts, intl = [], [], [], []
    for e in sorted(events, key=lambda e: e.get("ts", 0)):
        name, args = e.get("name", ""), e.get("args", {})
        base = {"ts": e.get("ts"), "pid": e.get("pid"), "tid": e.get("tid"), "process": processes.get(e.get("pid"), ""),
                "thread": threads.get((e.get("pid"), e.get("tid")), "")}
        if name == "Promise.settle":
            continue
        if name == "CSS.mediaFeature":
            css.append({**base, **args})
        elif name == "CSS.fontFamily":
            fonts.append({**base, **args})
        elif name.startswith("Intl."):
            intl.append({**base, "api": name, "fields": args})
        else:
            call = {**base, "api": name, "args": args.get("args", []), "threw": bool(args.get("threw")),
                    "ret": args.get("ret"), "caller": args.get("caller") or [], "world": args.get("world"),
                    "main_world": args.get("main_world")}
            if args.get("promise") is not None:
                call["promise"] = args["promise"]
                call["settled"] = settles.get((e["pid"], e["tid"], args["promise"]))
            calls.append(call)
    return {"calls": calls, "css": css, "fonts": fonts, "intl": intl, "data_loss": trace.get("data_loss", False),
            "event_count": len(events)}


# ---------------------------------------------------------------- rendering

def short(value, limit: int = 80) -> str:
    """One line for a value as the patch describes it."""
    if not isinstance(value, dict):
        return json.dumps(value)[:limit]
    t = value.get("type")
    if t in ("undefined", "null", "Proxy", "function", "SharedArrayBuffer", "other"):
        out = t
    elif t in ("boolean", "number", "bigint"):
        out = str(value.get("value")).lower() if t == "boolean" else str(value.get("value"))
    elif t == "string":
        s = value.get("value", "")
        out = json.dumps(s if len(s) <= 40 else s[:40] + "...")
        if value.get("length", 0) > 40:
            out += f" (len {value['length']}, fnv {value.get('hash')})"
    elif t == "symbol":
        out = f"Symbol({value.get('description', '')})"
    elif t == "wrapper":
        out = f"<{value.get('interface')}>"
    elif t == "Promise":
        out = f"Promise {value.get('state')}"
        if "value" in value:
            out += f": {short(value['value'], limit)}"
    elif t == "array":
        items = value.get("entries", {})
        out = "[" + ", ".join(short(v, 20) for v in items.values()) + (", ..." if value.get("length", 0) > len(items) else "") + "]"
    elif t == "object":
        items = value.get("entries")
        if items is None:
            out = f"<{value.get('constructor')}>"
        else:
            out = "{" + ", ".join(f"{k}: {short(v, 20)}" for k, v in items.items()) + "}"
    elif t == "accessor":
        out = "get/set"
    elif "byteLength" in value:
        items = value.get("items")
        shown = "" if items is None else "(" + ", ".join(str(i) for i in items[:8]) + (", ..." if value.get("length", 0) > 8 else "") + ")"
        out = f"{t}[{value.get('length', value.get('byteLength'))}]{shown} fnv {value.get('hash')}"
    else:
        out = json.dumps(value)
    return out if len(out) <= limit else out[: limit - 3] + "..."


def caller_text(call: dict) -> str:
    if not call.get("caller"):
        return ""
    f = call["caller"][0]
    url = f.get("url", "")
    return f"{url}:{f.get('line')}:{f.get('column')}"


def result_text(call: dict) -> str:
    if call["threw"]:
        return "threw"
    if call.get("settled"):
        s = call["settled"]
        return f"{s.get('state')}: {short(s.get('value'))}"
    return short(call.get("ret"))


def markdown(trace: dict, parsed: dict) -> str:
    lines = [f"# Fingerprint trace", "",
             f"- browser: {trace.get('browser')}", f"- binary: `{trace.get('binary')}`",
             f"- profile: `{trace.get('profile')}`", f"- headless: {trace.get('headless')}",
             f"- pages: " + ", ".join(p["url"] for p in trace.get("pages", [])),
             f"- events: {parsed['event_count']}, calls: {len(parsed['calls'])}, data loss: {parsed['data_loss']}", ""]
    by_thread = defaultdict(list)
    for c in parsed["calls"]:
        by_thread[(c["process"] or f"pid {c['pid']}", c["thread"] or f"tid {c['tid']}")].append(c)
    for (process, thread), calls in sorted(by_thread.items()):
        lines += [f"## {thread} ({process})", "", "| API | n | args | result | caller |", "| --- | --- | --- | --- | --- |"]
        seen = Counter()
        rows = {}
        for c in calls:
            key = (c["api"], tuple(short(a, 40) for a in c["args"]), result_text(c), caller_text(c))
            seen[key] += 1
            rows.setdefault(key, c)
        for key, c in rows.items():
            api, args, result, caller = key
            world = "" if c.get("main_world") in (True, None) else f" (world {c.get('world')})"
            lines.append(f"| `{api}`{world} | {seen[key]} | {', '.join(args)} | {result} | {caller} |".replace("\n", " "))
        lines.append("")
    if parsed["intl"]:
        lines += ["## Intl", "", "| thread | API | fields |", "| --- | --- | --- |"]
        for i in parsed["intl"]:
            lines.append(f"| {i['thread']} | `{i['api']}` | {json.dumps(i['fields'])} |")
        lines.append("")
    if parsed["css"]:
        lines += ["## CSS media features", "", "| thread | feature | result |", "| --- | --- | --- |"]
        for k, n in Counter((m["thread"], m.get("feature"), m.get("result")) for m in parsed["css"]).items():
            lines.append(f"| {k[0]} | `{k[1]}` | {k[2]} ({n}x) |")
        lines.append("")
    if parsed["fonts"]:
        lines += ["## Font families", "", "| thread | selector | family | resolved | result |", "| --- | --- | --- | --- | --- |"]
        for k in sorted({(f["thread"], f.get("selector"), f.get("family"), f.get("resolved"), f.get("result")) for f in parsed["fonts"]}, key=str):
            lines.append("| " + " | ".join(str(x) for x in k) + " |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- diff

def _path(url: str) -> str:
    u = urlparse(url)
    return u.path or url


def signature(parsed: dict) -> dict[tuple, Counter]:
    """(thread kind, calling script path, API, args) -> results, so that two
    runs on random ports and pids compare."""
    out: dict[tuple, Counter] = defaultdict(Counter)
    for c in parsed["calls"]:
        script = _path(c["caller"][0].get("url", "")) if c.get("caller") else ""
        key = (thread_kind(c["thread"]), script, c["api"], tuple(short(a, 200) for a in c["args"]))
        out[key][result_text(c)] += 1
    for i in parsed["intl"]:
        out[(thread_kind(i["thread"]), "", i["api"], ())][json.dumps(i["fields"], sort_keys=True)] += 1
    for m in parsed["css"]:
        out[(thread_kind(m["thread"]), "", "CSS.mediaFeature", (m.get("feature"),))][m.get("result")] += 1
    for f in parsed["fonts"]:
        out[(thread_kind(f["thread"]), "", "CSS.fontFamily", (f.get("selector"), f.get("family")))][f"{f.get('resolved')} {f.get('result')}"] += 1
    return out


def diff(a: dict, b: dict) -> list[str]:
    sa, sb = signature(a), signature(b)
    out = []
    for key in sorted(set(sa) | set(sb), key=str):
        ra, rb = set(sa.get(key, {})), set(sb.get(key, {}))
        if ra != rb:
            kind, script, api, args = key
            where = f"{kind} {script}".strip()
            out.append(f"{api}({', '.join(str(x) for x in args)}) [{where}]: {sorted(ra) or '-'} -> {sorted(rb) or '-'}")
    return out


def load(path: Path) -> tuple[dict, dict]:
    trace = json.loads(Path(path).read_text())
    return trace, parse(trace)
