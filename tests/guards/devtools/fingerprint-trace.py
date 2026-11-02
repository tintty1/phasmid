"""Guard for patches/phasmid/devtools/fingerprint-trace.patch (and
fingerprint-trace-intl.patch).

A local page calls a sample of allowlisted APIs in the main frame, a
cross-origin iframe, a dedicated, a shared and a service worker; each context
posts what JavaScript saw to the guard's server. The collector
(tests/inspector) traces the run from the browser target only.

  0. precondition: every context reported its values (the page ran);
  1. the trace holds events in disabled-by-default-phasmid.fingerprint, with
     no data loss;
  2. every sampled API has an event in every context it was called in, named
     as the generator names it, called from that context's script, in the
     main world, whose return value equals what JavaScript saw (strings by
     length and FNV-1a of their UTF-16 code units);
  3. a promise-returning call (StorageManager.estimate) carries a promise id
     whose Promise.settle event holds the resolved quota;
  4. tier 3: the matchMedia query appears as CSS.mediaFeature, canvas text
     resolves a font family in the document and in a worker
     (CSS.fontFamily), and Intl.DateTimeFormat resolvedOptions reports the
     time zone JavaScript saw, in the window and in the dedicated worker;
  5. the same page without tracing reports exactly the same values.
"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
from pathlib import Path

from helpers import GuardFailure, finish, parse_args
from inspector.trace import collect, parse

DONE = "phasmid-guard-done"
CONTEXTS = {"main": "main.js", "iframe_cross": "frame.js", "worker": "worker.js", "shared_worker": "shared.js",
            "service_worker": "sw.js"}

SAMPLE = r"""
function phasmidSample(win) {
  const v = {};
  const t = (k, f) => { try { v[k] = f(); } catch (e) { v[k] = {error: String(e)}; } };
  t("hardwareConcurrency", () => navigator.hardwareConcurrency);
  t("userAgent", () => navigator.userAgent);
  t("language", () => navigator.language);
  t("timeZone", () => Intl.DateTimeFormat().resolvedOptions().timeZone);
  if (win) {
    t("screenWidth", () => screen.width);
    t("devicePixelRatio", () => devicePixelRatio);
    t("matchMedia", () => matchMedia("(min-resolution: 1dppx)").matches);
    const c = document.createElement("canvas");
    c.width = 16; c.height = 16;
    const x = c.getContext("2d");
    x.fillStyle = "#f60"; x.fillRect(0, 0, 8, 8);
    x.font = "12px monospace";
    t("measure", () => x.measureText("phasmid").width);
    t("dataURL", () => c.toDataURL());
  } else {
    const x = new OffscreenCanvas(16, 16).getContext("2d");
    x.font = "12px monospace";
    t("measure", () => x.measureText("phasmid").width);
  }
  return v;
}
const phasmidReport = (ctx, values) => fetch("/report", {method: "POST", body: JSON.stringify({ctx, values})});
"""

FILES = {
    "main.html": '<!doctype html><meta charset="utf-8"><title>running</title><body><script src="/main.js"></script>',
    "frame.html": '<!doctype html><meta charset="utf-8"><body><script src="/frame.js"></script>',
    "main.js": SAMPLE + r"""
(async () => {
  const v = phasmidSample(true);
  try { v.quota = (await navigator.storage.estimate()).quota; } catch (e) { v.quota = {error: String(e)}; }
  await phasmidReport("main", v);
  const waiting = new Set(["iframe_cross", "worker", "shared_worker", "service_worker"]);
  const done = (ctx) => { waiting.delete(ctx); if (!waiting.size) document.title = "%DONE%"; };
  addEventListener("message", (e) => done(e.data));
  const frame = document.createElement("iframe");
  frame.src = "http://127.0.0.1:%PORT_B%/frame.html";
  document.body.append(frame);
  new Worker("/worker.js").onmessage = (e) => done(e.data);
  const shared = new SharedWorker("/shared.js");
  shared.port.onmessage = (e) => done(e.data);
  shared.port.start();
  await navigator.serviceWorker.register("/sw.js");
  const reg = await navigator.serviceWorker.ready;
  const channel = new MessageChannel();
  channel.port1.onmessage = (e) => done(e.data);
  reg.active.postMessage("go", [channel.port2]);
})();
""",
    "frame.js": SAMPLE + r"""
phasmidReport("iframe_cross", phasmidSample(true)).then(() => parent.postMessage("iframe_cross", "*"));
""",
    "worker.js": SAMPLE + r"""
phasmidReport("worker", phasmidSample(false)).then(() => postMessage("worker"));
""",
    "shared.js": SAMPLE + r"""
onconnect = (e) => { const port = e.ports[0]; phasmidReport("shared_worker", phasmidSample(false)).then(() => port.postMessage("shared_worker")); };
""",
    "sw.js": SAMPLE + r"""
addEventListener("message", (e) => { e.waitUntil(phasmidReport("service_worker", phasmidSample(false)).then(() => e.ports[0].postMessage("service_worker"))); });
""",
}


class Site:
    """Two loopback origins over one directory; POST /report collects what
    each context saw."""

    def __init__(self, root: Path):
        self.reports: dict[str, dict] = {}
        site = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(root), **kw)

            def log_message(self, *a):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                msg = json.loads(body)
                site.reports[msg["ctx"]] = msg["values"]
                self.send_response(204)
                self.end_headers()

        self.servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.ports = [s.server_address[1] for s in self.servers]

    def close(self):
        for s in self.servers:
            s.shutdown()


def fnv1a64_utf16(text: str) -> str:
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-16-le"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def same(ret: dict | None, js) -> bool:
    if not isinstance(ret, dict):
        return False
    if isinstance(js, bool):
        return ret.get("type") == "boolean" and ret.get("value") is js
    if isinstance(js, (int, float)):
        return ret.get("type") == "number" and ret.get("value") == js
    if isinstance(js, str):
        return (ret.get("type") == "string" and ret.get("length") == len(js.encode("utf-16-le")) // 2
                and ret.get("hash") == fnv1a64_utf16(js) and js.startswith(ret.get("value", "\0")))
    return False


def run_page(binary: str, profile: str, headful: bool, work: Path, trace: bool) -> tuple[dict, dict | None]:
    root = Path(tempfile.mkdtemp(dir=work))
    site = Site(root)
    try:
        for name, text in FILES.items():
            (root / name).write_text(text.replace("%PORT_B%", str(site.ports[1])).replace("%DONE%", DONE))
        result = collect(Path(binary), [f"http://127.0.0.1:{site.ports[0]}/main.html"], profile=Path(profile) if profile else None,
                         headless=not headful, until_title=DONE, wait=60, trace=trace)
        return dict(site.reports), (parse(result) if trace else None)
    finally:
        site.close()


# (context, API event name, JS key): the event's return value must equal what JS saw.
WINDOW = {"Navigator.hardwareConcurrency.get": "hardwareConcurrency", "Navigator.userAgent.get": "userAgent",
          "Navigator.language.get": "language", "Screen.width.get": "screenWidth",
          "Window.devicePixelRatio.get": "devicePixelRatio", "TextMetrics.width.get": "measure",
          "HTMLCanvasElement.toDataURL": "dataURL"}
WORKER = {"WorkerNavigator.hardwareConcurrency.get": "hardwareConcurrency", "WorkerNavigator.userAgent.get": "userAgent",
          "WorkerNavigator.language.get": "language", "TextMetrics.width.get": "measure"}
EXPECTED = {"main": WINDOW, "iframe_cross": WINDOW, "worker": WORKER, "shared_worker": WORKER, "service_worker": WORKER}


def from_script(call: dict, script: str) -> bool:
    return bool(call.get("caller")) and call["caller"][0].get("url", "").endswith("/" + script) and call["caller"][0].get("line", 0) > 0


def main() -> None:
    args = parse_args(__doc__)
    work = Path(args.work_dir)
    reports, parsed = run_page(args.binary, args.profile, args.headful, work, trace=True)

    missing = [c for c in CONTEXTS if c not in reports]
    if missing:
        raise GuardFailure(f"vacuous: contexts {missing} never reported; got {sorted(reports)}")
    errors = {c: {k: v for k, v in r.items() if isinstance(v, dict)} for c, r in reports.items()}
    errors = {c: e for c, e in errors.items() if e}
    if errors:
        raise GuardFailure(f"vacuous: the probe failed in some contexts: {errors}")
    print(f"PASS: precondition: all {len(CONTEXTS)} contexts ran the probe")

    if parsed["event_count"] == 0:
        raise GuardFailure("no events in category disabled-by-default-phasmid.fingerprint: the bindings carry no trace "
                           "(devtools/fingerprint-trace.patch missing or the generator hook no longer fires)")
    if parsed["data_loss"]:
        raise GuardFailure("the browser reported trace data loss")
    print(f"PASS: {parsed['event_count']} events, {len(parsed['calls'])} calls, no data loss")

    calls = parsed["calls"]
    for ctx, expected in EXPECTED.items():
        script = CONTEXTS[ctx]
        for api, key in expected.items():
            found = [c for c in calls if c["api"] == api and from_script(c, script)]
            if not found:
                raise GuardFailure(f"{ctx}: no {api} event called from {script}")
            # Workers have one world and no main world; a window context must be the page's main world.
            good = [c for c in found if not c["threw"] and (c.get("main_world") or expected is WORKER)
                    and same(c.get("ret"), reports[ctx][key])]
            if not good:
                raise GuardFailure(f"{ctx}: {api} returned {[c.get('ret') for c in found]}, JavaScript saw {reports[ctx][key]!r}")
        print(f"PASS: {ctx}: {len(expected)} APIs traced from {script} with the values JavaScript saw")
    worker_threads = {c["thread"] for c in calls if from_script(c, "worker.js")}
    if not worker_threads or worker_threads & {c["thread"] for c in calls if from_script(c, "main.js")}:
        raise GuardFailure(f"worker events are not on a thread of their own: {worker_threads}")

    estimate = [c for c in calls if c["api"] == "StorageManager.estimate" and from_script(c, "main.js")]
    settled = [c.get("settled") for c in estimate if c.get("settled")]
    quota = [(s.get("value") or {}).get("entries", {}).get("quota") for s in settled if s.get("state") == "fulfilled"]
    if not any(same(q, reports["main"]["quota"]) for q in quota):
        raise GuardFailure(f"StorageManager.estimate: no Promise.settle joined with quota {reports['main']['quota']}: {estimate}")
    print(f"PASS: StorageManager.estimate promise {estimate[0].get('promise')} settles with quota {reports['main']['quota']}")

    media = [m for m in parsed["css"] if "min-resolution" in (m.get("feature") or "")]
    if not media or any(m.get("result") != ("true" if reports["main"]["matchMedia"] else "false") for m in media):
        raise GuardFailure(f"CSS.mediaFeature (min-resolution: 1dppx): {media}, JavaScript saw {reports['main']['matchMedia']}")
    selectors = {f.get("selector") for f in parsed["fonts"]}
    if not {"document", "worker"} <= selectors:
        raise GuardFailure(f"CSS.fontFamily: lookups seen from {selectors}, expected document and worker")
    print(f"PASS: tier 3: media feature and font lookups (document, worker) traced")

    for ctx, script in (("main", "main.js"), ("worker", "worker.js")):
        thread = {c["thread"] for c in calls if from_script(c, script)}
        zones = [i["fields"].get("timeZone") for i in parsed["intl"]
                 if i["api"] == "Intl.DateTimeFormat.prototype.resolvedOptions" and i["thread"] in thread]
        if reports[ctx]["timeZone"] not in zones:
            raise GuardFailure(f"{ctx}: Intl.DateTimeFormat resolvedOptions events {zones}, JavaScript saw {reports[ctx]['timeZone']!r}")
    print("PASS: Intl.DateTimeFormat resolvedOptions traced in window and worker")

    untraced, _ = run_page(args.binary, args.profile, args.headful, work, trace=False)
    if untraced != reports:
        diff = {c: {k: (reports.get(c, {}).get(k), untraced.get(c, {}).get(k))
                    for k in set(reports.get(c, {})) | set(untraced.get(c, {}))
                    if reports.get(c, {}).get(k) != untraced.get(c, {}).get(k)} for c in CONTEXTS}
        raise GuardFailure(f"the page sees different values with tracing on: {diff}")
    print("PASS: tracing on and off: every context reports identical values")


if __name__ == "__main__":
    finish(main)
