"""Guard for patches/phasmid/display/media-queries.patch.

pointer, hover, any-pointer, any-hover, color-gamut, dynamic-range,
prefers-color-scheme, prefers-reduced-motion, prefers-contrast and
forced-colors follow display.*, navigator.maxTouchPoints follows
hardware.max_touch_points, and the Sec-CH-Prefers-Color-Scheme and
Sec-CH-Prefers-Reduced-Motion hints agree, in the main frame and both
iframes (matchMedia and maxTouchPoints do not exist in workers). Two
devices of the profile's OS that no host or stock headless run looks like:

  touch: a touchscreen only (pointer coarse, hover none, 10 touch points),
         dark, reduced motion, more contrast (no preference on Windows,
         which sets prefers-contrast only with forced colors), a p3 HDR
         screen;
  both:  mouse and touchscreen (pointer fine, any-pointer fine and coarse,
         5 touch points), forced colors with more contrast, a rec2020 screen.

The precondition is a run without a profile that differs from the claims on
every feature checked. Then values Chrome cannot report on the profile's OS
are refused at startup, naming the field; on Windows that includes more
contrast without forced colors.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from helpers import WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, expect_header, finish, load_profile, parse_args, run_binary

from consistency.compare import _get

DEVICES = {
    "touch": {
        "display": {
            "pointer": {"primary": "coarse", "hover": "none", "any_pointer": ["coarse"], "any_hover": []},
            "color_gamut": "p3",
            "dynamic_range": "high",
            "prefers_color_scheme": "dark",
            "prefers_reduced_motion": "reduce",
            "prefers_contrast": "more",
            "forced_colors": "none",
        },
        "max_touch_points": 10,
    },
    "both": {
        "display": {
            "pointer": {"primary": "fine", "hover": "hover", "any_pointer": ["fine", "coarse"], "any_hover": ["hover"]},
            "color_gamut": "rec2020",
            "dynamic_range": "standard",
            "prefers_color_scheme": "light",
            "prefers_reduced_motion": "no-preference",
            "prefers_contrast": "more",
            "forced_colors": "active",
        },
        "max_touch_points": 5,
    },
}

# The features the precondition must see differ from the host in at least one
# device, so no check below can pass on host values.
PROVEN = (
    "mediaQueries.pointerCoarse", "mediaQueries.hoverNone", "mediaQueries.anyPointerCoarse",
    "mediaQueries.anyHoverNone", "mediaQueries.colorGamutP3", "mediaQueries.colorGamutRec2020",
    "mediaQueries.dynamicRangeHigh", "mediaQueries.prefersColorSchemeDark", "mediaQueries.prefersReducedMotion",
    "mediaQueries.prefersContrastMore", "mediaQueries.forcedColorsActive", "navigator.maxTouchPoints",
)


def devices(os_family: str) -> dict:
    """DEVICES as the profile's OS can report them: Windows sets
    prefers-contrast only with forced colors (core/profile.patch
    ParseMediaFeatures), so the touch device has no contrast preference."""
    out = copy.deepcopy(DEVICES)
    if os_family == "windows":
        out["touch"]["display"]["prefers_contrast"] = "no-preference"
    return out


def make_profile(base: dict, device: dict) -> dict:
    profile = copy.deepcopy(base)
    profile["display"].update(copy.deepcopy(device["display"]))
    profile["hardware"]["max_touch_points"] = device["max_touch_points"]
    return profile


def expected(device: dict) -> dict:
    """What matchMedia and navigator report for the device, by dump key."""
    d = device["display"]
    p = d["pointer"]
    gamut = d["color_gamut"]
    return {
        "mediaQueries.pointerFine": p["primary"] == "fine",
        "mediaQueries.pointerCoarse": p["primary"] == "coarse",
        "mediaQueries.pointerNone": p["primary"] == "none",
        "mediaQueries.hoverHover": p["hover"] == "hover",
        "mediaQueries.hoverNone": p["hover"] == "none",
        "mediaQueries.anyPointerFine": "fine" in p["any_pointer"],
        "mediaQueries.anyPointerCoarse": "coarse" in p["any_pointer"],
        "mediaQueries.anyPointerNone": not p["any_pointer"],
        "mediaQueries.anyHoverHover": "hover" in p["any_hover"],
        "mediaQueries.anyHoverNone": not p["any_hover"],
        "mediaQueries.colorGamutSrgb": True,
        "mediaQueries.colorGamutP3": gamut in ("p3", "rec2020"),
        "mediaQueries.colorGamutRec2020": gamut == "rec2020",
        "mediaQueries.dynamicRangeHigh": d["dynamic_range"] == "high",
        "mediaQueries.dynamicRangeStandard": True,
        # CSSVideoDynamicRangeMediaQueries is "experimental" in 155, so stock
        # Chrome does not parse video-dynamic-range and never matches it.
        "mediaQueries.videoDynamicRangeHigh": False,
        "mediaQueries.prefersColorSchemeDark": d["prefers_color_scheme"] == "dark",
        "mediaQueries.prefersColorSchemeLight": d["prefers_color_scheme"] == "light",
        "mediaQueries.prefersReducedMotion": d["prefers_reduced_motion"] == "reduce",
        "mediaQueries.prefersContrastMore": d["prefers_contrast"] == "more",
        "mediaQueries.prefersContrastLess": d["prefers_contrast"] == "less",
        "mediaQueries.prefersContrastNoPreference": d["prefers_contrast"] == "no-preference",
        "mediaQueries.forcedColorsActive": d["forced_colors"] == "active",
        "navigator.maxTouchPoints": device["max_touch_points"],
    }


def check_precondition(plain: dict, devs: dict) -> None:
    if plain["context_errors"]:
        raise GuardFailure(f"precondition: contexts failed: {plain['context_errors']}")
    host = plain["contexts"]["main"]
    same = [key for key in PROVEN if all(_get(host, key) == expected(device)[key] for device in devs.values())]
    if same:
        raise GuardFailure(f"vacuous: the host already reports what every device claims for {same}")
    shown = ", ".join(f"{key.split('.')[-1]}={_get(host, key)}" for key in PROVEN)
    print(f"PASS: precondition, without a profile: {shown}")


def check_device(d: dict, name: str, device: dict) -> None:
    if d["context_errors"]:
        raise GuardFailure(f"{name}: contexts failed: {d['context_errors']}")
    for key, want in expected(device).items():
        expect_everywhere(d, key, want, contexts=WINDOW_CONTEXTS, label=f"{name}: {key}")
    expect_header(d, "navigation_with_hints", "sec-ch-prefers-color-scheme", device["display"]["prefers_color_scheme"])
    expect_header(d, "navigation_with_hints", "sec-ch-prefers-reduced-motion", device["display"]["prefers_reduced_motion"])


def expect_refused(binary: str, profile: dict, work: Path, name: str, field: str) -> None:
    path = work / f"refused-{name}.json"
    path.write_text(json.dumps(profile))
    r = run_binary(binary, [f"--phasmid-profile={path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {name} refused at startup naming {field}")


def refusals(base: dict, devs: dict) -> list[tuple[str, dict, str]]:
    """(name, profile, field the refusal must name). Each changes one thing
    in the touch device, which is itself valid."""
    out = []

    def case(name: str, field: str, mutate) -> None:
        profile = make_profile(base, devs["touch"])
        mutate(profile)
        out.append((name, profile, field))

    case("primary-fine-without-mouse", "display.pointer.primary", lambda p: p["display"]["pointer"].update(primary="fine"))
    case("coarse-without-touch-points", "hardware.max_touch_points", lambda p: p["hardware"].update(max_touch_points=0))
    case("touch-points-without-coarse", "hardware.max_touch_points",
         lambda p: p["display"]["pointer"].update(primary="none", any_pointer=[]))
    case("mouse-without-hover", "display.pointer.any_hover",
         lambda p: p["display"]["pointer"].update(primary="fine", any_pointer=["fine", "coarse"]))
    case("hover-without-any-hover", "display.pointer.hover", lambda p: p["display"]["pointer"].update(hover="hover"))
    case("duplicate-any-pointer", "display.pointer.any_pointer",
         lambda p: p["display"]["pointer"].update(any_pointer=["coarse", "coarse"]))
    case("unknown-pointer", "display.pointer.primary", lambda p: p["display"]["pointer"].update(primary="pen"))
    case("too-many-touch-points", "hardware.max_touch_points", lambda p: p["hardware"].update(max_touch_points=21))
    case("hdr-srgb", "display.color_gamut", lambda p: p["display"].update(color_gamut="srgb"))
    case("unknown-gamut", "display.color_gamut", lambda p: p["display"].update(color_gamut="adobe-rgb", dynamic_range="standard"))
    case("unknown-color-scheme", "display.prefers_color_scheme", lambda p: p["display"].update(prefers_color_scheme="no-preference"))
    case("contrast-less", "display.prefers_contrast", lambda p: p["display"].update(prefers_contrast="less"))
    if base["os"]["family"] == "windows":
        case("contrast-more-without-forced-colors-on-windows", "display.prefers_contrast",
             lambda p: p["display"].update(prefers_contrast="more", forced_colors="none"))
    case("forced-colors-without-more-contrast", "display.prefers_contrast",
         lambda p: p["display"].update(forced_colors="active", prefers_contrast="no-preference"))
    case("missing-reduced-motion", "display.prefers_reduced_motion", lambda p: p["display"].pop("prefers_reduced_motion"))
    return out


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    devs = devices(base["os"]["family"])
    check_precondition(dump(args.binary, None, headless=headless), devs)

    for name, device in devs.items():
        path = work / f"profile-{name}.json"
        path.write_text(json.dumps(make_profile(base, device)))
        check_device(dump(args.binary, str(path), headless=headless), name, device)

    for name, profile, field in refusals(base, devs):
        expect_refused(args.binary, profile, work, name, field)


finish(main)
