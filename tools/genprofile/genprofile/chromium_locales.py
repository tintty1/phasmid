"""Chrome's default accept-languages per UI locale, extracted from the pinned Chromium.

``intl.accept_languages`` (navigator.languages and the Accept-Language header
of a fresh profile) defaults to ``IDS_ACCEPT_LANGUAGES`` in the UI locale's
resources (``LanguagePrefs::RegisterProfilePrefs``,
components/language/core/browser/language_prefs.cc). The message is in
``components/components_locale_settings.grd`` (``en-US,en``) with one
translation per ``components/strings/components_locale_settings_<lang>.xtb``;
a UI locale with no translation of it gets the English text
(``fallback_to_english``). Neither file branches on the OS, and Windows and
Linux ship the same UI locales (``build/config/locales.gni``:
``all_chrome_locales`` minus ``extended_locales``), so the table is the same
for both.

``profiles/data/chromium/accept-languages.json`` holds, from the Chromium tag
``TAG`` (the repository's ``chromium_version.txt``), each file's git blob id,
the desktop UI locales and each one's accept-languages list. Rebuild it from a
Chromium checkout that has the tag:

    python -m genprofile.chromium_locales /path/to/chromium/src

The UI locale itself follows the OS locale: ``l10n_util::GetApplicationLocale``
resolves it with ``ui_l10n::GetPlatformLanguageMatcher``
(base/i18n/language_tag_matcher.cc), which walks CLDR's locale inheritance
(es_MX -> es_419 -> es) to the nearest desktop locale. ``ui_locale`` follows
the same walk; ``ANCESTORS`` are the matcher's edges from a locale Chrome has
no resources for to the one it prefers, and the expectations of
ui/base/l10n/l10n_util_unittest.cc at the tag are tested in test_location.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path

from . import cldr
from .captures import REPO

TAG = (REPO / "chromium_version.txt").read_text().strip()
GRD = "components/components_locale_settings.grd"
XTB_DIR = "components/strings"
LOCALES_GNI = "build/config/locales.gni"
MESSAGE = "IDS_ACCEPT_LANGUAGES"
DATA_DIR = REPO / "profiles" / "data" / "chromium"
EXTRACT = DATA_DIR / "accept-languages.json"

# CLDR locale (underscore form) -> the desktop UI locale the matcher picks for it
# when it is not itself one. From base/i18n/language_tag_matcher.cc at the tag:
# GetEdgeWeight prefers en -> en-US, en-001 -> en-GB, pt -> pt-BR, zh -> zh-CN,
# zh-Hant -> zh-TW; LanguageTagPreferenceGraph adds en-PH and en-LR -> en-US and
# en-CA -> en-GB; ICU4X falls nb back to no, so no (and nn, whose CLDR parent is
# no) reaches nb.
ANCESTORS = {
    "en": "en-US",
    "en_001": "en-GB",
    "en_CA": "en-GB",
    "en_LR": "en-US",
    "en_PH": "en-US",
    "no": "nb",
    "pt": "pt-BR",
    "zh": "zh-CN",
    "zh_Hans": "zh-CN",
    "zh_Hant": "zh-TW",
}
# GetApplicationLocale's answer when nothing matches.
FALLBACK_LOCALE = "en-US"


class ChromiumLocalesError(Exception):
    pass


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True).stdout


def _gn_list(gni: str, name: str) -> list[str]:
    """The first top-level ``name = [ ... ]`` of a .gni file."""
    m = re.search(rf"^{name}\s*=\s*\[(.*?)\]", gni, re.MULTILINE | re.DOTALL)
    if m is None:
        raise ChromiumLocalesError(f"{LOCALES_GNI}: no list {name}")
    body = "\n".join(line.split("#", 1)[0] for line in m.group(1).splitlines())
    return re.findall(r'"([^"]+)"', body)


def desktop_locales(gni: str) -> list[str]:
    extended = set(_gn_list(gni, "extended_locales")) | set(_gn_list(gni, "pseudolocales"))
    return sorted(set(_gn_list(gni, "all_chrome_locales")) - extended)


def _pak_locales(grd: ET.Element) -> dict[str, str]:
    """grit lang -> the .pak locale it is written as (no -> nb, en -> en-US)."""
    names: dict[str, list[str]] = {}
    for out in grd.iter("output"):
        m = re.fullmatch(r"components_locale_settings_(.+)\.pak", out.get("filename", ""))
        if m and out.get("type") == "data_package":
            names.setdefault(out.get("lang", ""), []).append(m.group(1))
    # A lang with two outputs is an is_ios branch (es-419 as es-MX); the desktop one is named as the lang.
    return {lang: lang if lang in files else files[0] for lang, files in names.items()}


def build(checkout: Path) -> dict:
    files = [GRD, LOCALES_GNI]
    grd_text = _git(checkout, "show", f"{TAG}:{GRD}")
    grd = ET.fromstring(grd_text)
    default = next((m.text or "").strip() for m in grd.iter("message") if m.get("name") == MESSAGE)
    pak = _pak_locales(grd)
    translated: dict[str, list[str]] = {}
    for f in grd.iter("file"):
        path = f"components/{f.get('path')}"
        files.append(path)
        m = re.search(rf'<translation id="{MESSAGE}">([^<]*)</translation>', _git(checkout, "show", f"{TAG}:{path}"))
        if m:
            translated[pak[f.get("lang", "")]] = m.group(1).split(",")
    locales = desktop_locales(_git(checkout, "show", f"{TAG}:{LOCALES_GNI}"))
    return {
        "source": {
            "chromium": TAG,
            "files": {p: _git(checkout, "rev-parse", f"{TAG}:{p}").strip() for p in sorted(files)},
            "message": MESSAGE,
            "default": default,
        },
        "desktop_locales": locales,
        "accept_languages": {loc: translated.get(loc, default.split(",")) for loc in locales},
    }


@cache
def extract() -> dict:
    data = json.loads(EXTRACT.read_text())
    if data["source"]["chromium"] != TAG:
        raise ChromiumLocalesError(f"{EXTRACT}: built from {data['source']['chromium']}, pinned {TAG}; rebuild it")
    return data


def _chain(locale: str):
    """``locale`` and its CLDR ancestors, nearest first (es_MX, es_419, es)."""
    while locale and locale != "root":
        yield locale
        locale = cldr.parent_locale(locale) or locale.rpartition("_")[0]


def ui_locale(language: str, country: str) -> str:
    """Chrome's UI locale on an OS set to ``language`` (a CLDR code, possibly with a
    script: zh_Hant) in ``country``: de, DE -> de; es, MX -> es-419; zh_Hant, HK -> zh-TW."""
    desktop = set(extract()["desktop_locales"])
    for node in _chain(f"{language}_{country}"):
        tag = node.replace("_", "-")
        if node in ANCESTORS:
            return ANCESTORS[node]
        if tag in desktop:
            return tag
    return FALLBACK_LOCALE


def accept_languages(locale: str) -> list[str]:
    """The default intl.accept_languages of a desktop UI locale, in order."""
    try:
        return list(extract()["accept_languages"][locale])
    except KeyError:
        raise ChromiumLocalesError(f"{locale!r} is not a desktop UI locale of Chromium {TAG}") from None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m genprofile.chromium_locales <chromium checkout with tag " + TAG + ">", file=sys.stderr)
        return 2
    data = build(Path(argv[0]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {EXTRACT}: {len(data['desktop_locales'])} desktop UI locales")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
