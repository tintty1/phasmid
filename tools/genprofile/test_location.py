"""Tests for the exit-IP location derivation. No network: lookups use fixtures/geoip-rows.json
(marked fixture rows); the real database is read only by the opt-in test at the end
(PHASMID_GEOIP_NETWORK=1), which downloads the pinned build when it is not cached."""

from __future__ import annotations

import copy
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path

import jsonschema
import pytest

from genprofile import __main__ as cli
from genprofile import chromium_locales, cldr, geoip
from genprofile.captures import harness, validate
from genprofile.generate import GenerationError, dumps, generate
from genprofile.location import (ACCURACY_RANGE, LocationError, boundary_zone, device_zone, distance_m, languages_for,
                                 locate, position)

HERE = Path(__file__).parent
SEEDS = ("0a1b2c3d4e5f60718293a4b5c6d7e8f9", "5f3c9e1a7b2d4086c1e9a3f5b7d20e48", "c4e8a2f6b1d3957e0f2a4c6e8b1d3f57")
HANOI, GENEVA, FALKENSTEIN, MONTREAL_V6, COUNTRY_ONLY = "14.161.0.1", "46.14.0.9", "5.9.0.1", "2001:4860:4860::8888", "8.8.8.8"
# The locale.geolocation shape the browser validates.
GEOLOCATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["latitude", "longitude", "accuracy"],
    "properties": {
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "accuracy": {"type": "number", "minimum": 10, "maximum": 100000},
    },
}


class FixtureReader:
    """The maxminddb Reader.get interface over the fixture rows."""

    def __init__(self) -> None:
        rows = json.loads((HERE / "fixtures" / "geoip-rows.json").read_text())["rows"]
        self.rows = [(ipaddress.ip_network(r["network"]), r["record"]) for r in rows]

    def get(self, ip: str):
        addr = ipaddress.ip_address(ip)
        return next((rec for net, rec in self.rows if addr in net), None)


READER = FixtureReader()


@pytest.fixture
def schema_with_geolocation(monkeypatch):
    """The profile schema, with locale.geolocation added if it lacks the field."""
    module = harness()
    schema = module.load_schema()
    locale = schema["properties"]["locale"]["properties"]
    if "geolocation" not in locale:
        patched = copy.deepcopy(schema)
        patched["properties"]["locale"]["properties"]["geolocation"] = GEOLOCATION_SCHEMA
        monkeypatch.setattr(module, "load_schema", lambda: patched)


@pytest.mark.parametrize("seed", SEEDS)
def test_same_seed_gives_the_same_location(seed):
    assert locate(HANOI, seed, READER) == locate(HANOI, seed, FixtureReader())


def test_different_seeds_give_different_positions():
    assert len({(loc.latitude, loc.longitude, loc.accuracy) for loc in (locate(HANOI, s, READER) for s in SEEDS)}) == 3


@pytest.mark.parametrize("seed", SEEDS)
def test_profile_from_exit_ip_is_byte_identical_and_schema_valid(seed, schema_with_geolocation):
    loc = locate(HANOI, seed, READER)
    a = generate("linux", seed, loc.languages, loc.timezone, location=loc, location_note="fixture rows")
    b = generate("linux", seed, loc.languages, loc.timezone, location=loc, location_note="fixture rows")
    assert dumps(a) == dumps(b)
    assert validate(a) == []
    assert a["locale"]["timezone"] == "Asia/Ho_Chi_Minh"
    assert a["locale"]["geolocation"] == loc.geolocation()
    assert a["network"]["webrtc"] == {"ip_handling_policy": "default", "public_ip": HANOI}
    assert "proxy exit IP: Hanoi, VN" in a["captured"]["note"]


def test_a_timezone_that_contradicts_the_exit_ip_is_refused():
    loc = locate(HANOI, SEEDS[0], READER)
    with pytest.raises(GenerationError, match="differs from the exit IP's zone"):
        generate("linux", SEEDS[0], loc.languages, "Asia/Bangkok", location=loc)


@pytest.mark.parametrize("alias, primary", [
    ("Asia/Saigon", "Asia/Ho_Chi_Minh"),
    ("Asia/Calcutta", "Asia/Kolkata"),
    ("Europe/Kiev", "Europe/Kyiv"),
    ("US/Eastern", "America/New_York"),
    ("Asia/Ho_Chi_Minh", "Asia/Ho_Chi_Minh"),
    ("Europe/Amsterdam", "Europe/Amsterdam"),  # merged into Brussels by tzdata, kept apart by CLDR and ICU
])
def test_zone_aliases_resolve_to_the_iana_primary_name(alias, primary):
    assert cldr.canonical_zone(alias) == primary


def test_every_zone_name_written_is_its_own_primary():
    primaries = set(cldr.extract()["zones"].values())
    assert all(cldr.canonical_zone(z) == z for z in primaries)
    assert "Asia/Saigon" not in primaries and "Asia/Calcutta" not in primaries


def test_north_vietnam_gets_the_vietnamese_zone_not_the_boundary_polygon():
    """timezonefinder puts Hanoi in Asia/Bangkok's polygon; a Vietnamese device is on Asia/Ho_Chi_Minh."""
    assert boundary_zone(21.0278, 105.834) == "Asia/Bangkok"
    assert device_zone("Asia/Bangkok", "VN") == "Asia/Ho_Chi_Minh"
    assert device_zone("Europe/Belgrade", "XK") == "Europe/Belgrade"  # no zone of its own
    with pytest.raises(LocationError, match="not one of US's zones"):
        device_zone("America/Toronto", "US")


def test_a_country_only_row_is_refused():
    with pytest.raises(LocationError, match="resolves only to a country"):
        locate(COUNTRY_ONLY, SEEDS[0], READER)


@pytest.mark.parametrize("ip", ["10.0.0.1", "192.168.1.1", "not-an-ip", "::1"])
def test_private_and_malformed_addresses_are_refused(ip):
    with pytest.raises(LocationError):
        locate(ip, SEEDS[0], READER)


def test_an_address_missing_from_the_database_is_refused():
    with pytest.raises(LocationError, match="not in the database"):
        locate("1.1.1.1", SEEDS[0], READER)


@pytest.mark.parametrize("ip, city", [(HANOI, (21.0278, 105.834)), (GENEVA, (46.2044, 6.1432)),
                                      (FALKENSTEIN, (50.475, 12.365)), (MONTREAL_V6, (45.5019, -73.5674))])
def test_position_is_jittered_inside_the_accuracy_circle_and_the_city_zone(ip, city):
    for i in range(12):
        loc = locate(ip, f"{i:032x}", READER)
        assert ACCURACY_RANGE[0] <= loc.accuracy <= ACCURACY_RANGE[1]
        assert 0 < distance_m(city, (loc.latitude, loc.longitude)) <= loc.accuracy
        assert boundary_zone(loc.latitude, loc.longitude) == boundary_zone(*city)
        assert jsonschema.Draft202012Validator(GEOLOCATION_SCHEMA).is_valid(loc.geolocation())


def test_geneva_jitter_retries_out_of_france():
    """Most of a 25-100 km disc around Geneva is in France: some seed needs more than one draw."""
    first_draw_zones = set()
    for i in range(12):
        seed = f"{i:032x}"
        loc = locate(GENEVA, seed, READER)
        assert (loc.timezone, boundary_zone(loc.latitude, loc.longitude)) == ("Europe/Zurich", "Europe/Zurich")
        first_draw_zones.add(_first_draw_zone(seed, 46.2044, 6.1432, loc.accuracy))
    assert "Europe/Paris" in first_draw_zones


def _first_draw_zone(seed: str, lat: float, lon: float, accuracy: float) -> str | None:
    import math

    from genprofile import forge
    from genprofile.location import _destination

    rng = forge.decision_rng(seed, "geo-position@0")
    bearing, r = 2 * math.pi * rng.random(), accuracy * math.sqrt(rng.random())
    return boundary_zone(*(round(v, 6) for v in _destination(lat, lon, bearing, r)))


def test_jitter_that_never_stays_in_zone_fails_loudly(monkeypatch):
    from genprofile import location

    monkeypatch.setattr(location, "boundary_zone", lambda lat, lon: "Europe/Paris")
    with pytest.raises(LocationError, match="after 32 draws"):
        position(SEEDS[0], 46.2044, 6.1432, "Europe/Zurich")


def test_languages_are_chromes_default_for_the_countrys_ui_locale():
    # IDS_ACCEPT_LANGUAGES of the UI locale, components/strings/components_locale_settings_<lang>.xtb at 155.0.8059.84.
    assert languages_for(SEEDS[0], "VN") == ["vi-VN", "vi", "fr-FR", "fr", "en-US", "en"]
    assert languages_for(SEEDS[0], "DE") == ["de-DE", "de", "en-US", "en"]
    assert languages_for(SEEDS[0], "JP") == ["ja", "en-US", "en"]
    assert languages_for(SEEDS[0], "US") == ["en-US", "en"]
    assert languages_for(SEEDS[0], "BR") == ["pt-BR", "pt", "en-US", "en"]
    assert languages_for(SEEDS[0], "TW") == ["zh-TW", "zh", "en-US", "en"]
    assert languages_for(SEEDS[0], "MX") == ["es-419", "es"]
    swiss = {languages_for(s, "CH")[0] for s in (f"{i:032x}" for i in range(40))}
    assert swiss == {"de-DE", "fr-FR", "it-IT"}  # official, modern coverage (no gsw, no rm); Chrome has no -CH UI


@pytest.mark.parametrize(("language", "country", "locale"), [
    # ui/base/l10n/l10n_util_unittest.cc at 155.0.8059.84 (GetApplicationLocale).
    ("en", "US", "en-US"), ("en", "GB", "en-GB"), ("en", "AU", "en-GB"), ("en", "NZ", "en-GB"),
    ("en", "CA", "en-GB"), ("en", "ZA", "en-GB"), ("en", "LR", "en-US"), ("en", "PH", "en-US"),
    ("es", "MX", "es-419"), ("es", "AR", "es-419"), ("es", "ES", "es"), ("fr", "CA", "fr"),
    ("pt", "PT", "pt-PT"), ("pt", "BR", "pt-BR"), ("pt", "AO", "pt-PT"), ("zh_Hant", "HK", "zh-TW"),
    ("zh_Hant", "MO", "zh-TW"), ("zh", "SG", "zh-CN"), ("zh", "CN", "zh-CN"), ("no", "NO", "nb"),
    ("ca", "ES", "ca"), ("de", "AT", "de"), ("he", "IL", "he"), ("fil", "PH", "fil"),
    # No desktop UI in that language: GetApplicationLocale falls back to en-US.
    ("rm", "CH", "en-US"),
])
def test_ui_locale_resolves_as_chrome_does(language, country, locale):
    assert chromium_locales.ui_locale(language, country) == locale


def test_accept_language_table_is_the_pinned_chromiums():
    data = chromium_locales.extract()
    assert data["source"]["chromium"] == (cldr.REPO / "chromium_version.txt").read_text().strip()
    assert set(data["accept_languages"]) == set(data["desktop_locales"])
    assert data["accept_languages"]["en-US"] == ["en-US", "en"]
    assert data["accept_languages"]["ms"] == ["en-US", "en"]  # ms.xtb has no translation: grit's English text
    assert data["accept_languages"]["nb"] == ["nb-NO", "nb", "no", "nn", "en-US", "en"]  # the "no" xtb, as nb.pak


def _fake_reader(monkeypatch):
    monkeypatch.setattr(geoip, "reader", lambda: READER)


def test_cli_derives_the_location_group(tmp_path, monkeypatch, schema_with_geolocation):
    _fake_reader(monkeypatch)
    out = tmp_path / "p.json"
    assert cli.main(["--os", "linux", "--seed", SEEDS[0], "--exit-ip", HANOI, "-o", str(out)]) == 0
    p = json.loads(out.read_text())
    loc = locate(HANOI, SEEDS[0], READER)
    assert p["locale"] == {"languages": loc.languages, "timezone": "Asia/Ho_Chi_Minh", "geolocation": loc.geolocation()}
    assert p["network"]["webrtc"]["public_ip"] == HANOI
    assert "DB-IP IP to City Lite 2026-10" in p["captured"]["note"] and "languages from CLDR" in p["captured"]["note"]


def test_cli_languages_override_the_country(tmp_path, monkeypatch, schema_with_geolocation):
    _fake_reader(monkeypatch)
    out = tmp_path / "p.json"
    assert cli.main(["--os", "linux", "--seed", SEEDS[0], "--exit-ip", HANOI, "--languages", "en-US,en",
                     "-o", str(out)]) == 0
    p = json.loads(out.read_text())
    assert p["locale"]["languages"] == ["en-US", "en"] and p["locale"]["timezone"] == "Asia/Ho_Chi_Minh"
    assert "languages from the command line" in p["captured"]["note"]


def test_cli_refuses_a_country_only_exit(tmp_path, monkeypatch, capsys):
    _fake_reader(monkeypatch)
    assert cli.main(["--os", "linux", "--seed", SEEDS[0], "--exit-ip", COUNTRY_ONLY, "-o", str(tmp_path / "p.json")]) == 1
    assert "resolves only to a country" in capsys.readouterr().err


def test_cli_timezone_and_exit_ip_are_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--os", "linux", "--seed", SEEDS[0], "--exit-ip", HANOI, "--timezone", "UTC", "-o", str(tmp_path / "p")])


def test_without_an_exit_ip_nothing_changes():
    p = generate("linux", SEEDS[0], ["en-US", "en"], "America/New_York")
    assert "geolocation" not in p["locale"] and "public_ip" not in p["network"]["webrtc"]
    assert p["captured"]["note"].endswith("; locale from the command line.")


def test_geoip_install_checks_the_pin(tmp_path):
    mmdb = b"not really an mmdb"
    archive = tmp_path / "a.mmdb.gz"
    archive.write_bytes(gzip.compress(mmdb, mtime=0))
    data = archive.read_bytes()
    pin = geoip.Pin("t", "2026-10", "https://example.invalid/a.mmdb.gz", len(data), hashlib.sha256(data).hexdigest(),
                    len(mmdb), hashlib.sha256(mmdb).hexdigest(), "CC BY 4.0", "t")
    path = geoip.install(archive, pin, tmp_path / "cache")
    assert path.read_bytes() == mmdb
    with pytest.raises(geoip.GeoipError, match="differs from the pin"):
        geoip.install(archive, geoip.Pin(**{**pin.__dict__, "sha256": "0" * 64}), tmp_path / "cache")
    with pytest.raises(geoip.GeoipError, match="differs from the pin"):
        geoip.install(archive, geoip.Pin(**{**pin.__dict__, "mmdb_sha256": "0" * 64}), tmp_path / "cache2")
    assert not list((tmp_path / "cache2").glob(".partial-*"))


@pytest.mark.skipif(os.environ.get("PHASMID_GEOIP_NETWORK") != "1",
                    reason="opt-in: reads the real pinned DB-IP build (downloads it when not cached)")
def test_real_database_vietnam():
    loc = locate(HANOI, SEEDS[0], geoip.reader())
    assert (loc.country, loc.city, loc.timezone) == ("VN", "Hanoi", "Asia/Ho_Chi_Minh")
    assert loc.languages[:2] == ["vi-VN", "vi"]
