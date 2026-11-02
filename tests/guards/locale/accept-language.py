"""Guard for patches/phasmid/locale/accept-language.patch.

locale.languages is navigator.languages (and its first entry
navigator.language) in the main frame, both iframes, and all three worker
kinds; the Accept-Language header on navigation, page, worker and service
worker requests is derived from it with Chrome's q-value algorithm; and the
default Intl locale is locale.intl_locale, or languages[0] when the profile
has none. The values are chosen to differ from the host so the checks cannot
pass by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import GuardFailure, dump, expect_everywhere, expect_header, finish, load_profile, parse_args

from consistency.compare import accept_language

# (languages, intl_locale): well-known tags present in Chrome's ICU data.
CANDIDATES = [(["fr-CH", "fr", "de"], "de-CH"), (["nl-BE", "nl", "fr"], "fr-BE")]
HEADER_SLOTS = ("navigation", "fetch_main", "fetch_worker", "fetch_service_worker")


def write_profile(base: dict, work: Path, name: str, languages: list[str], intl_locale: str | None) -> str:
    profile = json.loads(json.dumps(base))
    profile["locale"]["languages"] = languages
    if intl_locale is None:
        profile["locale"].pop("intl_locale", None)
    else:
        profile["locale"]["intl_locale"] = intl_locale
    path = work / name
    path.write_text(json.dumps(profile))
    return str(path)


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)

    # Precondition: the host must not already report the values under test.
    plain = dump(args.binary, None)
    host_main = plain["contexts"]["main"]
    host_languages = host_main["navigator"]["languages"]
    host_locale = host_main["intl"]["dateTimeFormat"]["locale"]
    host_header = plain["headers"].get("navigation", {}).get("accept-language")
    candidates = [
        (langs, intl)
        for langs, intl in CANDIDATES
        if langs != host_languages and accept_language(langs) != host_header and host_locale not in (intl, langs[0])
    ]
    if not candidates:
        raise GuardFailure(f"vacuous: every candidate matches the host (languages {host_languages!r}, Intl locale {host_locale!r})")
    languages, intl_locale = candidates[0]
    header = accept_language(languages)

    d = dump(args.binary, write_profile(base, work, "profile.json", languages, intl_locale))
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "navigator.languages", languages)
    expect_everywhere(d, "navigator.language", languages[0])
    expect_everywhere(d, "intl.dateTimeFormat.locale", intl_locale, label="Intl locale from locale.intl_locale")
    for slot in HEADER_SLOTS:
        expect_header(d, slot, "accept-language", header)

    d = dump(args.binary, write_profile(base, work, "profile-no-intl.json", languages, None))
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed without intl_locale: {d['context_errors']}")
    expect_everywhere(d, "navigator.languages", languages, label="navigator.languages without intl_locale")
    expect_everywhere(d, "intl.dateTimeFormat.locale", languages[0], label="Intl locale falls back to languages[0]")


finish(main)
