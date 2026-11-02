"""Guard for fonts/family-names.patch: a windows profile resolves exactly the
CSS family names stock Chrome 156 resolves on the Windows machine its font
store comes from, each with the face Windows draws it with.

Windows Chrome matches a family through DirectWrite, which names a face by
its weight-stretch-style family (OCR A, Franklin Gothic, Sitka Small,
Segoe UI Variable Display) rather than the names in the file, and Blink
retries "<family> <weight or stretch word>" as the family at that weight or
stretch (Arial Light, Lucida Sans Demibold, Bahnschrift Condensed). The
reference (profiles/data/fonts/metrics/<profile>.chrome156-family-names.json)
is the machine's answer for 2934 names: the 55 family names Pixelscan's
masking check probes, every name the font files carry, the names left after
dropping style words and Blink's suffix forms. A name resolves when the
canvas width of a Latin string under '"<name>", <base>' differs from the
base alone; the serif width identifies the face.

  0. precondition: the profile is a windows profile with a reference; the
     store starts with no fonts.* warning (otherwise the store is missing a
     file or does not verify, and the comparison is vacuous; no store at all
     is a SKIP); without a profile the probe sees a Linux family resolve, so
     it can tell a resolving name from a missing one;
  1. in the main frame and in a dedicated worker's OffscreenCanvas, each of
     the 55 Pixelscan names resolves;
  2. every name the machine resolves resolves, at the machine's serif width
     (within 0.01 px; PMingLiU-ExtB's own width is reported only, it differs
     by the advance rounding of fonts/metrics.patch);
  3. every name the machine does not resolve (Arial Rounded MT Bold, OCR A
     Extended, Sitka, Segoe UI Variable, Franklin Gothic Demi, ...) stays
     unresolved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure, GuardSkip, finish, load_profile, parse_args, start_and_exit, unbacked_warnings

ROOT = Path(__file__).resolve().parents[3]
REFERENCES = ROOT / "profiles" / "data" / "fonts" / "metrics"
LINUX = ["DejaVu Sans", "Liberation Sans", "Noto Sans", "Cantarell", "Ubuntu"]
# Widths reported only: the face is right, its advances are rounded.
WIDTH_EXEMPT = {"PMingLiU-ExtB", "新細明體-ExtB"}
TOLERANCE = 0.01

PROBE = r"""
async (args) => {
  const measure = (ctx) => {
    const q = (n) => '"' + n.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
    const w = (font) => { ctx.font = font; return ctx.measureText(args.string).width; };
    const base = {};
    for (const b of args.bases) base[b] = w(`64px ${b}`);
    const out = {};
    for (const n of args.names) {
      const r = { resolved: false, serif: null };
      for (const b of args.bases) {
        const v = w(`64px ${q(n)}, ${b}`);
        if (b === "serif") r.serif = v;
        if (Math.abs(v - base[b]) > 1e-3) r.resolved = true;
      }
      out[n] = r;
    }
    return out;
  };
  const main = measure(document.createElement("canvas").getContext("2d"));
  const src = `onmessage = (e) => { const args = e.data; const measure = ${measure.toString()};
    postMessage(measure(new OffscreenCanvas(8, 8).getContext("2d"))); };`;
  const worker = await new Promise((resolve) => {
    const wk = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
    wk.onmessage = (e) => resolve(e.data);
    wk.onerror = (e) => resolve({ error: String(e.message) });
    wk.postMessage(args);
  });
  return { main, worker };
}
"""


def run_probe(binary: str, profile: Path | None, headless: bool, names: list[str], bases: list[str], string: str) -> dict:
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            result = page.evaluate(PROBE, {"names": names, "bases": bases, "string": string})
        finally:
            launch.stop()
    if "error" in result["worker"]:
        raise GuardFailure(f"the probe's worker failed: {result['worker']['error']}")
    return result


def store_exists() -> bool:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    root = Path(data) / "phasmid" / "fonts" / "windows"
    return any((d / "manifest.json").is_file() for d in root.glob("*"))


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    if profile["os"]["family"] != "windows":
        raise GuardSkip("this guard covers Windows profiles (profiles/examples/win11-uhd600-1440x960.json)")
    ref_path = REFERENCES / f"{profile['id']}.chrome156-family-names.json"
    if not ref_path.is_file():
        raise GuardSkip(f"no Windows family-name reference for {profile['id']} under {REFERENCES.relative_to(ROOT)}")
    ref = json.loads(ref_path.read_text())
    if not store_exists():
        raise GuardSkip("no Windows font store under $XDG_DATA_HOME/phasmid/fonts/windows; import it")
    resolved, unresolved, core = ref["resolved"], ref["unresolved"], ref["pixelscan_core"]
    bases = list(ref["bases"])
    names = [*resolved, *unresolved]

    # 0. Preconditions.
    r = start_and_exit(args.binary, profile, work, "windows-store")
    fonts_warnings = {k: v for k, v in unbacked_warnings(r.stderr).items() if k.startswith("fonts.")}
    if r.returncode != 0 or fonts_warnings:
        raise GuardFailure(f"vacuous: the windows profile does not start on a verified store: exit {r.returncode}, "
                           f"font warnings {fonts_warnings}, stderr {r.stderr[-400:]!r}")
    stock = run_probe(args.binary, None, headless, LINUX + ["PhasmidNoSuchFont"], bases, ref["string"])
    seen = [n for n in LINUX if stock["main"][n]["resolved"]]
    if not seen or stock["main"]["PhasmidNoSuchFont"]["resolved"]:
        raise GuardFailure(f"vacuous: without a profile the probe sees {seen} of {LINUX} resolve and PhasmidNoSuchFont "
                           f"{'resolve' if stock['main']['PhasmidNoSuchFont']['resolved'] else 'not resolve'}")
    print(f"PASS: precondition: the profile starts on a verified store with no font warning; without a profile {seen} resolve")

    path = work / "windows.json"
    path.write_text(json.dumps(profile))
    got = run_probe(args.binary, path, headless, names, bases, ref["string"])
    failures = []
    for scope in ("main", "worker"):
        g = got[scope]
        # 1. Pixelscan's names.
        lost_core = [n for n in core if not g[n]["resolved"]]
        if lost_core:
            failures.append(f"{scope}: {len(lost_core)} of {len(core)} Pixelscan names unresolved: {lost_core}")
        else:
            print(f"PASS: {scope}: the {len(core)} Pixelscan names resolve")
        # 2. Every Windows name, on the Windows face.
        lost = [n for n in resolved if not g[n]["resolved"]]
        face = {n: (g[n]["serif"], w) for n, w in resolved.items()
                if g[n]["resolved"] and n not in WIDTH_EXEMPT and abs(g[n]["serif"] - w) > TOLERANCE}
        exempt = {n: (g[n]["serif"], resolved[n]) for n in WIDTH_EXEMPT if n in resolved and g[n]["resolved"]}
        if lost:
            failures.append(f"{scope}: {len(lost)} of {len(resolved)} names Windows resolves are unresolved: {lost}")
        if face:
            failures.append(f"{scope}: {len(face)} names draw another face than on Windows (got, Windows): {face}")
        if not lost and not face:
            print(f"PASS: {scope}: the {len(resolved)} names Windows resolves resolve at its serif width; "
                  f"INFO: exempt widths {exempt}")
        # 3. Nothing Windows does not resolve.
        extra = [n for n in unresolved if g[n]["resolved"]]
        if extra:
            failures.append(f"{scope}: {len(extra)} names resolve that Windows does not: {extra}")
        else:
            print(f"PASS: {scope}: the {len(unresolved)} names Windows does not resolve stay unresolved")
    if failures:
        raise GuardFailure("; ".join(failures))


finish(main)
