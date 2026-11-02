# genprofile

Deterministic profile generator: a profile regenerates byte for byte from its seed and inputs. See `profiles/samples/README.md` for what each section comes from and the commands that rebuild the samples.

```bash
cd tools/genprofile
uv run python -m genprofile --os linux --seed <32 hex> --timezone Europe/Berlin --languages de-DE,de,en-US,en -o out.json
uv run python -m genprofile --os windows --seed <32 hex> --exit-ip 14.161.0.1 -o out.json
uv run pytest -q
```

## Location from the proxy exit IP

`--exit-ip <address>` (instead of `--timezone`) derives the location group from the proxy the profile will run behind (`genprofile/location.py`):

- `network.webrtc.public_ip`: the exit IP.
- `locale.timezone`: the zone of the database row's city coordinate (timezonefinder), written as its IANA primary name (`Asia/Ho_Chi_Minh`, never the backward link `Asia/Saigon`; CLDR's `iana` attribute). The boundary data follows tzdata's zone1970 regions, which can cross a border (north Vietnam lies in `Asia/Bangkok`'s polygon); a zone of another territory is replaced by the country's own when it has exactly one, and refused when it has several.
- `locale.geolocation`: `accuracy` is IP class, drawn from the seed in 25-100 km. The only measured stock answer is Google's IP-only fix on Linux Chrome 156 over ethernet, 75609.25 m; the range keeps that inside it, stops at the schema's 100 km cap and stays well above city scale. Wi-Fi-class accuracy (tens of metres) is unmeasured and never written. The position is drawn uniformly in the accuracy disc around the city coordinate, never the coordinate itself, and must lie in the city's boundary polygon (same zone, not at sea); a draw outside is retried with the next draw, up to 32, then generation fails.
- `locale.languages`: Chrome's default accept-languages (`IDS_ACCEPT_LANGUAGES`) for the UI locale of an OS set to a language of the country, the language drawn from the seed by population share among the country's official languages with modern CLDR coverage (all its modern-coverage languages when none is official). The UI locale is resolved as `l10n_util::GetApplicationLocale` does on Windows and Linux (CLDR parent locales, then the matcher's preferred locale: es-MX -> es-419, en-AU -> en-GB, pt -> pt-BR, zh-HK -> zh-TW, a language Chrome has no UI for -> en-US), so VN gives `vi-VN, vi, fr-FR, fr, en-US, en`, JP `ja, en-US, en`, MX `es-419, es` (`genprofile/chromium_locales.py`). `--languages` overrides it.

A row without a city (a country-only answer, whose coordinate is a country centroid) is refused, as are private addresses and addresses the database lacks.

Without `--exit-ip` nothing changes: no `geolocation`, no `public_ip`, languages default to `en-US,en`. **Generate Windows profiles with an exit IP.** Windows Chrome answers geolocation from the system location provider, so a Windows profile with no position is a stronger tell than a Linux one.

### Data and licences

- **DB-IP IP to City Lite** ([db-ip.com](https://db-ip.com)), licensed under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/) (verified on db-ip.com/db/download/ip-to-city-lite, 2026-10-27). IP Geolocation by [DB-IP](https://db-ip.com). The build is pinned in `dbip-city-lite.json` by URL, size and SHA-256 of the archive and of the decompressed mmdb (about 127 MB). It is never committed: `python -m genprofile.geoip` (or the first `--exit-ip` run) downloads it into `$XDG_CACHE_HOME/phasmid/geoip/<sha256>/` and refuses any mismatch. DB-IP serves only the current and previous month's builds; once the pinned one is gone, install a saved copy with `python -m genprofile.geoip --archive <file>` or re-pin.
- **timezonefinder** 9.2.0 (MIT) with **timezonefinder-data** 4.2026.5, pinned exactly in `pyproject.toml`. The data is derived from [timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder), which is built from OpenStreetMap data and licensed under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (verified in the timezonefinder-data package metadata). Generated profiles hold only a zone name per profile.
- **maxminddb** 3.2.0 (Apache-2.0), the mmdb reader.
- **Chromium** at the pinned tag (`chromium_version.txt`): `IDS_ACCEPT_LANGUAGES` from `components/components_locale_settings.grd` and its `components/strings/components_locale_settings_<lang>.xtb` translations, and the desktop UI locales of `build/config/locales.gni` (the same on Windows and Linux), each file named by its git blob id in `profiles/data/chromium/accept-languages.json` (BSD-style Chromium license); `python -m genprofile.chromium_locales <chromium checkout>` rebuilds it.
- **Unicode CLDR** `release-48`: time zone IDs (`common/bcp47/timezone.xml`), territory languages (`supplementalData.xml` territoryInfo), parent locales (`supplementalData.xml` parentLocales) and coverage levels, each pinned by SHA-256 in `genprofile/cldr.py`. The committed extract `profiles/data/cldr/cldr.json` is under the Unicode License v3 (`profiles/data/cldr/LICENSE`); `python -m genprofile.cldr` rebuilds it.

Tests use `fixtures/geoip-rows.json`, a few rows copied from the pinned build or hand-written for a case, each marked. `PHASMID_GEOIP_NETWORK=1 uv run pytest -q test_location.py` also runs the opt-in lookup in the real database.
