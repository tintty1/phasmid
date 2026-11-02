"""The location group from a proxy exit IP: zone, position, WebRTC address, languages.

The exit IP is looked up in the pinned DB-IP City Lite build (geoip.py). A row
without a city is refused: the country centroid it carries is a well-known
tell. The row's city coordinate gives the zone (pinned ``timezonefinder`` and
its ODbL boundary data), named by its IANA primary name (cldr.py). The served
position is drawn from the seed inside the accuracy circle around the city and
must lie in the same zone; a draw that does not is retried with the next draw a
bounded number of times. Languages are Chrome's default accept-languages for
the UI locale of a language of the country (CLDR's territoryInfo, drawn from
the seed; chromium_locales.py); ``--languages`` overrides them.

Accuracy is IP class only. The one measured stock answer is Google's IP-only
fix on Linux Chrome 156 over ethernet, 75609.25 m; Wi-Fi-class answers (tens
of metres) are unmeasured and never emitted.
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass
from functools import cache
from importlib.metadata import version
from typing import Any, Protocol

from timezonefinder import TimezoneFinder

from . import chromium_locales, cldr, forge

# Metres. Brackets the measured 75609.25 m IP-only fix: from a third of it up to the schema's cap.
ACCURACY_RANGE = (25_000.0, 100_000.0)
_JITTER_ATTEMPTS = 32
_EARTH_RADIUS_M = 6_371_008.8
_OFFICIAL = ("official", "de_facto_official")


class LocationError(Exception):
    pass


class Reader(Protocol):
    def get(self, ip_address: str, /) -> Any: ...


@dataclass(frozen=True)
class Location:
    public_ip: str
    country: str
    city: str
    timezone: str
    latitude: float
    longitude: float
    accuracy: float
    languages: list[str]

    def geolocation(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude, "accuracy": self.accuracy}


@cache
def _finder() -> TimezoneFinder:
    return TimezoneFinder()


def boundary_zone(latitude: float, longitude: float) -> str | None:
    """The IANA primary name of the boundary polygon containing the point; None at sea
    (Etc/GMT±n) or nowhere."""
    name = _finder().timezone_at(lng=longitude, lat=latitude)
    if name is None or name.startswith("Etc/"):
        return None
    return cldr.canonical_zone(name)


def device_zone(boundary: str, country: str) -> str:
    """The zone a device in ``country`` inside ``boundary`` is set to.

    The boundary data follows tzdata's zone1970 regions, which can cross a border: north
    Vietnam lies in Asia/Bangkok's polygon, while a Vietnamese device is set to its country's
    zone, Asia/Ho_Chi_Minh (CLDR has no Thai zone for VN). A zone of another territory is
    replaced by the country's own when it has exactly one; a country with none (XK) keeps the
    boundary's zone, and one with several is refused rather than guessed.
    """
    if cldr.zone_region(boundary) == country:
        return boundary
    own = cldr.region_zones(country)
    if not own:
        return boundary
    if len(own) == 1:
        return own[0]
    raise LocationError(f"{boundary} ({cldr.zone_region(boundary)}) is not one of {country}'s zones {', '.join(own)}")


def provenance() -> str:
    return (f"timezonefinder {version('timezonefinder')} with timezonefinder-data "
            f"{version('timezonefinder-data')} (ODbL), CLDR {cldr.TAG}")


def normalize_ip(exit_ip: str) -> str:
    try:
        ip = ipaddress.ip_address(exit_ip.strip())
    except ValueError:
        raise LocationError(f"exit IP {exit_ip!r} is not an IPv4 or IPv6 address") from None
    if not ip.is_global:
        raise LocationError(f"exit IP {ip} is not a public address")
    return ip.compressed


def _row(reader: Reader, ip: str) -> tuple[str, str, float, float]:
    rec = reader.get(ip)
    if not rec:
        raise LocationError(f"exit IP {ip} is not in the database")
    country = (rec.get("country") or {}).get("iso_code")
    city = ((rec.get("city") or {}).get("names") or {}).get("en")
    loc = rec.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if not city:
        raise LocationError(
            f"exit IP {ip} resolves only to a country ({country or 'unknown'}), with no city: its coordinate "
            "is a country centroid, which no real location answer gives; use an exit with a city-level row")
    if not country or lat is None or lon is None:
        raise LocationError(f"exit IP {ip}: the database row for {city} has no country or coordinate")
    return country, city, float(lat), float(lon)


def _destination(lat: float, lon: float, bearing: float, distance: float) -> tuple[float, float]:
    """The point ``distance`` metres from (lat, lon) along ``bearing`` radians, on a sphere."""
    d = distance / _EARTH_RADIUS_M
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(bearing))
    l2 = l1 + math.atan2(math.sin(bearing) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def position(seed: str, lat: float, lon: float, boundary: str) -> tuple[float, float, float]:
    """(latitude, longitude, accuracy): accuracy drawn in ACCURACY_RANGE, the point uniform in
    the disc of that radius around the city and inside the city's boundary polygon (so not
    across a zone border, a national border between zones, or at sea)."""
    lo, hi = ACCURACY_RANGE
    accuracy = round(forge.decision_rng(seed, "geo-accuracy").uniform(lo, hi), 2)
    for attempt in range(_JITTER_ATTEMPTS):
        rng = forge.decision_rng(seed, f"geo-position@{attempt}")
        bearing, r = 2 * math.pi * rng.random(), accuracy * math.sqrt(rng.random())
        plat, plon = _destination(lat, lon, bearing, r)
        plat, plon = round(plat, 6), round(plon, 6)
        if (plat, plon) == (0.0, 0.0) or distance_m((lat, lon), (plat, plon)) > accuracy:
            continue
        if boundary_zone(plat, plon) == boundary:
            return plat, plon, accuracy
    raise LocationError(
        f"no position within {accuracy} m of ({lat}, {lon}) lies in {boundary} after {_JITTER_ATTEMPTS} draws")


def languages_for(seed: str, country: str) -> list[str]:
    """Chrome's default accept-languages on a device whose OS is set to a language of the country:
    a modern-coverage language drawn by population share among its official ones (all of its
    listed ones when none is official), resolved to Chrome's UI locale (vi, VN -> vi; es, MX ->
    es-419), whose IDS_ACCEPT_LANGUAGES is the list (vi -> vi-VN, vi, fr-FR, fr, en-US, en)."""
    rows = cldr.extract()["territories"].get(country, [])
    candidates = [r for r in rows if r[2] in _OFFICIAL] or rows
    if not candidates:
        raise LocationError(f"CLDR {cldr.TAG} lists no modern-coverage language for {country}; pass --languages")
    lang = forge.decision_rng(seed, "geo-language").choices(
        [r[0] for r in candidates], weights=[r[1] for r in candidates])[0]
    return chromium_locales.accept_languages(chromium_locales.ui_locale(lang, country))


def locate(exit_ip: str, seed: str, reader: Reader) -> Location:
    ip = normalize_ip(exit_ip)
    country, city, lat, lon = _row(reader, ip)
    boundary = boundary_zone(lat, lon)
    if boundary is None:
        raise LocationError(f"exit IP {ip}: {city}, {country} ({lat}, {lon}) lies in no named time zone")
    zone = device_zone(boundary, country)
    plat, plon, accuracy = position(seed, lat, lon, boundary)
    return Location(ip, country, city, zone, plat, plon, accuracy, languages_for(seed, country))
