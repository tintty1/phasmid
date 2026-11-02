"""CLI: python -m genprofile --os linux --seed <32 hex> (--timezone <zone> | --exit-ip <ip>) -o <profile.json>

With --exit-ip, the zone, geolocation, WebRTC public address and (unless
--languages is given) the languages come from the proxy exit IP. Windows
profiles should always be generated with one: Windows Chrome serves a position
from the system provider, so a Windows profile without one is a stronger tell.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .captures import REPO, validate
from .generate import FAMILIES, GenerationError, MissingCaptures, dumps, generate
from .location import Location, LocationError, locate


def _summary(profile: dict) -> str:
    d, h = profile["display"], profile["hardware"]
    return (
        f"{profile['os']['family']} {profile['os'].get('version', '?')}, {h['concurrency']} threads, "
        f"{h['device_memory']} GB, {d['width']}x{d['height']}@{d['device_pixel_ratio']} "
        f"window {d['window']['outer_width']}x{d['window']['outer_height']}, gpu {profile.get('gpu', {}).get('device_id', '-')}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="genprofile", description=__doc__)
    p.add_argument("--os", required=True, choices=FAMILIES)
    p.add_argument("--seed", required=True, help="32 lowercase hex chars; becomes the profile's seed")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--timezone", help="IANA zone, e.g. America/New_York")
    where.add_argument("--exit-ip", help="the proxy's exit IP: derives the zone, geolocation, WebRTC public address "
                                          "and languages (DB-IP City Lite, timezonefinder, CLDR)")
    p.add_argument("--languages", help="navigator.languages, comma separated (default en-US,en, or the exit IP "
                                       "country's with --exit-ip)")
    p.add_argument("--gpu", help="vendor_id:device_id of a captured GPU or of one its host class may claim, e.g. 0x10de:0x2504")
    p.add_argument("--draft", action="store_true",
                   help="when captures are missing, write the partial profile anyway (outside the repository) and exit 1")
    p.add_argument("-o", "--out", required=True)
    args = p.parse_args(argv)

    out = Path(args.out).resolve()
    languages = [lang.strip() for lang in args.languages.split(",") if lang.strip()] if args.languages else None
    location: Location | None = None
    note = ""
    if args.exit_ip:
        from . import geoip, location as loc

        try:
            location = locate(args.exit_ip, args.seed, geoip.reader())
        except (LocationError, geoip.GeoipError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
        note = f"{geoip.provenance()}, {loc.provenance()}"
    timezone = location.timezone if location else args.timezone
    languages = languages or (location.languages if location else ["en-US", "en"])
    try:
        profile = generate(args.os, args.seed, languages, timezone, args.gpu, location, note)
    except MissingCaptures as e:
        print(e, file=sys.stderr)
        if not args.draft:
            return 1
        if out.is_relative_to(REPO):
            print("a draft is not a profile; write it outside the repository", file=sys.stderr)
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dumps(e.draft))
        print(f"wrote draft {out}: {_summary(e.draft)}")
        for err in validate(e.draft):
            print(f"  schema: {err}", file=sys.stderr)
        return 1
    except GenerationError as e:
        print(e, file=sys.stderr)
        return 1

    errors = validate(profile)
    if errors:
        print("generated profile does not validate:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(profile))
    print(f"wrote {out}: {_summary(profile)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
