"""Guard for patches/phasmid/graphics/canvas-readback.patch.

Canvas readback noise is deterministic, idempotent and limited to drawn,
non-exact pixels. Full dumps (every context, headless unless --headful):

  R0  no profile: this build's own behaviour, the reference (headless with
      --enable-gpu: a profile run renders on the host GPU);
  R1  the profile with canvas.noise false, seed S1;
  R1' the same with seed S2;
  R2  canvas.noise true, seed S1.

Short sessions (main frame and a dedicated worker's OffscreenCanvas): L1
noise off, L2 seed S1, L3 seed S2, L4 seed S1 in a second launch.

  0. precondition: R2 changes the probe scene's getImageData against R1 in
     every context, else the patch did nothing ("vacuous");
  1. with noise off the patch is inert, in all six contexts: R1' equals R1
     on every canvas and canvasNoise key (the seed does nothing), and R1
     equals R0 on every key a Linux profile renders as the host does. A
     Windows profile renders text with Windows metrics and its font store
     (fonts/windows-*.patch), so there the keys of canvases with text
     (TEXT_KEYS) are left out of the R0 comparison;
  2. R2 in all six contexts: canvas.deterministic and subRectMatchesFull,
     getImageData == offscreenGetImageData, every detector invariant of the
     canvasNoise probe section as in R0, and the 2x2, blank and solid keys
     equal to R0's (exact writes and alpha-0 pixels stay exact), while text
     on a transparent canvas changes against R1 (the alpha branch); no
     cross-context split beyond R1's on canvas keys;
  3. L3 differs from L2 and L4 equals L2 (seed-keyed, stable across
     restarts); against L1, L2 changes more than 0 and less than 2% of the
     scene's pixels, each by at most 1 in one channel or in alpha (with the
     colour re-derived);
  4. a screenshot of an on-page scene canvas is byte-identical in L1 and L2:
     nothing on screen changes.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.contexts import cross_context_report
from consistency.diff import flatten
from consistency.server import Servers
from _gpu import host_gpu_args
from helpers import ALL_CONTEXTS, WINDOW_CONTEXTS, GuardFailure, dump, finish, load_profile, parse_args

SEED_1 = "5b1d7c0e9a3f4d2c8e6b1a0f7c3d9e21"
SEED_2 = "0123456789abcdef0123456789abcdef"
WIDTH, HEIGHT = 300, 150

# Draws an anti-aliased scene (text, curves, a translucent arc over an opaque
# rectangle) and returns its getImageData bytes, base64, and their SHA-256.
SCENE = """async () => {
  const w = %d, h = %d;
  const canvas = typeof document !== "undefined"
    ? Object.assign(document.createElement("canvas"), { width: w, height: h })
    : new OffscreenCanvas(w, h);
  const c = canvas.getContext("2d", { willReadFrequently: true });
  c.fillStyle = "#f60"; c.fillRect(10, 10, 100, 50);
  c.fillStyle = "#069"; c.font = "16px sans-serif"; c.fillText("Phasmid canvas guard", 2, 30);
  c.fillStyle = "rgba(102, 204, 0, 0.7)"; c.beginPath(); c.arc(200, 75, 40, 0, Math.PI * 2); c.fill();
  c.strokeStyle = "#333"; c.lineWidth = 2.5; c.beginPath(); c.moveTo(120, 90);
  c.bezierCurveTo(140, 60, 180, 140, 220, 100); c.stroke();
  const data = c.getImageData(0, 0, w, h).data;
  const digest = await crypto.subtle.digest("SHA-256", data);
  let bin = "";
  for (let i = 0; i < data.length; i += 0x8000) bin += String.fromCharCode(...data.subarray(i, i + 0x8000));
  return { hash: [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join(""), pixels: btoa(bin) };
}""" % (WIDTH, HEIGHT)

IN_WORKER = """async (src) => {
  const code = `self.onmessage = async (e) => {
    let r;
    try { r = await (0, eval)("(" + e.data + ")")(); } catch (err) { r = { error: String(err && err.stack || err) }; }
    self.postMessage(r);
  };`;
  const w = new Worker(URL.createObjectURL(new Blob([code], { type: "text/javascript" })));
  return await new Promise((res) => {
    w.onmessage = (e) => res(e.data);
    w.onerror = (e) => res({ error: "worker error: " + e.message });
    w.postMessage(src);
  });
}"""

# The same scene, painted on the page, for the screenshot.
SHOW = """async () => {
  document.body.innerHTML = "";
  document.body.style.margin = "0";
  const shown = document.createElement("canvas");
  shown.width = %d; shown.height = %d;
  shown.style.display = "block";
  document.body.appendChild(shown);
  const c = shown.getContext("2d");
  c.fillStyle = "#f60"; c.fillRect(10, 10, 100, 50);
  c.fillStyle = "#069"; c.font = "16px sans-serif"; c.fillText("Phasmid canvas guard", 2, 30);
  c.fillStyle = "rgba(102, 204, 0, 0.7)"; c.beginPath(); c.arc(200, 75, 40, 0, Math.PI * 2); c.fill();
  c.strokeStyle = "#333"; c.lineWidth = 2.5; c.beginPath(); c.moveTo(120, 90);
  c.bezierCurveTo(140, 60, 180, 140, 220, 100); c.stroke();
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  return true;
}""" % (WIDTH, HEIGHT)

# Keys equal to R0's even with noise on: exact writes and transparent pixels
# are never touched, and a 2x2 canvas has no pixel with two neighbours.
EXACT_KEYS = ["lowEntropy2x2", "blankPng", "solidPng", "pixelMods"]

# Flattened dump keys of canvases that draw text: a Windows profile changes
# them through its text rendering, not through this patch.
TEXT_KEYS = {f"canvas.{k}" for k in ("getImageData", "getImageDataSecondDraw", "toDataURL", "toDataURLJpeg", "toBlob",
                                     "offscreenGetImageData", "offscreenConvertToBlob", "viaImageBitmap", "textOnly")}
TEXT_KEYS.add("canvasNoise.transparentText")


# Families the dump's canvas scenes draw in, and the fallback for the CJK
# text of canvas.textOnly. R0 renders them with whatever host files they
# resolve to, so the profile runs list those families too:
# otherwise fonts/availability.patch hides them (a host with Windows Arial
# next to a profile without it) and R1 differs from R0 for a reason other
# than this patch.
SCENE_FAMILIES = ("Arial", "Times New Roman", "sans-serif", "sans-serif:charset=6f22")


def host_scene_families() -> list[str]:
    found = []
    for name in SCENE_FAMILIES:
        out = subprocess.run(["fc-match", "--format", "%{family[0]}", name], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            found.append(out.stdout.strip())
    return found


def write_profile(profile: dict, work: Path, name: str, noise: bool, seed: str) -> Path:
    p = json.loads(json.dumps(profile))
    families = p["fonts"]["families"]
    families += [f for f in dict.fromkeys(host_scene_families()) if f not in families]
    p["canvas"]["noise"] = noise
    p["seed"] = seed
    path = work / f"{name}.json"
    path.write_text(json.dumps(p))
    return path


def session(binary: str, profile: Path, headless: bool, screenshot: bool) -> dict:
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            out = {"main": page.evaluate(SCENE), "worker": page.evaluate(IN_WORKER, SCENE)}
            if screenshot:
                page.evaluate(SHOW)
                out["screenshot"] = page.screenshot(clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
            return out
        finally:
            launch.stop()


def checked_dump(binary: str, profile: Path | None, headless: bool, what: str) -> dict:
    extra = [] if profile else host_gpu_args(headless)
    d = dump(binary, str(profile) if profile else None, headless=headless, extra_args=extra)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed ({what}): {d['context_errors']}")
    for ctx in ALL_CONTEXTS:
        for section in ("canvas", "canvasNoise"):
            s = d["contexts"].get(ctx, {}).get(section)
            if not isinstance(s, dict) or "error" in s:
                raise GuardFailure(f"{what}: {ctx} has no {section} section: {s}")
    return d


def canvas_keys(d: dict, ctx: str) -> dict:
    c = d["contexts"][ctx]
    return flatten({"canvas": c["canvas"], "canvasNoise": c["canvasNoise"]})


def changed_pixels(reference: str, noised: str) -> tuple[int, list[str]]:
    a, b = base64.b64decode(reference), base64.b64decode(noised)
    if len(a) != len(b):
        raise GuardFailure(f"scene sizes differ: {len(a)} and {len(b)} bytes")
    changed, bad = 0, []
    for i in range(0, len(a), 4):
        pa, pb = a[i:i + 4], b[i:i + 4]
        if pa == pb:
            continue
        changed += 1
        deltas = [abs(x - y) for x, y in zip(pa, pb)]
        alpha_step = deltas[3] == 1 and pa[3] not in (0, 255)
        one_channel = deltas[3] == 0 and sorted(deltas) == [0, 0, 0, 1]
        if not (alpha_step or one_channel) and len(bad) < 5:
            bad.append(f"pixel {i // 4}: {list(pa)} -> {list(pb)}")
    return changed, bad


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    off = write_profile(profile, work, "noise-off", False, SEED_1)
    off2 = write_profile(profile, work, "noise-off-s2", False, SEED_2)
    on1 = write_profile(profile, work, "noise-s1", True, SEED_1)
    on2 = write_profile(profile, work, "noise-s2", True, SEED_2)

    r0 = checked_dump(args.binary, None, headless, "R0, no profile")
    r1 = checked_dump(args.binary, off, headless, "R1, canvas.noise false")
    r1b = checked_dump(args.binary, off2, headless, "R1', canvas.noise false, seed S2")
    r2 = checked_dump(args.binary, on1, headless, "R2, canvas.noise true")

    # 0. Precondition.
    unchanged = [c for c in ALL_CONTEXTS if r2["contexts"][c]["canvas"]["getImageData"] == r1["contexts"][c]["canvas"]["getImageData"]]
    if unchanged:
        raise GuardFailure(f"vacuous: canvas.noise true left getImageData unchanged in {unchanged}")
    print(f"PASS: precondition: canvas.noise true changes getImageData in {len(ALL_CONTEXTS)} contexts")

    # 1. Inert when off.
    windows = profile["os"]["family"] == "windows"
    for ctx in ALL_CONTEXTS:
        a, b = canvas_keys(r1, ctx), canvas_keys(r1b, ctx)
        diff = {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
        if diff:
            raise GuardFailure(f"canvas.noise false depends on the seed in {ctx}: {diff}")
    print(f"PASS: canvas.noise false: another seed changes no canvas key in {len(ALL_CONTEXTS)} contexts")
    for ctx in ALL_CONTEXTS:
        a, b = canvas_keys(r0, ctx), canvas_keys(r1, ctx)
        keys = sorted(k for k in set(a) | set(b) if not (windows and k in TEXT_KEYS))
        diff = {k: (a.get(k), b.get(k)) for k in keys if a.get(k) != b.get(k)}
        if diff:
            raise GuardFailure(f"canvas.noise false differs from no profile in {ctx}: {diff}")
    which = "every canvas key without text (a windows profile renders text as Windows)" if windows else "every canvas key"
    print(f"PASS: canvas.noise false equals no profile on {which} in {len(ALL_CONTEXTS)} contexts")

    # 2. Noise on, every context.
    for ctx in ALL_CONTEXTS:
        canvas, noise = r2["contexts"][ctx]["canvas"], r2["contexts"][ctx]["canvasNoise"]
        reference = r0["contexts"][ctx]["canvasNoise"]
        if not (canvas.get("deterministic") and canvas.get("subRectMatchesFull")):
            raise GuardFailure(f"{ctx}: deterministic={canvas.get('deterministic')} subRectMatchesFull={canvas.get('subRectMatchesFull')}")
        if "offscreenGetImageData" in canvas and canvas["offscreenGetImageData"] != canvas["getImageData"]:
            raise GuardFailure(f"{ctx}: OffscreenCanvas getImageData differs from the canvas element's")
        for key, want in reference.items():
            if (isinstance(want, bool) or key in EXACT_KEYS) and noise.get(key) != want:
                raise GuardFailure(f"{ctx}: canvasNoise.{key} is {noise.get(key)!r}, without noise {want!r}")
    print(f"PASS: noise on: deterministic, sub-rectangles, OffscreenCanvas and every detector invariant hold in {len(ALL_CONTEXTS)} contexts")
    if r2["contexts"]["main"]["canvasNoise"]["transparentText"] == r1["contexts"]["main"]["canvasNoise"]["transparentText"]:
        raise GuardFailure("text on a transparent canvas is unchanged with noise on: the alpha branch did nothing")
    print("PASS: text on a transparent canvas is noised (alpha branch)")
    for ctx in WINDOW_CONTEXTS:
        a, b = r1["contexts"][ctx]["canvas"], r2["contexts"][ctx]["canvas"]
        if (a.get("toBlob") == a.get("offscreenConvertToBlob")) != (b.get("toBlob") == b.get("offscreenConvertToBlob")):
            raise GuardFailure(f"{ctx}: toBlob and convertToBlob agreement differs from noise off")
        if b.get("toDataURL") == a.get("toDataURL") or b.get("toBlob") == a.get("toBlob"):
            raise GuardFailure(f"{ctx}: toDataURL or toBlob unchanged with noise on")
    print("PASS: toDataURL and toBlob are noised and agree with convertToBlob as without noise")
    report = cross_context_report(r2, baseline=r1)
    split = {k: v for k, v in report["disagreements"].items() if k.startswith(("canvas.", "canvasNoise."))}
    if split:
        raise GuardFailure(f"noise on adds cross-context splits: {sorted(split)}")
    print("PASS: no canvas split between contexts beyond the reference's")

    # 3. Seeds, restarts, magnitude.
    l1 = session(args.binary, off, headless, screenshot=True)
    l2 = session(args.binary, on1, headless, screenshot=True)
    l3 = session(args.binary, on2, headless, screenshot=False)
    l4 = session(args.binary, on1, headless, screenshot=False)
    for name, s in (("L1", l1), ("L2", l2), ("L3", l3), ("L4", l4)):
        for ctx in ("main", "worker"):
            if "error" in s[ctx]:
                raise GuardFailure(f"{name} {ctx}: {s[ctx]['error']}")
    for ctx in ("main", "worker"):
        if l3[ctx]["hash"] == l2[ctx]["hash"]:
            raise GuardFailure(f"{ctx}: seeds S1 and S2 give the same scene")
        if l4[ctx]["hash"] != l2[ctx]["hash"]:
            raise GuardFailure(f"{ctx}: the same seed gave another scene after a restart")
    print("PASS: the scene depends on the seed and is identical after a restart, main and worker")
    for ctx in ("main", "worker"):
        changed, bad = changed_pixels(l1[ctx]["pixels"], l2[ctx]["pixels"])
        fraction = changed / (WIDTH * HEIGHT)
        if bad:
            raise GuardFailure(f"{ctx}: changes larger than one step: {bad}")
        if not 0 < fraction < 0.02:
            raise GuardFailure(f"{ctx}: {changed} pixels ({fraction:.2%}) changed, expected more than 0 and under 2%")
        print(f"PASS: {ctx}: {changed} scene pixels ({fraction:.2%}) changed, each by one step")

    # 4. Nothing on screen changes.
    if l1["screenshot"] != l2["screenshot"]:
        raise GuardFailure("the on-page canvas looks different with noise on")
    print("PASS: the on-page canvas screenshot is byte-identical with noise on")


finish(main)
