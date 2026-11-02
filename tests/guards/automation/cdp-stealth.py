"""Guard for patches/phasmid/automation/cdp-stealth.patch.

Playwright sends Runtime.enable on attach, so the V8 inspector describes
every console argument and uncaught exception for the client. A page that
plants getters, a Proxy or Error.prepareStackTrace where that description
looks (Brotector's runtime.enabled test) learns that a client is attached.
Stock Chrome runs some page code for console calls with no client at all
(the message text calls Error.prototype.toString), so the reference is the
same binary without a client: the probe page records every call it sees and
writes the record into the document, read once with --dump-dom (no CDP) and
once with Playwright attached.

  0. precondition: with the client attached, it receives the probe's console
     messages and the control error is described as "ProbeError: control"
     (descriptions are still built, so the comparison is not vacuous), and
     the record without a client is non-empty (the probes ran);
  1. the record with the client equals the record without: Brotector's
     probe as it ships (an own `stack` getter, then an Error.prototype.name
     getter, through console.context().debug), getters on a prototype for
     name, message and stack across log, error, debug, dir and table, a
     Proxy on the prototype chain, Error.prepareStackTrace (on an error and
     on a stack Error.captureStackTrace installed), RegExp.prototype.flags
     reaching a page-defined `global`, the stack header a page reads after
     logging an error, and an uncaught exception.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from helpers import GuardFailure, finish, parse_args, run_binary

PROBE_HTML = """<!doctype html><body><script>
const rec = { getters: [], traps: [], prepareStackTrace: 0, lateHeader: "", uncaught: 0 };
const restore = [];
const hook = (target, prop, label) => {
  const before = Object.getOwnPropertyDescriptor(target, prop);
  Object.defineProperty(target, prop, {
    configurable: true,
    get() { rec.getters.push(label); return before && "value" in before ? before.value : label; },
  });
  restore.push(() => before ? Object.defineProperty(target, prop, before) : delete target[prop]);
};
const unhook = () => restore.splice(0).reverse().forEach((f) => f());

// Control: a data property, read as in stock Chrome.
class ProbeError extends Error {}
Object.defineProperty(ProbeError.prototype, "name", { value: "ProbeError" });
console.log(new ProbeError("control"));

// Brotector's probe.
const own = new Error();
Object.defineProperty(own, "stack", { configurable: false, enumerable: false, get() { rec.getters.push("own stack"); return ""; } });
let c;
try { c = console.context("probe: "); } catch { c = console; }
c.debug(own);
hook(Error.prototype, "name", "Error.prototype.name");
c.debug(new Error(""));
unhook();

// Prototype getters.
for (const prop of ["name", "message", "stack"]) {
  class Sub extends Error {}
  hook(Sub.prototype, prop, `prototype ${prop}`);
  for (const m of ["log", "error", "debug", "dir"]) console[m](prop === "message" ? new Sub() : new Sub("x"));
  console.table([new Sub("x")]);
  unhook();
}

// A Proxy on the chain.
const trapped = new Error("x");
const traps = {};
for (const t of ["get", "getOwnPropertyDescriptor", "getPrototypeOf", "has", "ownKeys"]) {
  traps[t] = (...a) => { rec.traps.push(`${t} ${String(a[1])}`); return Reflect[t](...a); };
}
Object.setPrototypeOf(trapped, new Proxy(Error.prototype, traps));
console.log(trapped);
console.dir(trapped);

// Error.prepareStackTrace runs when a stack is first formatted.
Error.prepareStackTrace = (e, frames) => { rec.prepareStackTrace++; return "prepared"; };
console.log(new Error("p"));
console.error(new TypeError("p"));
delete Error.prepareStackTrace;

// Builtin getters that reach page code: RegExp.prototype.flags reads
// `global`, and a stack Error.captureStackTrace installed on a plain object.
const re = /x/g;
Object.defineProperty(re, "global", { get() { rec.getters.push("regexp global"); return true; } });
console.log(re);
const captured = {};
Error.captureStackTrace(captured);
Error.prepareStackTrace = () => { rec.prepareStackTrace++; return "prepared"; };
console.log(captured);
delete Error.prepareStackTrace;

// The header of a stack the page reads after logging the error.
class Late extends Error {}
const late = new Late("late");
console.log(late);
Object.defineProperty(Late.prototype, "name", { value: "LateName" });
rec.lateHeader = late.stack.split("\\n")[0];

// Timing (reported, not asserted): with a
// client, each console call captures up to 200 frames and serializes its
// arguments with previews before returning.
{
  const N = 300;
  const timeLogs = (arg) => { const t = performance.now(); for (let i = 0; i < N; i++) console.debug(arg); return performance.now() - t; };
  const atDepth = (d, f) => d === 0 ? f() : atDepth(d - 1, f);
  const wide = {}; for (let i = 0; i < 2000; i++) wide["k" + i] = i;
  let shallow = 0, deep = 0, wideMs = 0;
  for (let k = 0; k < 2; k++) { shallow = timeLogs(1); deep = atDepth(150, () => timeLogs(1)); wideMs = timeLogs(wide); }
  rec.timing = { shallowMs: shallow, deepRatio: deep / shallow, wideRatio: wideMs / shallow };
}

// An uncaught exception.
class Thrown extends Error {}
hook(Thrown.prototype, "name", "uncaught name");
setTimeout(() => { throw new Thrown("t"); }, 0);
setTimeout(() => {
  unhook();
  document.body.textContent = "RESULT " + JSON.stringify(rec);
}, 400);
</script></body>"""


def record_from(text: str) -> dict:
    m = re.search(r"RESULT (\{.*\})", text)
    if not m:
        raise GuardFailure(f"the probe page wrote no record: {text[-300:]!r}")
    return json.loads(m.group(1))


def main() -> None:
    args = parse_args(__doc__)
    work = Path(args.work_dir)
    page_path = work / "cdp-probe.html"
    page_path.write_text(PROBE_HTML)
    url = page_path.resolve().as_uri()
    profile = [f"--phasmid-profile={Path(args.profile).resolve()}"] if args.profile else []

    r = run_binary(args.binary, [*profile, "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}",
                                 "--no-first-run", "--virtual-time-budget=2000", "--dump-dom", url], timeout=60)
    baseline = record_from(r.stdout)

    launch = Launch(binary=args.binary, headless=not args.headful, profile=args.profile or None)
    messages: list[str] = []
    with sync_playwright() as pw:
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.on("console", lambda msg: messages.append(msg.text))
            page.goto(url)
            page.wait_for_function("document.body.textContent.startsWith('RESULT')", timeout=10_000)
            attached = record_from(page.evaluate("document.body.textContent"))
            deadline = time.monotonic() + 5
            while not any("ProbeError: control" in m for m in messages) and time.monotonic() < deadline:
                page.wait_for_timeout(100)
        finally:
            launch.stop()

    if not any("ProbeError: control" in m for m in messages):
        raise GuardFailure(f"vacuous: the client never received the control error's description; console messages: {messages[:5]}")
    if not baseline["getters"] or not baseline["lateHeader"]:
        raise GuardFailure(f"vacuous: without a client the probes recorded nothing: {baseline}")
    print(f"PASS: precondition: the client received {len(messages)} console messages, the control described as ProbeError: control; "
          f"without a client the page saw {len(baseline['getters'])} getter calls")

    tb, ta = baseline.pop("timing"), attached.pop("timing")
    print(f"INFO: console timing without / with the client (not asserted, a known limit): "
          f"300 calls {tb['shallowMs']:.1f} / {ta['shallowMs']:.1f} ms, deep/shallow {tb['deepRatio']:.1f} / {ta['deepRatio']:.1f}, "
          f"wide/shallow {tb['wideRatio']:.1f} / {ta['wideRatio']:.1f}")
    diffs = {k: (baseline[k], attached[k]) for k in baseline if baseline[k] != attached.get(k)}
    if diffs:
        raise GuardFailure("the page sees the client: " + "; ".join(f"{k}: without {b!r}, with {a!r}" for k, (b, a) in diffs.items()))
    print("PASS: with the client attached the page records exactly what it records without one "
          "(getters, Proxy traps, Error.prepareStackTrace, late stack header, uncaught exception)")


finish(main)
