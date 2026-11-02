"""Guard for patches/phasmid/display/screen-window.patch.

screen.*, devicePixelRatio, the resolution media query, isExtended,
orientation and the window's outer size and position all follow display.*,
and the viewport measures display.window.inner_* when the profile gives it.
Two profiles are used: one at device pixel ratio 1 and one at 1.5, both with
a screen no host or stock headless run has (stock headless is 800x600 with a
780x580 window at 10,10).
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, finish, load_profile, parse_args

from consistency.compare import _get


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


finish(main)
