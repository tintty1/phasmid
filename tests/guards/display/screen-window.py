"""Guard for patches/phasmid/display/screen-window.patch.

screen.*, devicePixelRatio, the resolution media query, isExtended,
orientation and the window's outer size and position all follow display.*,
and the viewport measures display.window.inner_* when the profile gives it.
Two profiles are used: one at device pixel ratio 1 and one at 1.5, both with
a screen no host or stock headless run has (stock headless is 800x600 with a
780x580 window at 10,10).

Popups: a window opened by window.open as a popup reports its own window,
as stock Chrome does, never the profile's: outerWidth/outerHeight and
screenX/screenY equal the browser window's bounds (Browser.getWindowForTarget),
in the popup and in an iframe inside it, and at a whole device pixel ratio
the viewport is the requested width. Cases: a sized and placed popup (500x600
at 101,81, odd so at ratio 1.5 it is not on a whole pixel and stock Chrome
snaps it and widens the viewport by 2 px), the bare `popup`
feature, and in headless mode a popup larger than the screen, which stays in
the profile's work area (headful places windows on the host's screen). A tab
opened with _blank, and the opener after the popups, still report the
profile's window.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from helpers import WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, finish, load_profile, parse_args

from consistency.browser import Launch
from consistency.compare import _get

# What a page reads about its window, in the popup and in an iframe in it.
WINDOW_PROBE = """() => {
  const read = (w) => ({inner: [w.innerWidth, w.innerHeight], outer: [w.outerWidth, w.outerHeight],
                        pos: [w.screenX, w.screenY]});
  const frame = document.createElement('iframe');
  document.body.appendChild(frame);
  return {top: read(window), iframe: read(frame.contentWindow)};
}"""


def make_profile(base: dict, dpr: float) -> dict:
    profile = json.loads(json.dumps(base))
    profile["display"].update(
        width=1600,
        height=900,
        avail_left=0,
        avail_top=27,
        avail_width=1600,
        avail_height=873,
        color_depth=30,
        device_pixel_ratio=dpr,
        is_extended=True,
        orientation={"type": "landscape-primary", "angle": 0},
        window={
            "outer_width": 1500,
            "outer_height": 820,
            "screen_x": 33,
            "screen_y": 41,
            "inner_width": 1500,
            "inner_height": 733,
        },
    )
    return profile


def check(d: dict, dpr: float) -> None:
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    for key, want in (
        ("screen.width", 1600),
        ("screen.height", 900),
        ("screen.availLeft", 0),
        ("screen.availTop", 27),
        ("screen.availWidth", 1600),
        ("screen.availHeight", 873),
        ("screen.colorDepth", 30),
        ("screen.pixelDepth", 30),
        ("screen.devicePixelRatio", dpr),
        ("mediaQueries.resolutionDppx", dpr),
        ("screen.orientation.type", "landscape-primary"),
        ("screen.outerWidth", 1500),
        ("screen.outerHeight", 820),
        ("screen.screenX", 33),
        ("screen.screenY", 41),
    ):
        expect_everywhere(d, key, want, contexts=WINDOW_CONTEXTS, label=f"dpr {dpr}: {key}")
    expect_everywhere(d, "screen.isExtended", True, contexts=("main",), label=f"dpr {dpr}: screen.isExtended")
    expect_everywhere(d, "screen.innerWidth", 1500, contexts=("main",), label=f"dpr {dpr}: innerWidth")
    expect_everywhere(d, "screen.innerHeight", 733, contexts=("main",), label=f"dpr {dpr}: innerHeight")


def check_popups(binary: str, profile: Path, dpr: float, *, headless: bool) -> None:
    profile_window = {"outer": [1500, 820], "pos": [33, 41]}
    cases = [("sized popup", "width=500,height=600,left=101,top=81"), ("bare popup", "popup"), ("tab", None)]
    if headless:
        cases.append(("oversized popup", "width=4000,height=4000,left=3000,top=3000"))
    launch = Launch(binary=Path(binary), headless=headless, profile=profile)
    with sync_playwright() as pw:
        try:
            browser = launch.start(pw)
            context = browser.contexts[0]
            opener = context.pages[0]
            opener.goto("data:text/html,<title>opener</title>opener")
            for name, features in cases:
                label = f"dpr {dpr}: {name}"
                with context.expect_page() as info:
                    opener.evaluate("f => { window.open('about:blank', '_blank', ...(f ? [f] : [])); }", features)
                page = info.value
                page.wait_for_load_state()
                page.wait_for_timeout(500)
                seen = page.evaluate(WINDOW_PROBE)
                cdp = context.new_cdp_session(page)
                b = cdp.send("Browser.getWindowForTarget")["bounds"]
                cdp.detach()
                page.close()
                if seen["iframe"]["outer"] != seen["top"]["outer"] or seen["iframe"]["pos"] != seen["top"]["pos"]:
                    raise GuardFailure(f"{label}: the iframe reports another window than its page: {seen}")
                top = seen["top"]
                if features is None:
                    if {"outer": top["outer"], "pos": top["pos"]} != profile_window:
                        raise GuardFailure(f"{label}: expected the profile's window {profile_window}, got {top}")
                    print(f"PASS: {label} reports the profile's window {profile_window}, in its iframe too")
                    continue
                real = {"outer": [b["width"], b["height"]], "pos": [b["left"], b["top"]]}
                if {"outer": top["outer"], "pos": top["pos"]} != real:
                    raise GuardFailure(f"{label}: expected its own window {real} (Browser.getWindowForTarget), got {top}")
                if top["inner"][0] > top["outer"][0] or top["inner"][1] >= top["outer"][1]:
                    raise GuardFailure(f"{label}: viewport {top['inner']} does not fit in the window {top['outer']}")
                # At a fractional ratio stock Chrome too snaps an origin that is
                # not a whole pixel and widens the viewport (Chrome 156 headless
                # at 1.5: left=101 gives screenX 100 and a 502x601 viewport).
                if features.startswith("width=500") and dpr == int(dpr) and top["inner"][0] != 500:
                    raise GuardFailure(f"{label}: expected the requested 500 px viewport width, got {top['inner']}")
                if name == "oversized popup":
                    x, y, w, h = b["left"], b["top"], b["width"], b["height"]
                    if x < 0 or y < 27 or x + w > 1600 or y + h > 900:
                        raise GuardFailure(f"{label}: window {b} leaves the profile's work area 0,27 1600x873")
                print(f"PASS: {label} reports its own window {real}, viewport {top['inner']}, in its iframe too")
            after = opener.evaluate(WINDOW_PROBE)["top"]
            if {"outer": after["outer"], "pos": after["pos"]} != profile_window:
                raise GuardFailure(f"dpr {dpr}: the opener reports {after} after the popups, expected {profile_window}")
            print(f"PASS: dpr {dpr}: the opener still reports the profile's window after the popups")
        finally:
            launch.stop()


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)

    plain = dump(args.binary, None, headless=not args.headful)
    s = plain["contexts"]["main"]
    if _get(s, "screen.width") == 1600 and _get(s, "screen.outerWidth") == 1500:
        raise GuardFailure("vacuous: the host already reports the guard's screen and window")
    print(f"PASS: precondition, host screen is {_get(s, 'screen.width')}x{_get(s, 'screen.height')}, window {_get(s, 'screen.outerWidth')}x{_get(s, 'screen.outerHeight')}")

    for dpr in (1, 1.5):
        path = work / f"profile-dpr{dpr}.json"
        path.write_text(json.dumps(make_profile(base, dpr)))
        check(dump(args.binary, str(path), headless=not args.headful), dpr)
        check_popups(args.binary, path, dpr, headless=not args.headful)


finish(main)
