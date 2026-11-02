"""Guard for patches/phasmid/core/profile.patch.

Asserts the browser-side contract of the core patch:
  1. an invalid profile stops the browser with a non-zero exit and a message
     that names the field;
  2. a missing profile file stops the browser;
  3. a valid profile starts and reaches every context: hardware.concurrency,
     chosen to differ from the host, is read from every context;
  4. without --phasmid-profile the binary behaves as plain Chromium;
  5. a linux profile's capability claims (media.hevc, dolby_audio, playready,
     gpu.hardware_decode.*, webauthn.*, capabilities.bluetooth,
     bluetooth_available, web_share) are what pages see in every context
     where each API exists; values stock Linux Chrome never reports stop the
     browser naming the field; claims this build cannot back start with one
     "host cannot back" warning each; a battery that is not full, or not
     charging, starts;
  6. os.family macos stops the browser as an unknown value;
  7. a windows profile (made here from a linux profile) runs on this Linux
     host: exit 0, each warning logged once and, without a Windows font
     store, warnings for fonts.families (count, a family no host has, and
     names resolved only through a host alias such as Arimo),
     fonts.ui_font and, without Segoe UI Emoji, fonts.emoji;
     navigator.platform is Win32 in every context;
  8. a windows profile's display.pointer and prefers_contrast follow
     pointer_device_win.cc and the Windows contrast rules: a touch-only
     tablet starts; hover without a fine pointer, or prefers_contrast more
     without forced colors, stops the browser naming the field;
  9. a windows profile's capability claims as on the win11-uhd600 example
     start with a warning for each claim this build cannot back; pages see
     navigator.bluetooth and navigator.share in window contexts only, UVPAA
     and hybridTransport true, PlayReady unsupported and
     getClientCapabilities() extension:payment true; a windows profile
     without Web Bluetooth or Web Share, with AC-3, without conditional
     mediation, or with HEVC but no hardware HEVC decode stops the browser;
 10. locale.geolocation: a claimed position starts without a warning; one
     out of range, at (0, 0), with an accuracy outside [10, 100000] m,
     without a latitude, with an unknown field, or in a profile with an Etc/
     zone stops the browser naming the field;
 11. the example profiles start without gpu.vendor_id, device_id,
     vendor_name, device_name, gl_vendor and gl_renderer, which the browser
     derives from gpu.webgl: the windows one (D3D11) with no gpu warning,
     the linux one (OpenGL) with one warning per PCI id its renderer does
     not name; without the driver too, the linux one also warns
     gpu.gl_renderer, driver_vendor and driver_version, the windows one
     gpu.driver_version. A D3D11 id other than the renderer's, a gl_vendor
     other than webgl.unmasked_vendor and a gl_renderer that does not
     extend webgl.unmasked_renderer stop the browser naming the field;
 12. the linux example without driver_vendor and driver_version starts with
     no gpu warning and reports in CDP SystemInfo the driver its gl_renderer
     names (NVIDIA 615.71.09); a driver other than that one stops the
     browser naming the field.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path

from helpers import (ALL_CONTEXTS, WINDOW_CONTEXTS, WINDOWS_FAMILIES, GuardFailure, dump, expect_everywhere, expect_refused, expect_warned, finish,
                     load_profile, parse_args, run_binary, start_and_exit, unbacked_warnings, windows_variant)

HEVC = ('video/mp4; codecs="hev1.1.6.L93.B0"', 'video/mp4; codecs="hvc1.1.6.L93.B0"')
DOLBY = ('video/mp4; codecs="ac-3"', 'video/mp4; codecs="ec-3"')
# The decodingInfo() configs whose main-thread powerEfficient is
# gpu.hardware_decode.<codec> (consistency/compare.py).
DECODE = {"h264": "h264_1080p", "hevc": "hevc_1080p", "vp9": "vp9_4k", "av1": "av1_1080p"}
# The values this build reports on Linux without a claim; the guard's base
# profile must have them (core/profile.patch ParseCapabilities).
LINUX_CLAIMS = {
    ("media", "hevc"): False,
    ("media", "dolby_audio"): False,
    ("media", "playready"): False,
    ("gpu", "hardware_decode", "h264"): False,
    ("gpu", "hardware_decode", "hevc"): False,
    ("gpu", "hardware_decode", "vp9"): False,
    ("gpu", "hardware_decode", "av1"): False,
    ("webauthn", "user_verifying_platform_authenticator"): False,
    ("webauthn", "conditional_mediation"): True,
    ("capabilities", "bluetooth"): False,
    ("capabilities", "bluetooth_available"): False,
    ("capabilities", "web_share"): False,
}
# Values no stock Chrome on the OS reports: each stops the browser naming the
# field. Paths with their refused value.
LINUX_REFUSED = {
    ("media", "dolby_audio"): True,
    ("media", "playready"): True,
    ("webauthn", "conditional_mediation"): False,
    ("capabilities", "bluetooth"): True,
    ("capabilities", "web_share"): True,
    ("media", "hevc"): True,  # without gpu.hardware_decode.hevc
}
WINDOWS_REFUSED = {
    ("media", "dolby_audio"): True,
    ("webauthn", "conditional_mediation"): False,
    ("capabilities", "bluetooth"): False,
    ("capabilities", "web_share"): False,
    ("media", "hevc"): False,  # with gpu.hardware_decode.hevc
}
# Claims this build reports without the host backing them, and the warning
# each logs, on either OS.
UNBACKED_CLAIMS = {
    ("gpu", "hardware_decode", "h264"): True,
    ("gpu", "hardware_decode", "hevc"): True,
    ("gpu", "hardware_decode", "vp9"): True,
    ("media", "hevc"): True,
    ("webauthn", "user_verifying_platform_authenticator"): True,
    ("capabilities", "bluetooth_available"): True,
}
UNBACKED_WARNINGS = {
    "gpu.hardware_decode": ("h264, hevc, vp9", "decodingInfo()"),
    "media.hevc": ("HEVC playback fails",),
    "webauthn.user_verifying_platform_authenticator": ("finds no passkey",),
    "capabilities.bluetooth_available": ("hybridTransport",),
}
# A laptop's battery, draining or charging below full, is a real state.
BATTERY_ACCEPTED = ({"charging": False, "level": 1}, {"charging": True, "level": 0.42})


def with_value(profile: dict, path: tuple[str, ...], value) -> dict:
    p = copy.deepcopy(profile)
    node = p
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return p


def check_linux_capabilities(d: dict, profile: dict) -> None:
    """What pages see for the claims in LINUX_CLAIMS, in every context whose
    scope has the API. Workers' decodingInfo powerEfficient comes from the
    decode statistics database in stock Chrome too, so only window contexts
    carry gpu.hardware_decode."""
    if profile["os"]["family"] != "linux":
        raise GuardFailure(f"vacuous: the profile claims os.family {profile['os']['family']}, not linux")
    for path, accepted in LINUX_CLAIMS.items():
        node = profile
        for part in path[:-1]:
            node = node[part]
        if node.get(path[-1], accepted) != accepted:
            raise GuardFailure(f"vacuous: the profile under test has {'.'.join(path)}={node[path[-1]]!r}, which this build refuses")
    for mime in HEVC + DOLBY:
        expect_everywhere(d, ("codecs", "canPlayType", mime), "", WINDOW_CONTEXTS, label=f"canPlayType({mime})")
        expect_everywhere(d, ("codecs", "mediaSourceIsTypeSupported", mime), False, (*WINDOW_CONTEXTS, "worker"), label=f"MediaSource.isTypeSupported({mime})")
    for config in ("hevc_1080p", "ac3"):
        expect_everywhere(d, f"codecs.decodingInfo.{config}.supported", False, ALL_CONTEXTS)
    for codec, config in DECODE.items():
        expect_everywhere(d, f"codecs.decodingInfo.{config}.powerEfficient", profile["gpu"]["hardware_decode"][codec], WINDOW_CONTEXTS,
                          label=f"gpu.hardware_decode.{codec} (decodingInfo {config} powerEfficient)")
    expect_everywhere(d, "codecs.keySystems.playready.supported", False, WINDOW_CONTEXTS, label="media.playready (requestMediaKeySystemAccess)")
    webauthn = profile["webauthn"]
    uvpaa = webauthn["user_verifying_platform_authenticator"]
    hybrid = profile["capabilities"].get("bluetooth_available", False)
    for dump_path, want in (
        ("webauthn.isUserVerifyingPlatformAuthenticatorAvailable", uvpaa),
        ("webauthn.getClientCapabilities.userVerifyingPlatformAuthenticator", uvpaa),
        ("webauthn.isConditionalMediationAvailable", webauthn["conditional_mediation"]),
        ("webauthn.getClientCapabilities.conditionalGet", webauthn["conditional_mediation"]),
        ("webauthn.getClientCapabilities.hybridTransport", hybrid),
        ("webauthn.getClientCapabilities.passkeyPlatformAuthenticator", uvpaa or hybrid),
        ("webauthn.getClientCapabilities.extension:payment", False),
    ):
        expect_everywhere(d, dump_path, want, WINDOW_CONTEXTS)
    expect_everywhere(d, "navigator.has.bluetooth", profile["capabilities"]["bluetooth"], label="capabilities.bluetooth (navigator.bluetooth)")
    for name in ("share", "canShare"):
        expect_everywhere(d, f"navigator.has.{name}", profile["capabilities"]["web_share"], label=f"capabilities.web_share (navigator.{name})")
    expect_everywhere(d, "navigator.battery.charging", True, WINDOW_CONTEXTS, label="capabilities.battery.charging (getBattery)")
    expect_everywhere(d, "navigator.battery.level", 1, WINDOW_CONTEXTS, label="capabilities.battery.level (getBattery)")


def with_values(profile: dict, values: dict) -> dict:
    p = profile
    for path, value in values.items():
        p = with_value(p, path, value)
    return p


def check_unbacked_claims(binary: str, profile: dict, work: Path, name: str, headless: bool) -> tuple:
    """The claims in UNBACKED_CLAIMS on `profile` start with one warning per
    field and are reported as claimed. Returns the start and the dump."""
    claimed = with_values(profile, UNBACKED_CLAIMS)
    r = start_and_exit(binary, claimed, work, name)
    if r.returncode != 0:
        raise GuardFailure(f"{name}: unbacked capability claims do not start: exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    for field, needles in UNBACKED_WARNINGS.items():
        expect_warned(r, name, field, *needles)
    d = dump(binary, str(work / f"{name}.json"), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"{name}: contexts failed: {d['context_errors']}")
    for dump_path in ("webauthn.isUserVerifyingPlatformAuthenticatorAvailable",
                      "webauthn.getClientCapabilities.userVerifyingPlatformAuthenticator",
                      "webauthn.isConditionalMediationAvailable",
                      "webauthn.getClientCapabilities.hybridTransport",
                      "webauthn.getClientCapabilities.passkeyPlatformAuthenticator"):
        expect_everywhere(d, dump_path, True, WINDOW_CONTEXTS, label=f"{name}: {dump_path}")
    expect_everywhere(d, ("codecs", "canPlayType", HEVC[0]), "probably", WINDOW_CONTEXTS, label=f"{name}: media.hevc (canPlayType)")
    for codec, config in DECODE.items():
        expect_everywhere(d, f"codecs.decodingInfo.{config}.powerEfficient", UNBACKED_CLAIMS.get(("gpu", "hardware_decode", codec), False),
                          WINDOW_CONTEXTS, label=f"{name}: gpu.hardware_decode.{codec} (decodingInfo {config} powerEfficient)")
    return r, d


def check_linux_refusals(binary: str, profile: dict, work: Path, headless: bool) -> None:
    """Each claim in LINUX_REFUSED stops the browser naming the field; the
    claims in UNBACKED_CLAIMS start with warnings; laptop batteries start.
    --dump-dom makes a browser that accepts the profile exit 0 once
    about:blank has loaded instead of running on."""
    for path, value in LINUX_REFUSED.items():
        field = ".".join(path)
        expect_refused(binary, with_value(profile, path, value), work, f"linux-refuse-{field}={value}", field)
    check_unbacked_claims(binary, profile, work, "linux-unbacked", headless)
    for battery in BATTERY_ACCEPTED:
        name = f"linux-battery-{battery['level']}-{battery['charging']}"
        r = start_and_exit(binary, with_value(profile, ("capabilities", "battery"), battery), work, name)
        if r.returncode != 0:
            raise GuardFailure(f"capabilities.battery={battery}: exit {r.returncode}, a laptop battery is refused: {r.stderr[-300:]!r}")
        print(f"PASS: capabilities.battery={battery} starts")


GEOLOCATION = {"latitude": 52.52, "longitude": 13.405, "accuracy": 75609.25}
# (field the refusal names, the claim, the zone; None keeps the profile's).
GEOLOCATION_REFUSED = (
    ("locale.geolocation.latitude", dict(GEOLOCATION, latitude=90.5), None),
    ("locale.geolocation.longitude", dict(GEOLOCATION, longitude=-180.5), None),
    ("locale.geolocation.accuracy", dict(GEOLOCATION, accuracy=5), None),
    ("locale.geolocation.accuracy", dict(GEOLOCATION, accuracy=100001), None),
    ("locale.geolocation", dict(GEOLOCATION, latitude=0, longitude=0), None),
    ("locale.geolocation.latitude", {"longitude": 13.405, "accuracy": 1000}, None),
    ("locale.geolocation.altitude", dict(GEOLOCATION, altitude=34), None),
    ("locale.timezone", GEOLOCATION, "Etc/UTC"),
    ("locale.timezone", GEOLOCATION, "Etc/GMT-1"),
)


def check_geolocation(binary: str, profile: dict, work: Path) -> None:
    """Check 10."""
    r = start_and_exit(binary, with_value(profile, ("locale", "geolocation"), GEOLOCATION), work, "geolocation-claimed")
    if r.returncode != 0 or "locale.geolocation" in unbacked_warnings(r.stderr):
        raise GuardFailure(f"a claimed position: exit {r.returncode}, warnings {unbacked_warnings(r.stderr)}, stderr {r.stderr[-300:]!r}")
    print("PASS: locale.geolocation claimed: starts without a locale.geolocation warning")
    for i, (field, claim, zone) in enumerate(GEOLOCATION_REFUSED):
        bad = with_value(profile, ("locale", "geolocation"), claim)
        if zone:
            bad = with_value(bad, ("locale", "timezone"), zone)
        expect_refused(binary, bad, work, f"geolocation-refuse-{i}", field)


def check_macos_refused(binary: str, profile: dict, work: Path) -> None:
    """Check 6, on a profile of either family."""
    bad_path = work / "refuse-os-family-macos.json"
    bad_path.write_text(json.dumps(with_value(profile, ("os", "family"), "macos")))
    r = run_binary(binary, [f"--phasmid-profile={bad_path}", "--headless=new", "--dump-dom", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or "'os.family' must be one of" not in r.stderr:
        raise GuardFailure(f"os.family=macos: exit {r.returncode}, stderr does not refuse it as an unknown family: {r.stderr[-300:]!r}")
    print(f"PASS: os.family=macos exits {r.returncode} as an unknown family")


EXAMPLES = Path(__file__).resolve().parents[3] / "profiles" / "examples"
OPTIONAL_IDENTITY = ("vendor_id", "device_id", "vendor_name", "device_name", "gl_vendor", "gl_renderer")
IDENTITY_WARNINGS = {f"gpu.{k}" for k in (*OPTIONAL_IDENTITY, "driver_vendor", "driver_version")}
# (example, fields removed, the gpu.* warnings it logs).
DERIVED = (
    ("win11-uhd600-1440x960.json", OPTIONAL_IDENTITY, set()),
    ("linux-arch-rtx3060-1080p.json", OPTIONAL_IDENTITY, {"gpu.vendor_id", "gpu.device_id"}),
    ("win11-uhd600-1440x960.json", (*OPTIONAL_IDENTITY, "driver_vendor", "driver_version"), {"gpu.driver_version"}),
    ("linux-arch-rtx3060-1080p.json", (*OPTIONAL_IDENTITY, "driver_vendor", "driver_version"),
     {"gpu.vendor_id", "gpu.device_id", "gpu.gl_renderer", "gpu.driver_vendor", "gpu.driver_version"}),
    ("linux-arch-rtx3060-1080p.json", ("driver_vendor", "driver_version"), set()),
)


def check_gpu_identity(binary: str, work: Path) -> None:
    """Check 11."""
    for name, removed, expected in DERIVED:
        example = json.loads((EXAMPLES / name).read_text())
        gpu = example["gpu"]
        for key in removed:
            del gpu[key]
        label = f"{name} without {len(removed)} gpu identity fields"
        r = start_and_exit(binary, example, work, f"derived-{len(removed)}-{name}")
        warnings = unbacked_warnings(r.stderr)
        got = {field for field in warnings if field in IDENTITY_WARNINGS}
        if r.returncode != 0 or got != expected or any(len(warnings[f]) != 1 for f in got):
            raise GuardFailure(f"{label}: expected exit 0 and one warning each for {sorted(expected)}, got exit {r.returncode}, "
                               f"warnings {({f: warnings[f] for f in got})}, stderr {r.stderr[-400:]!r}")
        print(f"PASS: {label}: starts, warning {sorted(expected) or 'nothing'}")
    win = json.loads((EXAMPLES / "win11-uhd600-1440x960.json").read_text())
    linux = json.loads((EXAMPLES / "linux-arch-rtx3060-1080p.json").read_text())
    for profile, field, value in (
        (win, "device_id", "0x3184"),
        (win, "vendor_id", "0x10de"),
        (linux, "gl_vendor", "Google Inc. (Intel)"),
        (win, "gl_renderer", win["gpu"]["gl_renderer"].replace(win["gpu"]["driver_version"], "1.2.3.4")),
        (linux, "gl_renderer", linux["gpu"]["gl_renderer"].replace("RTX 3060", "RTX 3070")),
        (linux, "driver_vendor", "Mesa"),
        (linux, "driver_version", "580.95.05"),
    ):
        expect_refused(binary, with_value(profile, ("gpu", field), value), work, f"gpu-identity-refuse-{field}-{profile['os']['family']}", f"gpu.{field}")


def check_linux_driver_derived(binary: str, work: Path, headless: bool) -> None:
    """Check 12: the linux example's driver, derived from its gl_renderer."""
    linux = json.loads((EXAMPLES / "linux-arch-rtx3060-1080p.json").read_text())
    want = {"driverVendor": linux["gpu"].pop("driver_vendor"), "driverVersion": linux["gpu"].pop("driver_version")}
    path = work / "linux-driver-derived.json"
    path.write_text(json.dumps(linux))
    d = dump(binary, str(path), headless=headless)
    devices = ((d.get("system_info") or {}).get("gpu") or {}).get("devices") or []
    if not devices:
        raise GuardFailure(f"vacuous: CDP SystemInfo.getInfo has no GPU device: {d.get('system_info')!r:.300}")
    got = {k: devices[0].get(k) for k in want}
    if got != want:
        raise GuardFailure(f"linux example without its driver: CDP SystemInfo reports {got}, gl_renderer names {want}")
    print(f"PASS: linux example without its driver reports the one gl_renderer names: {want}")


MISSING_FAMILY = "Phasmid Guard Missing Sans"
# Not a Windows font: a name in Skia's Arial metric class that fontconfig's
# metric aliases resolve, so the windows profile has one alias to report.
ALIAS_FAMILY = "Arimo"


def fontconfig(*args: str) -> str:
    """fc-list or fc-match output on the host, empty when unavailable."""
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def check_windows(binary: str, profile: dict, work: Path, headless: bool) -> None:
    """Checks 7 and 8."""
    win = windows_variant(profile)
    families = [*WINDOWS_FAMILIES, ALIAS_FAMILY, MISSING_FAMILY]
    win["fonts"]["families"] = families
    published = {f.strip().lower() for line in fontconfig("fc-list", "--format", "%{family}\n").splitlines() for f in line.split(",")}
    if not published:
        raise GuardFailure("vacuous: fc-list lists no font family on this host")
    # An empty directory stands in for the Windows font store, so the
    # warnings are about the host's files whatever store this host has.
    r = start_and_exit(binary, win, work, "windows", [f"--phasmid-windows-fonts={tempfile.mkdtemp(dir=work)}"])
    if r.returncode != 0:
        raise GuardFailure(f"a windows profile does not start: exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    warnings = unbacked_warnings(r.stderr)
    repeated = {field: details for field, details in warnings.items() if len(details) > 1}
    if repeated:
        raise GuardFailure(f"windows profile: warnings logged more than once: {repeated}")
    needles = [f"of {len(families)} families have no font here", MISSING_FAMILY, "resolve only through host aliases"]
    alias = fontconfig("fc-match", "--format", "%{family[0]}", ALIAS_FAMILY).strip()
    if ALIAS_FAMILY.lower() not in published and alias in ("Arial", "Liberation Sans"):
        needles.append(f"{ALIAS_FAMILY} -> ")
    expect_warned(r, "windows profile", "fonts.families", *needles)
    expect_warned(r, "windows profile", "fonts.ui_font", "Segoe UI", "Arial at 16 px")
    if "segoe ui emoji" not in published:
        expect_warned(r, "windows profile", "fonts.emoji", "Segoe UI Emoji")
    path = work / "windows.json"
    d = dump(binary, str(path), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed with a windows profile: {d['context_errors']}")
    expect_everywhere(d, "navigator.platform", "Win32", label="a windows profile reaches every context (navigator.platform)")

    tablet = copy.deepcopy(win)
    tablet["display"]["pointer"] = {"primary": "coarse", "hover": "none", "any_pointer": ["coarse"], "any_hover": []}
    tablet["hardware"]["max_touch_points"] = 10
    r = start_and_exit(binary, tablet, work, "windows-tablet")
    if r.returncode != 0:
        raise GuardFailure(f"a windows tablet (coarse, no hover, 10 touch points) does not start: exit {r.returncode}, stderr {r.stderr[-300:]!r}")
    print("PASS: a windows tablet (coarse, no hover, 10 touch points) starts")
    hover = copy.deepcopy(tablet)
    hover["display"]["pointer"]["any_hover"] = ["hover"]
    contrast = copy.deepcopy(win)
    contrast["display"]["prefers_contrast"] = "more"
    contrast["display"]["forced_colors"] = "none"
    for name, bad, field in (("windows-hover-without-mouse", hover, "display.pointer.any_hover"),
                             ("windows-contrast-without-forced-colors", contrast, "display.prefers_contrast")):
        r = start_and_exit(binary, bad, work, name)
        if r.returncode == 0 or f"'{field}'" not in r.stderr:
            raise GuardFailure(f"{name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-300:]!r}")
        print(f"PASS: {name} exits {r.returncode} naming {field}")


def check_windows_capabilities(binary: str, profile: dict, work: Path, headless: bool) -> None:
    """Check 9."""
    win = windows_variant(profile)
    win["fonts"]["families"] = list(WINDOWS_FAMILIES)
    win["media"]["playready"] = True
    r, d = check_unbacked_claims(binary, win, work, "windows-unbacked", headless)
    expect_warned(r, "windows profile", "media.playready", "unsupported")
    expect_warned(r, "windows profile", "capabilities.web_share", "AbortError")
    for name in ("bluetooth", "share", "canShare"):
        expect_everywhere(d, f"navigator.has.{name}", True, WINDOW_CONTEXTS, label=f"windows profile: navigator.{name} in window contexts")
        expect_everywhere(d, f"navigator.has.{name}", False, ("worker", "shared_worker", "service_worker"), label=f"windows profile: no navigator.{name} in workers")
    expect_everywhere(d, "codecs.keySystems.playready.supported", False, WINDOW_CONTEXTS, label="windows profile: media.playready reported unsupported")
    expect_everywhere(d, "webauthn.getClientCapabilities.extension:payment", True, WINDOW_CONTEXTS,
                      label="windows profile: getClientCapabilities() extension:payment (Secure Payment Confirmation)")
    for path, value in WINDOWS_REFUSED.items():
        field = ".".join(path)
        bad = with_value(win, path, value)
        if path == ("media", "hevc"):
            bad = with_value(bad, ("gpu", "hardware_decode", "hevc"), True)
        expect_refused(binary, bad, work, f"windows-refuse-{field}={value}", field)


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)

    # Precondition: the binary knows the switch at all. A plain Chromium
    # ignores unknown switches and starts, which would make checks 1 and 2
    # vacuous, so check 1 doubles as the precondition.
    bad = dict(profile)
    bad["hardware"] = dict(profile["hardware"], concurrency=0)
    bad_path = work / "bad.json"
    bad_path.write_text(json.dumps(bad))
    r = run_binary(args.binary, [f"--phasmid-profile={bad_path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0:
        raise GuardFailure("vacuous: an invalid profile (hardware.concurrency=0) did not stop the browser; is this a Phasmid build?")
    if "hardware.concurrency" not in r.stderr:
        raise GuardFailure(f"invalid profile exit {r.returncode} but stderr does not name the field: {r.stderr[-500:]!r}")
    print(f"PASS: invalid profile exits {r.returncode} naming hardware.concurrency")

    r = run_binary(args.binary, [f"--phasmid-profile={work / 'does-not-exist.json'}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or "cannot read profile" not in r.stderr:
        raise GuardFailure(f"missing profile file: exit {r.returncode}, stderr {r.stderr[-300:]!r}")
    print(f"PASS: missing profile file exits {r.returncode}")

    host = os.cpu_count() or 0
    want = 6 if host != 6 else 10
    good = dict(profile)
    good["hardware"] = dict(profile["hardware"], concurrency=want)
    good_path = work / "good.json"
    good_path.write_text(json.dumps(good))
    d = dump(args.binary, str(good_path), headless=not args.headful)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed with a valid profile: {d['context_errors']}")
    expect_everywhere(d, "navigator.hardwareConcurrency", want, label="profile reaches every process (hardwareConcurrency)")

    plain = dump(args.binary, None)
    expect_everywhere(plain, "navigator.hardwareConcurrency", host, label="without --phasmid-profile the host value is reported")

    family = profile["os"]["family"]
    if family == "linux":
        check_linux_capabilities(d, good)
        check_linux_refusals(args.binary, profile, work, headless=not args.headful)
    elif family == "windows":
        # Check 5's Linux rules do not apply to a Windows profile; checks 7
        # to 9 below run on the profile itself (windows_variant keeps its
        # identity and hardware).
        print("NOTE: check 5 (linux capability rules) need a linux profile; this one is windows")
    else:
        raise GuardFailure(f"vacuous: the profile claims os.family {family!r}, neither linux nor windows")
    check_macos_refused(args.binary, profile, work)
    check_geolocation(args.binary, profile, work)
    check_windows(args.binary, profile, work, headless=not args.headful)
    check_windows_capabilities(args.binary, profile, work, headless=not args.headful)
    check_gpu_identity(args.binary, work)
    check_linux_driver_derived(args.binary, work, headless=not args.headful)


finish(main)
