"""Guard for patches/phasmid/display/system-colors.patch.

system_colors is the CSS system color set for display.prefers_color_scheme.
An element whose used color scheme is that one resolves every keyword from
it, deprecated keywords included; the other scheme keeps Chromium's own set;
the desktop theme (GTK, Qt) never shows. getComputedStyle is read for
elements with color-scheme normal, light and dark in the main frame, a
same-origin and a cross-origin iframe; OffscreenCanvas fillStyle in the page
and in a dedicated worker. Every run uses a desktop environment Chrome knows
no theme for, so "Chromium's own set" is the same on every host:

  0. precondition, no profile: Chromium's light and dark sets; the mutated
     sets below differ from both in every keyword;
  1. a light profile with a mutated set (aliases equal, a translucent
     Highlight): light and normal elements report it exactly, dark elements
     and -webkit-focus-ring-color report Chromium's; OffscreenCanvas
     SelectedItem and SelectedItemText follow the profile in the page and the
     worker, every other keyword stays as without a profile (stock's split);
  2. a dark profile with another set: dark elements report it, light and
     normal elements and OffscreenCanvas report Chromium's;
  3. headful, the desktop theme never reaches pages: XDG_CURRENT_DESKTOP=GNOME makes the GTK theme a new profile's default;
     it changes the values without a profile (precondition) and changes
     nothing in checks 1 and 2;
  4. malformed sets are refused at startup naming the field.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import WINDOW_CONTEXTS, GuardFailure, finish, load_profile, parse_args, run_binary

NAMES = ["Canvas", "CanvasText", "LinkText", "VisitedText", "ActiveText", "ButtonFace", "ButtonText", "ButtonBorder", "Field", "FieldText", "Highlight", "HighlightText", "SelectedItem", "SelectedItemText", "Mark", "MarkText", "GrayText", "AccentColor", "AccentColorText",
         "ActiveBorder", "ActiveCaption", "AppWorkspace", "Background", "ButtonHighlight", "ButtonShadow", "CaptionText", "InactiveBorder", "InactiveCaption", "InactiveCaptionText", "InfoBackground", "InfoText", "Menu", "MenuText", "Scrollbar", "ThreeDDarkShadow", "ThreeDFace", "ThreeDHighlight", "ThreeDLightShadow", "ThreeDShadow", "Window", "WindowFrame", "WindowText"]
# Keywords Blink resolves from one color provider entry; a profile must keep them equal.
ALIASES = {
    "ActiveBorder": "ButtonText", "ButtonBorder": "ButtonText", "InactiveBorder": "ButtonText", "ThreeDDarkShadow": "ButtonText",
    "ThreeDHighlight": "ButtonText", "ThreeDLightShadow": "ButtonText", "ThreeDShadow": "ButtonText", "WindowFrame": "ButtonText",
    "ButtonHighlight": "ButtonFace", "ButtonShadow": "ButtonFace", "ThreeDFace": "ButtonFace",
    "ActiveCaption": "Canvas", "AppWorkspace": "Canvas", "Background": "Canvas", "InactiveCaption": "Canvas", "InfoBackground": "Canvas",
    "Menu": "Canvas", "Scrollbar": "Canvas", "Window": "Canvas",
    "CaptionText": "CanvasText", "InfoText": "CanvasText", "MenuText": "CanvasText", "WindowText": "CanvasText",
}
SELECTION = ("SelectedItem", "SelectedItemText")
FOCUS_RING = "-webkit-focus-ring-color"
SCHEMES = ("normal", "light", "dark")
# A desktop Chrome has no theme for (base::nix::GetDesktopEnvironment: OTHER),
# and GNOME, whose new profiles default to the GTK theme.
NO_THEME = {"XDG_CURRENT_DESKTOP": "X-Phasmid-Guard", "DESKTOP_SESSION": ""}
GTK = {"XDG_CURRENT_DESKTOP": "GNOME", "DESKTOP_SESSION": ""}

SCRIPT = """(args) => {
  const [names, schemes, focusRing] = args;
  const out = {};
  for (const scheme of schemes) {
    const el = document.createElement("span");
    el.style.colorScheme = scheme;
    document.body.appendChild(el);
    const r = {};
    for (const n of names) {
      el.style.color = "";
      el.style.color = n;
      r[n] = el.style.color === "" ? null : getComputedStyle(el).color;
    }
    el.style.color = "";
    el.style.outlineColor = focusRing;
    r[focusRing] = el.style.outlineColor === "" ? null : getComputedStyle(el).outlineColor;
    el.remove();
    out[scheme] = r;
  }
  return out;
}"""

OFFSCREEN = """async (names) => {
  const body = `const names = ${JSON.stringify(names)};
    const fill = () => { const c = new OffscreenCanvas(1, 1).getContext("2d"); const r = {};
      for (const n of names) { c.fillStyle = "#010203"; c.fillStyle = n; r[n] = c.fillStyle; } return r; };`;
  const page = new Function(body + "return fill();")();
  const w = new Worker(URL.createObjectURL(new Blob([body + "postMessage(fill());"], { type: "text/javascript" })));
  const worker = await new Promise((res, rej) => {
    w.onmessage = (e) => res(e.data);
    w.onerror = (e) => rej(new Error("worker: " + e.message));
    setTimeout(() => rej(new Error("worker timeout")), 10000);
  });
  w.terminate();
  return { page, worker };
}"""

_ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""


def probe(binary: str, profile: Path | None, *, headless: bool, env: dict) -> dict:
    """Computed system colors per scheme in each window context, and the
    OffscreenCanvas answers in the page and a dedicated worker."""
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, env=env)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            args = [NAMES, list(SCHEMES), FOCUS_RING]
            out = {"main": page.evaluate(SCRIPT, args)}
            for slot, origin in (("iframe_same", servers.a), ("iframe_cross", servers.b)):
                page.evaluate(_ADD_FRAME, f"{origin.url}/h/guard-{slot}")
                frame = next(f for f in page.frames if f.url.endswith(f"/h/guard-{slot}"))
                out[slot] = frame.evaluate(SCRIPT, args)
            out["offscreen"] = page.evaluate(OFFSCREEN, NAMES)
            return out
        finally:
            launch.stop()


def mutated(base: int) -> dict[str, str]:
    """A complete set no host produces: one color per independent keyword,
    aliases equal to their source, a translucent Highlight."""
    colors = {}
    for i, name in enumerate(n for n in NAMES if n not in ALIASES):
        colors[name] = f"rgb({base + i}, {(base * 3 + 7 * i) % 251}, {(base * 5 + 11 * i) % 241})"
    colors["Highlight"] = f"rgba({base + 1}, {base + 41}, {base + 81}, 0.4)"
    for name, same in ALIASES.items():
        colors[name] = colors[same]
    return {n: colors[n] for n in NAMES}


def canvas_form(css: str) -> str:
    """fillStyle's serialization of an opaque computed color: #rrggbb."""
    m = re.fullmatch(r"rgb\((\d+), (\d+), (\d+)\)", css)
    return "#" + "".join(f"{int(v):02x}" for v in m.groups()) if m else css


def write_profile(base: dict, work: Path, name: str, scheme: str, colors: dict | None) -> Path:
    p = copy.deepcopy(base)
    p["display"]["prefers_color_scheme"] = scheme
    if colors is None:
        p.pop("system_colors", None)
    else:
        p["system_colors"] = colors
    path = work / f"{name}.json"
    path.write_text(json.dumps(p))
    return path


def names_of(values: dict) -> dict:
    return {n: values[n] for n in NAMES}


def expect_equal(actual, expected, label: str) -> None:
    if actual != expected:
        if isinstance(actual, dict) and isinstance(expected, dict):
            diff = {k: (actual.get(k), expected.get(k)) for k in sorted(set(actual) | set(expected)) if actual.get(k) != expected.get(k)}
            raise GuardFailure(f"{label}: (got, expected) differ in {diff}")
        raise GuardFailure(f"{label}: got {actual!r}, expected {expected!r}")
    print(f"PASS: {label}")


def check_profile_run(res: dict, ref: dict, colors: dict, scheme: str, label: str) -> None:
    own = [s for s in SCHEMES if (s == "dark") == (scheme == "dark")]
    other = [s for s in SCHEMES if s not in own]
    for ctx in WINDOW_CONTEXTS:
        for s in own:
            expect_equal(names_of(res[ctx][s]), colors, f"{label}: {ctx}, color-scheme {s} elements report system_colors")
            expect_equal(res[ctx][s][FOCUS_RING], ref[ctx][s][FOCUS_RING], f"{label}: {ctx}, color-scheme {s} {FOCUS_RING} is Chromium's")
        for s in other:
            expect_equal(res[ctx][s], ref[ctx][s], f"{label}: {ctx}, color-scheme {s} elements keep Chromium's set")
    # Without a color provider Blink resolves SelectedItem, SelectedItemText,
    # HighlightText and (blended with white) Highlight from the active
    # selection colors, which a light profile's set supplies, as GTK's do in
    # stock; everything else there is Chromium's constant table.
    for where in ("page", "worker"):
        got, want = dict(res["offscreen"][where]), dict(ref["offscreen"][where])
        if scheme == "light":
            want.update({n: canvas_form(colors[n]) for n in SELECTION}, HighlightText=canvas_form(colors["SelectedItemText"]))
            if got["Highlight"] == ref["offscreen"][where]["Highlight"]:
                raise GuardFailure(f"{label}: OffscreenCanvas Highlight in the {where} ignores the profile's SelectedItem")
            want["Highlight"] = got["Highlight"]
        expect_equal(got, want, f"{label}: OffscreenCanvas fillStyle in the {where}")


def expect_refused(binary: str, profile_path: Path, work: Path, field: str) -> None:
    try:
        r = run_binary(binary, [f"--phasmid-profile={profile_path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=30)
    except subprocess.TimeoutExpired:
        raise GuardFailure(f"{profile_path.name}: expected a refusal naming {field}, but the browser started") from None
    if r.returncode == 0 or f"'{field}'" not in r.stderr:
        raise GuardFailure(f"{profile_path.name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {profile_path.name} refused at startup naming {field}")


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    ref = probe(args.binary, None, headless=headless, env=NO_THEME)
    light, dark = mutated(10), mutated(150)
    for ctx in WINDOW_CONTEXTS:
        for s in SCHEMES:
            for colors in (light, dark):
                clash = [n for n in NAMES if colors[n] in (ref[ctx]["light"][n], ref[ctx]["dark"][n])]
                if clash or None in ref[ctx][s].values():
                    raise GuardFailure(f"vacuous: {ctx} {s}: mutated keywords {clash} equal Chromium's, or a keyword did not resolve: {ref[ctx][s]}")
    print("PASS: precondition: the mutated sets differ from Chromium's light and dark sets in every keyword")

    light_path = write_profile(base, work, "light", "light", light)
    dark_path = write_profile(base, work, "dark", "dark", dark)
    light_run = probe(args.binary, light_path, headless=headless, env=NO_THEME)
    check_profile_run(light_run, ref, light, "light", "light profile")
    dark_run = probe(args.binary, dark_path, headless=headless, env=NO_THEME)
    check_profile_run(dark_run, ref, dark, "dark", "dark profile")

    if headless:
        print("NOTE: headless loads no desktop theme; the GTK checks run with --headful")
    else:
        gtk_ref = probe(args.binary, None, headless=False, env=GTK)
        if gtk_ref["main"]["light"] == ref["main"]["light"]:
            raise GuardFailure("vacuous: XDG_CURRENT_DESKTOP=GNOME did not change the light set without a profile (no GTK theme on this host?)")
        changed = sorted(k for k, v in gtk_ref["main"]["light"].items() if v != ref["main"]["light"][k])
        print(f"PASS: precondition: the GTK theme changes {changed} without a profile")
        expect_equal(probe(args.binary, light_path, headless=False, env=GTK), light_run, "light profile under the GTK theme: every value as without it")
        expect_equal(probe(args.binary, dark_path, headless=False, env=GTK), dark_run, "dark profile under the GTK theme: every value as without it")

    def bad(name: str, field: str, edit, scheme: str = "light") -> None:
        colors = dict(light)
        edit(colors)
        expect_refused(args.binary, write_profile(base, work, name, scheme, colors), work, field)

    expect_refused(args.binary, write_profile(base, work, "no-system-colors", "light", None), work, "system_colors")
    bad("missing-keyword", "system_colors.Canvas", lambda c: c.pop("Canvas"))
    bad("unknown-keyword", "system_colors.Text", lambda c: c.update(Text="rgb(0, 0, 0)"))
    bad("hex", "system_colors.LinkText", lambda c: c.update(LinkText="#0000ee"))
    bad("unspaced", "system_colors.LinkText", lambda c: c.update(LinkText="rgb(0,0,238)"))
    bad("out-of-range", "system_colors.LinkText", lambda c: c.update(LinkText="rgb(0, 0, 256)"))
    bad("opaque-rgba", "system_colors.LinkText", lambda c: c.update(LinkText="rgba(0, 0, 238, 1)"))
    bad("non-canonical-alpha", "system_colors.Highlight", lambda c: c.update(Highlight="rgba(0, 65, 198, 0.80)"))
    bad("alias-differs", "system_colors.ButtonBorder", lambda c: c.update(ButtonBorder="rgb(1, 2, 3)"))
    bad("bad-scheme", "display.prefers_color_scheme", lambda c: None, scheme="no-preference")


finish(main)
