"""One profile from (os, seed, languages, timezone), or (os, seed, exit IP).

BrowserForge supplies popularity weights for the values it observes (OS
identity, concurrency, memory, touch, screen and window geometry, media
device presence, battery). Every node of its network is a child of the
user-agent string alone, so the joint is enforced here: each node is drawn
for one sampled UA, restricted to the values the conventions below and the
chosen capture allow. Everything else, the GPU, fonts, voices, audio, codecs,
system colours, pointer and preferences, comes from one capture in
profiles/data/captures/. The GPU's identity may instead be one the capture's
host class can claim, drawn by fpgen support (claims.py); its surface is
still the capture's. Without a capture for the OS, generation stops and
names the missing fields; nothing is filled in.

No user-agent, brand or version string is copied: the schema has no field
for one, and the browser builds them from its own version.

With a proxy exit IP the location group comes from it (location.py): the zone,
the served position, the WebRTC public address and, unless given, the
languages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import chromium_locales, forge
from .captures import Capture, for_os
from .claims import Claim, claims, derive, note
from .location import Location

FAMILIES = ("linux", "windows")
SEED_RE = re.compile(r"[0-9a-f]{32}")
# The reduced desktop Chrome UA; Edge, Opera and mobile UAs do not match.
_CHROME_UA = re.compile(r"Mozilla/5\.0 \(([^)]*)\) AppleWebKit/537\.36 \(KHTML, like Gecko\) Chrome/(\d+)\.0\.0\.0 Safari/537\.36")
_GREASE_BRAND = re.compile(r"Not.A.Brand")
_NEWEST_MAJORS = 8
_MAX_ATTEMPTS = 16
# navigator.deviceMemory buckets: powers of two inside the desktop clamp at 155.
_DEVICE_MEMORY = (2, 4, 8, 16, 32)
# Windows taskbar height in CSS px by marketing version (BrowserForge Windows Chrome:
# height - availHeight is 48 for 59% and 40 for 16% of the mass).
_WINDOWS_TASKBAR = {"11": 48, "10": 40}


@dataclass(frozen=True)
class Convention:
    ua_platform_token: str
    navigator_platform: str
    ch_platform: str
    platform_version: re.Pattern[str]
    dpr_steps: tuple[float, ...]
    color_depths: dict[int, bool] = field(default_factory=dict)  # colorDepth -> the hasHDR it occurs with
    depth_reported_as: dict[int, int] = field(default_factory=dict)  # recorded colorDepth -> what Chrome 155 reports


CONVENTIONS = {
    # Stock Chrome 153 on Linux reports platformVersion "" (profiles/data/captures).
    # Scale steps of 25%; no HDR or 30-bit without a capture that shows it.
    "linux": Convention("X11; Linux x86_64", "Linux x86_64", "Linux", re.compile(r""),
                        (1, 1.25, 1.5, 1.75, 2), {24: False}),
    # platformVersion 0.x is Windows 7/8.1, unsupported since Chrome 110. Display scale in
    # 25% steps. Chrome 155 builds every Windows display with 24 bits per pixel in SDR
    # (ui/display/win/screen_win.cc), so a recorded 32 is reported as 24; 30 comes only
    # with HDR, whose colour gamut is not modelled.
    "windows": Convention("Windows NT 10.0; Win64; x64", "Win32", "Windows", re.compile(r"[1-9]\d*\.0\.0"),
                          (1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 3), {24: False, 32: False}, {32: 24}),
}

# What only a capture can supply. A draft without a capture lacks exactly these.
CAPTURED_FIELDS = (
    "gpu", "audio", "fonts", "speech", "media", "webauthn", "system_colors",
    "capabilities.bluetooth", "capabilities.web_share",
    "display.pointer", "display.prefers_color_scheme", "display.prefers_reduced_motion",
    "display.prefers_contrast", "display.forced_colors", "display.window.inner_width",
    "display.window.inner_height", "hardware.physical_memory_mb", "hardware.js_heap_size_limit",
    "hardware.disk_total_bytes", "media_devices.labels", "network.connection",
)


class GenerationError(Exception):
    pass


class MissingCaptures(GenerationError):
    def __init__(self, family: str, fields: list[str], draft: dict[str, Any]):
        self.family = family
        self.fields = fields
        self.draft = draft
        super().__init__(
            f"no capture under profiles/data/captures for os={family}; these cannot be generated "
            f"and are never invented: {', '.join(fields)}"
        )


def dumps(profile: dict[str, Any]) -> str:
    return json.dumps(profile, indent=1, sort_keys=True) + "\n"


@dataclass(frozen=True)
class Choice:
    capture: Capture
    claim: Claim
    device_id: str


def _choose_capture(family: str, seed: str, gpu: str | None) -> Choice | None:
    """A capture, then the GPU it claims, weighted by fpgen support (claims.py)."""
    captures = for_os(family)
    if gpu is not None:
        captures = [c for c in captures if any(gpu in claim.gpu_keys for claim in claims(c))]
        if not captures:
            raise GenerationError(f"no capture for os={family} backs gpu {gpu}")
    if not captures:
        return None
    capture = forge.decision_rng(seed, "capture").choice(captures)
    options = [claim for claim in claims(capture) if gpu is None or gpu in claim.gpu_keys]
    claim = forge.decision_rng(seed, "gpu-claim").choices(options, weights=[c.weight for c in options])[0]
    device_id = gpu.split(":", 1)[1] if gpu else forge.decision_rng(seed, "gpu-device-id").choice(claim.device_ids)
    return Choice(capture, claim, device_id)


def _ua_pool(conv: Convention) -> set[str]:
    """Desktop Chrome UAs for the OS from the newest majors BrowserForge has."""
    matches = {ua: m for ua in forge.user_agents() if (m := _CHROME_UA.fullmatch(ua)) and m.group(1) == conv.ua_platform_token}
    majors = sorted({int(m.group(2)) for m in matches.values()}, reverse=True)[:_NEWEST_MAJORS]
    return {ua for ua, m in matches.items() if int(m.group(2)) in majors}


def _os_version(family: str, platform_version: str, capture: Capture | None) -> str | None:
    if family == "windows":
        return "11" if int(platform_version.split(".")[0]) >= 13 else "10"
    return capture.profile["os"]["version"] if capture else None


def _ua_data_ok(conv: Convention, d: Any) -> bool:
    if not isinstance(d, dict):
        return False
    brands = {b.get("brand") for b in d.get("brands") or [] if not _GREASE_BRAND.fullmatch(b.get("brand", ""))}
    return (
        brands == {"Chromium", "Google Chrome"}
        and d.get("mobile") is False
        and d.get("platform") == conv.ch_platform
        and d.get("architecture") == "x86"
        and d.get("bitness") == "64"
        and d.get("model") == ""
        and isinstance(d.get("platformVersion"), str)
        and conv.platform_version.fullmatch(d["platformVersion"]) is not None
    )


def _work_area_ok(family: str, os_version: str | None, s: dict[str, Any]) -> bool:
    """Primary monitor with the OS's work-area convention."""
    if s["availLeft"] != 0 or s["availWidth"] != s["width"]:
        return False
    if family == "linux":  # bare Linux reports no panel: the work area is the screen
        return s["availTop"] == 0 and s["availHeight"] == s["height"]
    if family == "windows":  # taskbar at the bottom
        return s["availTop"] == 0 and s["height"] - s["availHeight"] == _WINDOWS_TASKBAR.get(os_version or "", -1)
    return s["availTop"] > 0 and s["availTop"] + s["availHeight"] <= s["height"]  # menu bar, dock at the bottom


def _screen_ok(family: str, conv: Convention, os_version: str | None, s: Any) -> bool:
    if not isinstance(s, dict):
        return False
    return (
        s["width"] > s["height"] and s["width"] >= 1280 and s["height"] >= 720  # landscape desktop panel
        and s["devicePixelRatio"] in conv.dpr_steps  # drops page zoom leaking into DPR
        and conv.color_depths.get(s["colorDepth"]) == s["hasHDR"]
        and _work_area_ok(family, os_version, s)
        # The window starts at the work area's left edge and fits inside it; its top
        # (BrowserForge has no screenY) is then the work area's top.
        and s["screenX"] == s["availLeft"]
        and 200 <= s["outerWidth"] <= s["availWidth"]
        and 200 <= s["outerHeight"] <= s["availHeight"]
    )


def _touch_ok(capture: Capture | None, t: Any) -> bool:
    """maxTouchPoints agrees with the captured pointer (Display group)."""
    if not isinstance(t, int) or not 0 <= t <= 20:
        return False
    if capture is None:
        return True
    return (t > 0) == ("coarse" in capture.profile["display"]["pointer"]["any_pointer"])


def _battery_ok(capture: Capture | None, b: Any) -> bool:
    """A captured device without a battery (charging, level 1) only takes the same."""
    if not isinstance(b, dict) or not isinstance(b.get("charging"), bool) or not isinstance(b.get("level"), (int, float)):
        return False
    if capture is None or capture.battery is None:
        return True
    no_battery = capture.battery.get("charging") is True and capture.battery.get("level") == 1
    return (b["charging"] is True and b["level"] == 1) == no_battery


# BrowserForge's multimediaDevices lists against the profile's device kinds.
_DEVICE_KINDS = (("micros", "audioinput"), ("speakers", "audiooutput"), ("webcams", "videoinput"))


def _devices_ok(capture: Capture | None, m: Any) -> bool:
    """No more devices of a kind than the capture has labels for: a claimed device needs one."""
    if not isinstance(m, dict):
        return False
    if capture is None:
        return True
    return all(len(m.get(bf) or []) <= len(capture.media_device_labels.get(kind, [])) for bf, kind in _DEVICE_KINDS)


def _sample(family: str, seed: str, capture: Capture | None) -> dict[str, Any]:
    conv = CONVENTIONS[family]
    pool = _ua_pool(conv)
    banned: set[str] = set()
    for attempt in range(_MAX_ATTEMPTS):
        ua = forge.draw(seed, f"userAgent@{attempt}", "userAgent", {}, lambda u: u in pool and u not in banned)
        if ua is None:
            break
        parents = {"userAgent": ua}
        picks: dict[str, Any] = {"userAgent": ua}

        def pick(name: str, accept) -> bool:
            picks[name] = forge.draw(seed, f"{name}@{attempt}", name, parents, accept)
            return picks[name] is not None

        ok = pick("userAgentData", lambda d: _ua_data_ok(conv, d))
        os_version = _os_version(family, picks["userAgentData"]["platformVersion"], capture) if ok else None
        ok = (
            ok
            and pick("platform", lambda p: p == conv.navigator_platform)
            and pick("deviceMemory", lambda m: m in _DEVICE_MEMORY)
            # Odd counts are Brave's farbling, which shares Chrome's UA in this network.
            and pick("hardwareConcurrency", lambda c: isinstance(c, int) and c >= 2 and c % 2 == 0)
            and pick("maxTouchPoints", lambda t: _touch_ok(capture, t))
            and pick("screen", lambda s: _screen_ok(family, conv, os_version, s))
            and pick("multimediaDevices", lambda m: _devices_ok(capture, m))
            and pick("battery", lambda b: _battery_ok(capture, b))
        )
        if ok:
            picks["os_version"] = os_version
            return picks
        banned.add(ua)
    raise GenerationError(f"no BrowserForge sample for os={family} passes the filters")


def generate(family: str, seed: str, languages: list[str], timezone: str, gpu: str | None = None,
             location: Location | None = None, location_note: str = "") -> dict[str, Any]:
    """``location``, when given, sets the zone, position and WebRTC address; ``timezone`` must
    then be its zone. ``location_note`` names the data it came from."""
    if location is not None and timezone != location.timezone:
        raise GenerationError(f"timezone {timezone} differs from the exit IP's zone {location.timezone}")
    if family not in FAMILIES:
        raise GenerationError(f"os must be one of {', '.join(FAMILIES)}")
    if not SEED_RE.fullmatch(seed):
        raise GenerationError("seed must be 32 lowercase hex characters")
    choice = _choose_capture(family, seed, gpu)
    capture = choice.capture if choice else None
    p = _sample(family, seed, capture)
    uad, s, devices = p["userAgentData"], p["screen"], p["multimediaDevices"]
    token = _CHROME_UA.fullmatch(p["userAgent"]).group(1)  # type: ignore[union-attr]
    hdr = bool(s["hasHDR"])

    profile: dict[str, Any] = {
        "schema_version": 1,
        "id": f"gen-{family}-{s['width']}x{s['height']}-{seed[:8]}",
        "seed": seed,
        "captured": {
            "note": (
                "Generated by tools/genprofile, not captured. os, hardware.concurrency, device_memory, "
                "max_touch_points, display geometry, media device presence and battery are a BrowserForge "
                f"sample ({forge.provenance()})"
                + (f"; device data from capture {capture.name}" if capture else "")
                + (f"; {note(choice.capture, choice.claim, choice.device_id)}" if choice and choice.claim.derived else "")
                + (_location_note(location, languages, location_note) if location else "; locale from the command line.")
            ),
        },
        "os": {
            "family": family,
            "ua_platform_token": token,
            "navigator_platform": p["platform"],
            "ch_platform": uad["platform"],
            "ch_platform_version": uad["platformVersion"],
            "architecture": uad["architecture"],
            "bitness": uad["bitness"],
            "wow64": False,  # 64-bit Chrome on a 64-bit OS; BrowserForge records no wow64
            "model": uad["model"],
            "form_factors": ["Desktop"],  # BrowserForge records no formFactors; mobile is false
        },
        "hardware": {
            "concurrency": p["hardwareConcurrency"],
            "device_memory": p["deviceMemory"],
            "max_touch_points": p["maxTouchPoints"],
        },
        "display": {
            "width": s["width"],
            "height": s["height"],
            "avail_width": s["availWidth"],
            "avail_height": s["availHeight"],
            "avail_left": s["availLeft"],
            "avail_top": s["availTop"],
            "color_depth": CONVENTIONS[family].depth_reported_as.get(s["colorDepth"], s["colorDepth"]),
            "device_pixel_ratio": s["devicePixelRatio"],
            "is_extended": False,  # primary monitor only
            "orientation": {"type": "landscape-primary", "angle": 0},
            "window": {
                "outer_width": s["outerWidth"],
                "outer_height": s["outerHeight"],
                "screen_x": s["screenX"],
                "screen_y": s["availTop"],
            },
            "color_gamut": "p3" if hdr else "srgb",
            "dynamic_range": "high" if hdr else "standard",
        },
        "canvas": {"noise": family != "linux"},  # only when the Linux host cannot render the claimed OS
        "locale": {"languages": languages, "timezone": timezone},
        "media_devices": {kind: len(devices.get(bf) or []) for bf, kind in _DEVICE_KINDS},
        "capabilities": {"battery": {"charging": p["battery"]["charging"], "level": p["battery"]["level"]}},
        "network": {"webrtc": {"ip_handling_policy": "default"}},
        "automation": {"report_headful": True},
    }
    if p["os_version"] is not None:
        profile["os"]["version"] = p["os_version"]
    if location is not None:
        profile["locale"]["geolocation"] = location.geolocation()
        profile["network"]["webrtc"]["public_ip"] = location.public_ip

    if choice is None:
        missing = list(CAPTURED_FIELDS) + ([] if p["os_version"] is not None else ["os.version"])
        raise MissingCaptures(family, missing, profile)

    _fill_from_capture(profile, choice)
    return profile


def _location_note(location: Location, languages: list[str], data: str) -> str:
    source = (f"CLDR territoryInfo through Chromium {chromium_locales.TAG}'s default accept-languages of the UI locale"
              if languages == location.languages else "the command line")
    return (
        f"; locale from the proxy exit IP: {location.city}, {location.country} in {data}; zone of the city "
        f"coordinate (its country's own where the boundary is another territory's), the position drawn inside "
        f"the accuracy circle around it and in the same boundary; "
        f"languages from {source}."
    )


def _fill_from_capture(profile: dict[str, Any], choice: Choice) -> None:
    capture = choice.capture
    cap = capture.profile
    for section in ("gpu", "audio", "fonts", "speech", "media", "webauthn", "system_colors"):
        profile[section] = cap[section]
    if choice.claim.derived:
        profile["gpu"] = derive(cap["gpu"], choice.claim, choice.device_id)
    profile["capabilities"] = {**cap["capabilities"], **profile["capabilities"]}
    # The network quality the capture's page saw; BrowserForge records none.
    if "connection" in cap["network"]:
        profile["network"]["connection"] = cap["network"]["connection"]
    profile["captured"].update({"date": capture.date, "dump_sha256": capture.dump_sha256})
    # The captured host's devices, its first ones where the sample has fewer.
    devices = profile["media_devices"]
    labels = {kind: capture.media_device_labels[kind][: devices[kind]] for _bf, kind in _DEVICE_KINDS if devices[kind]}
    if labels:
        devices["labels"] = labels

    display, cap_display = profile["display"], cap["display"]
    for key in ("pointer", "prefers_color_scheme", "prefers_reduced_motion", "prefers_contrast", "forced_colors"):
        display[key] = cap_display[key]
    # Browser chrome around the viewport is per OS and DPR; reuse the captured delta only at the captured DPR.
    window, cap_window = display["window"], cap_display["window"]
    if display["device_pixel_ratio"] == cap_display["device_pixel_ratio"] and "inner_width" in cap_window:
        window["inner_width"] = window["outer_width"] - (cap_window["outer_width"] - cap_window["inner_width"])
        window["inner_height"] = window["outer_height"] - (cap_window["outer_height"] - cap_window["inner_height"])

    hardware, cap_hardware = profile["hardware"], cap["hardware"]
    if "disk_total_bytes" in cap_hardware:
        hardware["disk_total_bytes"] = cap_hardware["disk_total_bytes"]
    # RAM and the V8 heap limit follow physical memory: only the captured bucket can reuse them.
    if hardware["device_memory"] == cap_hardware["device_memory"]:
        for key in ("physical_memory_mb", "js_heap_size_limit"):
            if key in cap_hardware:
                hardware[key] = cap_hardware[key]
