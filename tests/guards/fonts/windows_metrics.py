"""Guard for the Windows font patches: fonts/availability.patch (the Windows
font store and the name gate), fonts/generic-families.patch,
fonts/system-font.patch, fonts/metrics.patch and fonts/windows-fallback.patch.

A windows profile on this Linux build measures text as stock Chrome 156 does
on the Windows 11 machine its font files come from. The probe
(metrics_probe.js) measures 38 families (18 Windows, 12 Linux, the 7 generic
families, one missing name) on canvas in the main frame and in a dedicated
worker's OffscreenCanvas, as DOM spans, with document.fonts.check, and
whether each family resolves. The references are that machine's results
(profiles/data/fonts/metrics/<profile>.chrome156-dpr{1.5,1}.json).

  0. precondition: the Windows font store exists (otherwise SKIP: the user
     imports it), its
     manifest.json is a committed profiles/data/fonts manifest, and every
     file of the probed Windows families matches its SHA-256 (otherwise the
     comparison is vacuous); without a profile this build resolves at least
     one of the probed Linux families, so check 3 can fail;
  1. the profile starts on the store's default location: exit 0 and no
     fonts.families or fonts.emoji warning (a refused windows profile, or a
     store the browser does not find or verify, fails here);
  2. at the profile's DPR (1.5) and in a DPR 1 variant: every main-frame and
     worker value equals the reference except actualBoundingBox* (ink
     bounds, reported only), and the DOM rects and
     document.fonts.check equal it too;
  3. no Linux family, the generic emoji and PhasmidNoSuchFont stay
     unresolved, and every probed Windows family resolves, in the main frame
     and the worker.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure, GuardSkip, find_store, finish, load_profile, parse_args, start_and_exit, unbacked_warnings

ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).with_name("metrics_probe.js")
MANIFESTS = ROOT / "profiles" / "data" / "fonts"
REFERENCES = MANIFESTS / "metrics"
INK = ("actualBoundingBoxLeft", "actualBoundingBoxRight", "actualBoundingBoxAscent", "actualBoundingBoxDescent")
GEOMETRY = ("width", "height", "avail_width", "avail_height", "avail_left", "avail_top")
WINDOW = ("inner_width", "inner_height", "outer_width", "outer_height", "screen_x", "screen_y")


def check_store(store: Path, manifest: dict, families: list[str]) -> None:
    committed = MANIFESTS / f"{manifest.get('set')}.json"
    if not committed.is_file() or json.loads(committed.read_text()) != manifest:
        raise GuardFailure(f"vacuous: {store}/manifest.json is not the committed manifest {committed.relative_to(ROOT)}")
    hashes = {f["name"]: f["sha256"] for f in manifest["files"]}
    lower = {k.lower(): v for k, v in manifest["families"].items()}
    bad = {}
    for family in families:
        for name in lower.get(family.lower(), []):
            path = store / name
            if not path.is_file():
                bad[name] = "missing"
            elif hashlib.sha256(path.read_bytes()).hexdigest() != hashes.get(name):
                bad[name] = "SHA-256 differs from the manifest"
        if family.lower() not in lower:
            bad[family] = "no file in the manifest"
    if bad:
        raise GuardFailure(f"vacuous: the store {store} cannot back the probed Windows families: {bad}")
    print(f"PASS: precondition: the store {store} holds the committed manifest {manifest['set']} and verified files for {len(families)} probed Windows families")


def dpr1_variant(profile: dict) -> dict:
    """The same machine at 100 % scaling: DPR 1, every CSS-pixel length of
    the display times 1.5 (the DPR 1 reference reports 2160x1440)."""
    p = copy.deepcopy(profile)
    d = p["display"]
    scale = d["device_pixel_ratio"]
    for k in GEOMETRY:
        d[k] = round(d[k] * scale)
    for k in WINDOW:
        d["window"][k] = round(d["window"][k] * scale)
    d["device_pixel_ratio"] = 1.0
    return p


def run_probe(binary: str, profile: Path | None, headless: bool) -> dict:
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            result = page.evaluate(PROBE.read_text())
        finally:
            launch.stop()
    if "error" in result.get("worker", {}):
        raise GuardFailure(f"the probe's worker failed: {result['worker']['error']}")
    return result


def diffs(got: dict, want: dict) -> tuple[list[str], int]:
    """Values that differ, as 'scope/family/key/field: got != want', and the
    number of differing ink-bounds values (reported only)."""
    out, ink = [], 0
    for scope in ("main", "worker"):
        for family, keys in want[scope].items():
            for key, fields in keys.items():
                actual = got.get(scope, {}).get(family, {}).get(key)
                if not isinstance(fields, dict):
                    if actual != fields:
                        out.append(f"{scope}/{family}/{key}: {actual!r} != {fields!r}")
                    continue
                for field, value in fields.items():
                    a = (actual or {}).get(field)
                    if a == value:
                        continue
                    if field in INK:
                        ink += 1
                    else:
                        out.append(f"{scope}/{family}/{key}/{field}: {a!r} != {value!r}")
    for family, rect in want["dom"].items():
        if got["dom"].get(family) != rect:
            out.append(f"dom/{family}: {got['dom'].get(family)!r} != {rect!r}")
    for family, value in want["fontsCheck"].items():
        if got["fontsCheck"].get(family) != value:
            out.append(f"fontsCheck/{family}: {got['fontsCheck'].get(family)!r} != {value!r}")
    return out, ink


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    if profile["os"]["family"] != "windows":
        raise GuardSkip("this guard covers Windows profiles (profiles/examples/win11-uhd600-1440x960.json)")
    refs = {dpr: REFERENCES / f"{profile['id']}.chrome156-dpr{dpr}.json" for dpr in ("1.5", "1")}
    if not all(p.is_file() for p in refs.values()):
        raise GuardSkip(f"no Windows reference measurements for {profile['id']} under {REFERENCES.relative_to(ROOT)}")
    want = {dpr: json.loads(p.read_text()) for dpr, p in refs.items()}
    probed = want["1.5"]["fonts"]
    if profile["display"]["device_pixel_ratio"] != want["1.5"]["dpr"]:
        raise GuardFailure(f"vacuous: the profile's DPR {profile['display']['device_pixel_ratio']} is not the reference's {want['1.5']['dpr']}")

    # 0. Preconditions.
    store, manifest = find_store(profile["fonts"]["families"])
    check_store(store, manifest, probed["windows"])
    stock = run_probe(args.binary, None, headless)
    linux_here = [f for f in probed["linux"] if stock["main"][f]["resolved"]]
    if not linux_here:
        raise GuardFailure(f"vacuous: without a profile this build resolves none of {probed['linux']}")
    print(f"PASS: precondition: without a profile {linux_here} resolve")

    # 1. The profile starts on the store, with nothing unbacked in fonts.
    r = start_and_exit(args.binary, profile, work, "windows-store")
    warnings = unbacked_warnings(r.stderr)
    fonts_warnings = {k: v for k, v in warnings.items() if k.startswith("fonts.")}
    if r.returncode != 0 or fonts_warnings:
        raise GuardFailure(f"the windows profile with the store {store}: exit {r.returncode}, font warnings {fonts_warnings}, "
                           f"stderr {r.stderr[-400:]!r}")
    print(f"PASS: the windows profile starts on the store {store} with no font warning")

    # 2 and 3, at each DPR.
    variants = {"1.5": profile, "1": dpr1_variant(profile)}
    failures = []
    for dpr, p in variants.items():
        path = work / f"windows-dpr{dpr}.json"
        path.write_text(json.dumps(p))
        got = run_probe(args.binary, path, headless)
        if got["dpr"] != want[dpr]["dpr"]:
            failures.append(f"DPR {dpr}: devicePixelRatio is {got['dpr']}")
            continue
        bad, ink = diffs(got, want[dpr])
        print(f"INFO: DPR {dpr}: {ink} actualBoundingBox* values differ from Windows (not checked)")
        if bad:
            failures.append(f"DPR {dpr}: {len(bad)} values differ from Windows Chrome 156, first {bad[:12]}")
        else:
            print(f"PASS: DPR {dpr}: main frame, worker, DOM and fonts.check equal Windows Chrome 156 outside actualBoundingBox*")
        for scope in ("main", "worker"):
            leaked = [f for f in [*probed["linux"], "emoji", *probed["missing"]] if got[scope][f]["resolved"]]
            lost = [f for f in probed["windows"] if not got[scope][f]["resolved"]]
            if leaked or lost:
                failures.append(f"DPR {dpr} {scope}: resolved but absent on Windows {leaked}, absent but resolved on Windows {lost}")
        if not any(f.startswith(f"DPR {dpr} ") for f in failures):
            print(f"PASS: DPR {dpr}: Linux families, emoji and PhasmidNoSuchFont unresolved, {len(probed['windows'])} Windows families resolved, main and worker")
    if failures:
        raise GuardFailure("; ".join(failures))


finish(main)
