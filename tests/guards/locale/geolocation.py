"""Guard for patches/phasmid/locale/geolocation.patch.

A profile with locale.geolocation reports that position to a granted page,
with the latency, timestamps and re-reports of the provider stock Chrome runs
on the claimed OS. Geolocation is granted through the content setting (Preferences
profile.default_content_setting_values.geolocation = 1, written before the
browser starts): a CDP Browser.grantPermissions grant never reaches the
provider and hangs in stock Chrome and Phasmid alike.

  1. precondition: on a fresh user data directory permissions.query() says
     "prompt" in the main frame and in a cross-origin iframe with
     allow="geolocation"; with the content setting it says "granted", so the
     grant this guard relies on is in place;
  2. with a claimed position, for the profile's own OS and, from a linux
     profile, its Windows variant: getCurrentPosition() in the main frame,
     a same-origin iframe and a cross-origin iframe with allow="geolocation"
     returns the claimed latitude, longitude and accuracy, with altitude,
     altitudeAccuracy, heading and speed null; a cross-origin iframe without
     allow="geolocation" gets PERMISSION_DENIED, as in stock;
  3. the first fix after a fresh start arrives no sooner than the delay the
     seed gives (200 to 300 ms, core/profile.patch "phasmid/geolocation/1")
     and at most 60 ms after it, and two launches agree within 30 ms;
  4. watchPosition() from a fresh start: on Linux the first fix carries the
     start time (about the delay old on arrival) and is re-reported with a
     fresh timestamp about 2 s after start, then nothing; on Windows the fix
     is stamped on arrival and nothing follows within 4.5 s;
  5. a profile without locale.geolocation starts with one "host cannot back
     locale.geolocation" warning, and a granted page gets POSITION_UNAVAILABLE
     (code 2), as from an ungoogled build.

The refusals at load (out of range, (0, 0), an Etc/ zone with a position)
are in tests/guards/core/profile-loading.py.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure, expect_warned, finish, load_profile, parse_args, start_and_exit, windows_variant

# A claimed position that is no host's: the guard checks it is reported
# exactly, so the accuracy has a fraction.
CLAIM = {"latitude": 48.858370, "longitude": 2.294481, "accuracy": 1234.5}
# Slack for the IPC between the provider and the page, on top of the delay.
OVERHEAD_MS = 60
SAME_LAUNCH_MS = 30
WATCH_MS = 4500

MASK = (1 << 64) - 1


# ---- components/phasmid/noise.cc DeriveNoiseKey and Profile::Init ----

def _rotl(x: int, b: int) -> int:
    return ((x << b) | (x >> (64 - b))) & MASK


def siphash24(k0: int, k1: int, data: bytes) -> int:
    v = [0x736F6D6570736575 ^ k0, 0x646F72616E646F6D ^ k1, 0x6C7967656E657261 ^ k0, 0x7465646279746573 ^ k1]

    def rnd() -> None:
        v[0] = (v[0] + v[1]) & MASK; v[1] = _rotl(v[1], 13) ^ v[0]; v[0] = _rotl(v[0], 32)  # noqa: E702
        v[2] = (v[2] + v[3]) & MASK; v[3] = _rotl(v[3], 16) ^ v[2]  # noqa: E702
        v[0] = (v[0] + v[3]) & MASK; v[3] = _rotl(v[3], 21) ^ v[0]  # noqa: E702
        v[2] = (v[2] + v[1]) & MASK; v[1] = _rotl(v[1], 17) ^ v[2]; v[2] = _rotl(v[2], 32)  # noqa: E702

    def absorb(m: int) -> None:
        v[3] ^= m
        rnd()
        rnd()
        v[0] ^= m

    tail = len(data) % 8
    for i in range(0, len(data) - tail, 8):
        absorb(int.from_bytes(data[i:i + 8], "little"))
    absorb(((len(data) & 0xFF) << 56) | int.from_bytes(data[len(data) - tail:], "little"))
    v[2] ^= 0xFF
    for _ in range(4):
        rnd()
    return v[0] ^ v[1] ^ v[2] ^ v[3]


def fix_delay_ms(seed_hex: str) -> int:
    """Profile::Geolocation::fix_delay_ms: 200 + k0 % 101, k0 the first half
    of DeriveNoiseKey(seed, "phasmid/geolocation/1")."""
    if siphash24(0x0706050403020100, 0x0F0E0D0C0B0A0908, b"") != 0x726FDB47DD0E0E31:
        raise GuardFailure("SipHash-2-4 model disagrees with the reference vector")
    raw = bytes.fromhex(seed_hex)
    k0 = siphash24(int.from_bytes(raw[:8], "little"), int.from_bytes(raw[8:], "little"), b"phasmid/geolocation/1\0")
    return 200 + k0 % 101


# ---- page scripts ----

ADD_FRAME = """([src, allow]) => new Promise((res) => {
  const f = document.createElement("iframe");
  if (allow) f.allow = allow;
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""
QUERY = "async () => (await navigator.permissions.query({name: 'geolocation'})).state"
GET = """() => new Promise((res) => {
  const t0 = performance.now();
  navigator.geolocation.getCurrentPosition(
    (p) => res({ok: true, ms: performance.now() - t0, age: Date.now() - p.timestamp,
                coords: {latitude: p.coords.latitude, longitude: p.coords.longitude, accuracy: p.coords.accuracy,
                         altitude: p.coords.altitude, altitudeAccuracy: p.coords.altitudeAccuracy,
                         heading: p.coords.heading, speed: p.coords.speed}}),
    (e) => res({ok: false, code: e.code, message: e.message, ms: performance.now() - t0}),
    {timeout: 10000, maximumAge: 0});
})"""
WATCH = """(ms) => new Promise((res) => {
  const t0 = performance.now();
  const fires = [];
  const id = navigator.geolocation.watchPosition(
    (p) => fires.push({t: performance.now() - t0, age: Date.now() - p.timestamp}),
    (e) => fires.push({t: performance.now() - t0, code: e.code, message: e.message}));
  setTimeout(() => { navigator.geolocation.clearWatch(id); res(fires); }, ms);
})"""


def seeded_user_data_dir(work: Path) -> Path:
    """A user data directory whose Default profile allows geolocation for
    every site, as the content setting a user sets in chrome://settings."""
    udd = Path(tempfile.mkdtemp(prefix="geo-ud-", dir=work))
    (udd / "Default").mkdir()
    prefs = {"profile": {"default_content_setting_values": {"geolocation": 1}}}
    (udd / "Default" / "Preferences").write_text(json.dumps(prefs))
    return udd


def session(binary: str, profile: Path, work: Path, *, headless: bool, granted: bool, steps):
    """Launch, open origin A, run `steps(page, servers)`. A granted session
    uses a pre-seeded user data directory (the later --user-data-dir wins)."""
    extra = [f"--user-data-dir={seeded_user_data_dir(work)}"] if granted else []
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=extra)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            return steps(page, servers)
        finally:
            launch.stop()


def frames(page, servers) -> dict:
    page.evaluate(ADD_FRAME, [f"{servers.a.url}/h/same", ""])
    page.evaluate(ADD_FRAME, [f"{servers.b.url}/h/cross", "geolocation"])
    page.evaluate(ADD_FRAME, [f"{servers.b.url}/h/cross-blocked", ""])
    out = {"main": page.main_frame}
    for f in page.frames:
        for name, path in (("iframe_same", "/h/same"), ("iframe_cross", "/h/cross"), ("iframe_cross_blocked", "/h/cross-blocked")):
            if f.url.endswith(path):
                out[name] = f
    if len(out) != 4:
        raise GuardFailure(f"frames missing: {sorted(out)}")
    return out


def write(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def with_claim(profile: dict, claim: dict | None) -> dict:
    p = copy.deepcopy(profile)
    p["locale"].pop("geolocation", None)
    if claim is not None:
        p["locale"]["geolocation"] = dict(claim)
    return p


def expected_coords() -> dict:
    return {"latitude": CLAIM["latitude"], "longitude": CLAIM["longitude"], "accuracy": CLAIM["accuracy"],
            "altitude": None, "altitudeAccuracy": None, "heading": None, "speed": None}


def check_permission_default(binary: str, profile: Path, work: Path, headless: bool) -> None:
    """Check 1."""
    def query(page, servers):
        f = frames(page, servers)
        return {name: f[name].evaluate(QUERY) for name in ("main", "iframe_cross")}

    fresh = session(binary, profile, work, headless=headless, granted=False, steps=query)
    if fresh != {"main": "prompt", "iframe_cross": "prompt"}:
        raise GuardFailure(f"a fresh profile's permissions.query geolocation is {fresh}, stock says prompt")
    print("PASS: permissions.query geolocation is 'prompt' on a fresh profile (main, cross-origin iframe)")
    granted = session(binary, profile, work, headless=headless, granted=True, steps=query)
    if granted != {"main": "granted", "iframe_cross": "granted"}:
        raise GuardFailure(f"vacuous: the pre-seeded content setting does not grant geolocation: {granted}")
    print("PASS: precondition: the pre-seeded content setting grants geolocation (main, cross-origin iframe)")


def check_unclaimed(binary: str, base: dict, work: Path, headless: bool) -> None:
    """Check 5."""
    unclaimed = with_claim(base, None)
    r = start_and_exit(binary, unclaimed, work, "geolocation-absent")
    expect_warned(r, "no locale.geolocation", "locale.geolocation", "no position claimed", "POSITION_UNAVAILABLE")
    got = session(binary, write(unclaimed, work, "geolocation-absent"), work, headless=headless, granted=True,
                  steps=lambda page, servers: page.evaluate(GET))
    if got.get("ok") or got.get("code") != 2:
        raise GuardFailure(f"no locale.geolocation: a granted page got {got}, expected POSITION_UNAVAILABLE (code 2) as from an ungoogled build")
    print(f"PASS: no locale.geolocation: a granted page gets POSITION_UNAVAILABLE ({got.get('message')!r})")


def check_claim(binary: str, profile: dict, work: Path, headless: bool, label: str) -> None:
    """Checks 2 to 4 for one OS."""
    claimed = with_claim(profile, CLAIM)
    path = write(claimed, work, f"geolocation-{label}")
    delay = fix_delay_ms(claimed["seed"])
    linux = claimed["os"]["family"] == "linux"

    def watch_then_frames(page, servers):
        fires = page.evaluate(WATCH, WATCH_MS)
        f = frames(page, servers)
        return fires, {name: frame.evaluate(GET) for name, frame in f.items()}

    fires, results = session(binary, path, work, headless=headless, granted=True, steps=watch_then_frames)
    want = expected_coords()
    for name in ("main", "iframe_same", "iframe_cross"):
        got = results[name]
        if not got.get("ok") or got["coords"] != want:
            raise GuardFailure(f"{label}: {name}: getCurrentPosition() gave {got}, expected {want}")
    print(f"PASS: {label}: getCurrentPosition() reports {CLAIM} with altitude, altitudeAccuracy, heading and speed null "
          f"in main, iframe_same, iframe_cross")
    blocked = results["iframe_cross_blocked"]
    if blocked.get("ok") or blocked.get("code") != 1:
        raise GuardFailure(f"{label}: a cross-origin iframe without allow=\"geolocation\" gave {blocked}, stock gives PERMISSION_DENIED")
    print(f"PASS: {label}: a cross-origin iframe without allow=\"geolocation\" gets PERMISSION_DENIED")

    errors = [f for f in fires if "code" in f]
    if errors or not fires:
        raise GuardFailure(f"{label}: watchPosition() from a fresh start gave {fires}")
    first = fires[0]
    if not delay <= first["t"] <= delay + OVERHEAD_MS:
        raise GuardFailure(f"{label}: first fix after {first['t']:.0f} ms, the seed's delay is {delay} ms (allowed {delay} to {delay + OVERHEAD_MS})")
    print(f"PASS: {label}: first fix after {first['t']:.0f} ms (seed delay {delay} ms)")
    if linux:
        # NetworkLocationProvider: the fix carries the start time; RequestPosition
        # re-reports it with a fresh timestamp 2 s after start.
        if not delay - 10 <= first["age"] <= delay + OVERHEAD_MS:
            raise GuardFailure(f"{label}: the first fix is {first['age']} ms old on arrival, expected about {delay} ms (stamped at start)")
        later = fires[1:]
        if len(later) != 1 or not 1950 <= later[0]["t"] <= 2000 + OVERHEAD_MS + 50 or later[0]["age"] > OVERHEAD_MS:
            raise GuardFailure(f"{label}: expected one fresh re-report about 2 s after start and nothing else in {WATCH_MS} ms, got {fires}")
        print(f"PASS: {label}: the first fix is {first['age']} ms old (start time); re-reported fresh at {later[0]['t']:.0f} ms, then nothing")
    else:
        # LocationProviderWinrt::CreateGeoposition stamps the fix on arrival.
        if first["age"] > OVERHEAD_MS:
            raise GuardFailure(f"{label}: the first fix is {first['age']} ms old on arrival, WinRT stamps it on arrival")
        if len(fires) != 1:
            raise GuardFailure(f"{label}: expected one fix and nothing else in {WATCH_MS} ms, got {fires}")
        print(f"PASS: {label}: the fix is {first['age']} ms old (stamped on arrival) and nothing follows in {WATCH_MS} ms")

    again = session(binary, path, work, headless=headless, granted=True, steps=lambda page, servers: page.evaluate(GET))
    if not again.get("ok") or abs(again["ms"] - first["t"]) > SAME_LAUNCH_MS:
        raise GuardFailure(f"{label}: a second launch's first fix took {again.get('ms')} ms, the first launch's {first['t']:.0f} ms")
    print(f"PASS: {label}: a second launch's first fix took {again['ms']:.0f} ms, the first's {first['t']:.0f} ms")


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    claimed = write(with_claim(base, CLAIM), work, "geolocation-permission")
    check_permission_default(args.binary, claimed, work, headless)
    check_claim(args.binary, base, work, headless, base["os"]["family"])
    if base["os"]["family"] == "linux":
        check_claim(args.binary, windows_variant(base), work, headless, "windows")
    check_unclaimed(args.binary, base, work, headless)


finish(main)
