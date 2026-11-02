"""The CLDR facts the location derivation needs, extracted from one pinned release.

``profiles/data/cldr/cldr.json`` holds, from CLDR's own files at ``TAG``:

- ``zones``: every time zone ID CLDR knows (``common/bcp47/timezone.xml``)
  mapped to its IANA primary name: the ``iana`` attribute where CLDR's
  canonical alias is a backward link (Asia/Saigon -> Asia/Ho_Chi_Minh), else
  the first alias; deprecated entries follow ``preferred``. Zones tzdata merged
  but CLDR keeps apart (Europe/Amsterdam) stay apart, as ICU reports them.
- ``zone_regions``: each IANA primary name's territory, the first two letters
  of its BCP 47 ID (``vnsgn`` -> VN), except the IDs LDML spells otherwise.
- ``territories``: per territory, the ``languagePopulation`` rows of
  ``territoryInfo`` (``common/supplemental/supplementalData.xml``) whose
  language has modern coverage (``common/properties/coverageLevels.txt``), as
  [language, population percent, official status].
- ``parent_locales``: each locale's parent from the default ``parentLocales``
  of ``supplementalData.xml`` (es_MX -> es_419, en_AU -> en_001), the
  inheritance Chromium's UI locale matcher follows (chromium_locales.py).

Every file is checked against the sha256 pinned here before it is read. The
data is under the Unicode License v3 (profiles/data/cldr/LICENSE).

    python -m genprofile.cldr     # rebuilds the extract; needs network
"""

from __future__ import annotations

import hashlib
import json
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from functools import cache
from typing import Any

from .captures import REPO

TAG = "release-48"
_BASE = f"https://raw.githubusercontent.com/unicode-org/cldr/{TAG}/"
FILES = {
    "common/bcp47/timezone.xml": "54ce7d9f1d385101e24356df7d48ae62cf2acc4421378da60a4007d2488c1aad",
    "common/supplemental/supplementalData.xml": "fb054c9f9a95f0c4da9dd1e5e0ef6dd208b6f9a73f45303011be247277d81bef",
    "common/properties/coverageLevels.txt": "219b247b1c5471802f8ce3fc8449031d2943a77c076270425ed074505cd06f23",
    "LICENSE": "b4c0ae8ef04f7059f96ce5bbe0467f9fe6f6d81bbe13517701dfeb961fb4d0b6",
}
DATA_DIR = REPO / "profiles" / "data" / "cldr"
EXTRACT = DATA_DIR / "cldr.json"


class CldrError(Exception):
    pass


def zones(timezone_xml: bytes) -> dict[str, str]:
    types = {t.get("name", ""): t for t in ET.fromstring(timezone_xml).iter("type")}

    def primary(name: str) -> str | None:
        t = types[name]
        if t.get("deprecated") == "true":
            preferred = t.get("preferred")
            return primary(preferred) if preferred else None
        aliases = (t.get("alias") or "").split()
        return t.get("iana") or (aliases[0] if aliases else None)

    out: dict[str, str] = {}
    for name, t in types.items():
        target = primary(name)
        for alias in (t.get("alias") or "").split():
            if target is not None:
                out[alias] = target
    return dict(sorted(out.items()))


# BCP 47 time zone IDs whose first letters are not their region (LDML part 1, Time Zone Identifiers).
_REGION_EXCEPTIONS = {"gazastrp": "PS", "hebron": "PS", "jeruslm": "IL"}
_NO_REGION = ("utc", "gmt", "unk")


def zone_regions(timezone_xml: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in ET.fromstring(timezone_xml).iter("type"):
        name, aliases = t.get("name") or "", (t.get("alias") or "").split()
        if t.get("deprecated") == "true" or not aliases or name.startswith(_NO_REGION):
            continue
        out[t.get("iana") or aliases[0]] = _REGION_EXCEPTIONS.get(name, name[:2].upper())
    return dict(sorted(out.items()))


def modern_languages(coverage_txt: str) -> set[str]:
    out = set()
    for line in coverage_txt.splitlines():
        if line.startswith("#") or ";" not in line:
            continue
        locale, level = (part.strip() for part in line.split(";")[:2])
        if level == "modern":
            out.add(locale)
    return out


def territories(supplemental_xml: bytes, modern: set[str]) -> dict[str, list[list[Any]]]:
    out: dict[str, list[list[Any]]] = {}
    for territory in ET.fromstring(supplemental_xml).iter("territory"):
        if territory.get("population") is None:  # territoryInfo rows only, not containment
            continue
        rows = [
            [lp.get("type"), float(lp.get("populationPercent", "0")), lp.get("officialStatus") or ""]
            for lp in territory.iter("languagePopulation")
            if lp.get("type") in modern
        ]
        if rows:
            out[territory.get("type", "")] = rows
    return dict(sorted(out.items()))


def parent_locales(supplemental_xml: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in ET.fromstring(supplemental_xml).iter("parentLocales"):
        if block.get("component") is not None:  # collation, plural, ... inheritance only
            continue
        for row in block.iter("parentLocale"):
            for locale in (row.get("locales") or "").split():
                out[locale] = row.get("parent", "")
    return dict(sorted(out.items()))


def _get(path: str) -> bytes:
    with urllib.request.urlopen(_BASE + path, context=ssl.create_default_context(), timeout=60) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != FILES[path]:
        raise CldrError(f"{_BASE + path}: sha256 differs from the pin")
    return data


def build() -> tuple[dict[str, Any], bytes]:
    raw = {path: _get(path) for path in FILES}
    modern = modern_languages(raw["common/properties/coverageLevels.txt"].decode())
    return {
        "source": {"tag": TAG, "files": {path: {"url": _BASE + path, "sha256": sha} for path, sha in FILES.items()}},
        "zones": zones(raw["common/bcp47/timezone.xml"]),
        "zone_regions": zone_regions(raw["common/bcp47/timezone.xml"]),
        "territories": territories(raw["common/supplemental/supplementalData.xml"], modern),
        "parent_locales": parent_locales(raw["common/supplemental/supplementalData.xml"]),
    }, raw["LICENSE"]


@cache
def extract() -> dict[str, Any]:
    data = json.loads(EXTRACT.read_text())
    if data["source"]["tag"] != TAG:
        raise CldrError(f"{EXTRACT}: built from {data['source']['tag']}, pinned {TAG}; rebuild it")
    return data


def canonical_zone(name: str) -> str:
    """The IANA primary name of a zone ID, e.g. Asia/Saigon -> Asia/Ho_Chi_Minh."""
    try:
        return extract()["zones"][name]
    except KeyError:
        raise CldrError(f"time zone {name!r} is not in CLDR {TAG}") from None


def zone_region(name: str) -> str | None:
    """The territory of an IANA primary zone name; None for UTC and the Etc zones."""
    return extract()["zone_regions"].get(name)


def region_zones(region: str) -> list[str]:
    return sorted(z for z, r in extract()["zone_regions"].items() if r == region)


def parent_locale(locale: str) -> str | None:
    """CLDR's parent of a locale in underscore form (es_MX -> es_419); None when it inherits by truncation."""
    return extract()["parent_locales"].get(locale)


def main() -> int:
    data, licence = build()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    (DATA_DIR / "LICENSE").write_bytes(licence)
    print(f"wrote {EXTRACT}: {len(data['zones'])} zone IDs, {len(data['territories'])} territories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
