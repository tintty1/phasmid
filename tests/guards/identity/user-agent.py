"""Guard for patches/phasmid/identity/user-agent.patch.

The UA string, navigator.platform, userAgentData (low and high entropy) and
the User-Agent and Sec-CH-UA* headers all follow os.*; the brand list is
GREASE + Chromium + Google Chrome; every version is the build's; no Headless
token in headless mode. The profile claims a Linux aarch64 machine so no
value can match this x86_64 host by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import ALL_CONTEXTS, GuardFailure, dump, expect_everywhere, expect_header, finish, load_profile, parse_args

from consistency.compare import _get

UA_SLOTS = ("navigation", "fetch_main", "fetch_worker", "fetch_service_worker")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    version = (Path(__file__).resolve().parents[3] / "chromium_version.txt").read_text().strip()
    major = version.split(".")[0]

    profile["os"].update(
        ua_platform_token="X11; Linux aarch64",
        navigator_platform="Linux aarch64",
        ch_platform="Linux",
        ch_platform_version="6.12.0",
        architecture="arm",
        bitness="64",
        wow64=False,
        model="",
        form_factors=["Desktop"],
    )
    profile["automation"]["report_headful"] = True
    path = Path(args.work_dir) / "profile.json"
    path.write_text(json.dumps(profile))
    ua = f"Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"

    # Precondition: without a profile this binary reports the host (and, in
    # headless mode, the Headless token); otherwise every check is vacuous.
    plain = dump(args.binary, None, headless=not args.headful)
    plain_ua = _get(plain["contexts"]["main"], "navigator.userAgent")
    if plain_ua == ua or _get(plain["contexts"]["main"], "navigator.platform") == "Linux aarch64":
        raise GuardFailure(f"vacuous: the host already reports the guard's values: {plain_ua!r}")
    print(f"PASS: precondition, host UA is {plain_ua!r}")

    d = dump(args.binary, str(path), headless=not args.headful)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "navigator.userAgent", ua)
    expect_everywhere(d, "navigator.platform", "Linux aarch64")
    expect_everywhere(d, "navigator.userAgentData.platform", "Linux")
    expect_everywhere(d, "navigator.userAgentData.mobile", False)
    for key, want in (
        ("architecture", "arm"),
        ("bitness", "64"),
        ("platformVersion", "6.12.0"),
        ("wow64", False),
        ("model", ""),
        ("formFactors", ["Desktop"]),
        ("uaFullVersion", version),
    ):
        expect_everywhere(d, f"navigator.userAgentData.highEntropy.{key}", want)

    for ctx in ALL_CONTEXTS:
        c = d["contexts"][ctx]
        brands = _get(c, "navigator.userAgentData.brands") or []
        names = sorted(b["brand"] for b in brands)
        grease = [n for n in names if n not in ("Chromium", "Google Chrome")]
        if len(names) != 3 or "Chromium" not in names or "Google Chrome" not in names or len(grease) != 1 or not grease[0].startswith("Not"):
            raise GuardFailure(f"{ctx}: brands {names} are not GREASE + Chromium + Google Chrome")
        if any(b["version"] != major for b in brands if b["brand"] in ("Chromium", "Google Chrome")):
            raise GuardFailure(f"{ctx}: brand major versions {brands} != {major}")
        full = _get(c, "navigator.userAgentData.highEntropy.fullVersionList") or []
        full_names = sorted(b["brand"] for b in full)
        if full_names != names:
            raise GuardFailure(f"{ctx}: fullVersionList brands {full_names} != brands {names}")
        if any(b["version"] != version for b in full if b["brand"] in ("Chromium", "Google Chrome")):
            raise GuardFailure(f"{ctx}: fullVersionList versions {full} != build {version}")
    print(f"PASS: brands are GREASE + Chromium + Google Chrome at {version} in {len(ALL_CONTEXTS)} contexts")

    for slot in UA_SLOTS:
        expect_header(d, slot, "user-agent", ua)
    expect_header(d, "navigation", "sec-ch-ua-platform", '"Linux"')
    expect_header(d, "navigation_with_hints", "sec-ch-ua-platform-version", '"6.12.0"')
    expect_header(d, "navigation_with_hints", "sec-ch-ua-arch", '"arm"')
    expect_header(d, "navigation_with_hints", "sec-ch-ua-bitness", '"64"')
    expect_header(d, "navigation_with_hints", "sec-ch-ua-wow64", "?0")
    expect_header(d, "navigation_with_hints", "sec-ch-ua-full-version", f'"{version}"')
    sec_ch_ua = d["headers"].get("navigation", {}).get("sec-ch-ua", "")
    if f'"Google Chrome";v="{major}"' not in sec_ch_ua or f'"Chromium";v="{major}"' not in sec_ch_ua:
        raise GuardFailure(f"Sec-CH-UA header {sec_ch_ua!r} lacks Google Chrome or Chromium at {major}")
    print(f"PASS: Sec-CH-UA is {sec_ch_ua!r}")


finish(main)
