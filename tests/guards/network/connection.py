"""Guard for patches/phasmid/network/connection.patch.

network.connection replaces the network service's estimate everywhere a page
can read it, and stock Chrome's per-host noise is keyed on the profile seed,
so JavaScript and every header agree:

  0. self-check: the SipHash-2-4 and key derivation below reproduce
     components/phasmid/noise.cc (reference vector, frozen canvas key);
  1. precondition, no profile, --force-effective-connection-type=Slow-2G: the
     forced estimate reaches navigator.connection and the ECT header, so the
     harness observes the estimator in both (otherwise every check below
     would be vacuous);
  2. with a profile and the same switch: effectiveType, rtt and downlink in
     the main frame, both iframes and all three worker kinds, and the ECT, RTT
     and Downlink headers on navigations, the same-origin frame and frame
     subresources, equal the profile's estimate after stock's noise for
     127.0.0.1 (x0.90..1.10, 50 ms and 50 kbps rounding); worker requests
     carry no network hints, as in stock; saveData stays false and type and
     downlinkMax unexposed, as stock on desktop Linux;
  3. no change event fires in a window or a worker while the network service
     keeps reporting the forced estimate;
  4. a profile without network.connection, an effective type the estimator
     would not derive from the RTT, and a downlink off the 0.05 Mbps grid are
     refused at startup, naming the field.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.compare import reported_downlink, reported_rtt
from consistency.server import Servers
from helpers import ALL_CONTEXTS, GuardFailure, dump, expect_everywhere, finish, load_profile, parse_args, run_binary

FORCE = "--force-effective-connection-type=Slow-2G"
HOST = "127.0.0.1"  # every harness origin; the noise is keyed on the host, not the port
# (effective_type, rtt ms, downlink Mbps): consistent with the estimator's
# thresholds (272, 1420, 2010 ms) and away from any host or forced value.
CANDIDATES = [("3g", 450, 0.75), ("2g", 1500, 0.3), ("3g", 600, 1.2)]
BROWSER_SLOTS = ("navigation_with_hints", "frame_same")  # content/browser/client_hints/client_hints.cc
RENDERER_SLOTS = ("fetch_main", "fetch_iframe_same")  # FrameFetchContext, NetworkStateNotifier
WORKER_SLOTS = ("fetch_worker", "fetch_shared_worker", "fetch_service_worker")
MASK = (1 << 64) - 1


# ---- components/phasmid/noise.cc and Profile::ConnectionNoiseMultiplier ----

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


def derive(seed_hex: str, tag: str) -> tuple[int, int]:
    raw = bytes.fromhex(seed_hex)
    k0, k1 = int.from_bytes(raw[:8], "little"), int.from_bytes(raw[8:], "little")
    return siphash24(k0, k1, tag.encode() + b"\0"), siphash24(k0, k1, tag.encode() + b"\1")


def multiplier(seed_hex: str, host: str) -> float:
    k0, k1 = derive(seed_hex, "phasmid/connection/1")
    return 0.9 + float(siphash24(k0, k1, host.encode()) % 21) * 0.01


def self_check() -> None:
    if siphash24(0x0706050403020100, 0x0F0E0D0C0B0A0908, b"") != 0x726FDB47DD0E0E31:
        raise GuardFailure("SipHash-2-4 model disagrees with the reference vector")
    if derive("5b1d7c0e9a3f4d2c8e6b1a0f7c3d9e21", "phasmid/canvas/1") != (0x76F2FB5423FA0BA9, 0x312D93BE81A91A51):
        raise GuardFailure("key derivation model disagrees with noise_unittest.cc DerivedKeysAreFrozen")
    print("PASS: SipHash-2-4 and key derivation reproduce components/phasmid/noise.cc")


# ---- the guard -------------------------------------------------------------

def write_profile(base: dict, work: Path, name: str, connection: dict | None) -> Path:
    p = copy.deepcopy(base)
    if connection is None:
        p["network"].pop("connection", None)
    else:
        p["network"]["connection"] = connection
    path = work / f"{name}.json"
    path.write_text(json.dumps(p))
    return path


def expect_refused(binary: str, profile_path: Path, work: Path, field: str) -> None:
    r = run_binary(binary, [f"--phasmid-profile={profile_path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{profile_path.name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {profile_path.name} refused at startup naming {field}")


def expect_hint(d: dict, slot: str, ect: str, rtt: int, downlink: float) -> None:
    h = d["headers"].get(slot, {})
    got = (h.get("ect"), h.get("rtt"), h.get("downlink"))
    ok = got[0] == ect and got[1] is not None and int(got[1]) == rtt and got[2] is not None and float(got[2]) == downlink
    if not ok:
        raise GuardFailure(f"{slot}: ECT/RTT/Downlink {got}, expected ({ect!r}, {rtt}, {downlink})")
    print(f"PASS: {slot} sends ECT {ect}, RTT {rtt}, Downlink {got[2]}")


EVENTS_SCRIPT = """async (ms) => {
  const watch = (c) => new Promise((res) => {
    const before = [c.effectiveType, c.rtt, c.downlink];
    let events = 0;
    c.addEventListener("change", () => { events++; });
    setTimeout(() => res({ events, before, after: [c.effectiveType, c.rtt, c.downlink] }), ms);
  });
  const src = `const c = navigator.connection; const before = [c.effectiveType, c.rtt, c.downlink]; let events = 0;
    c.addEventListener("change", () => { events++; });
    setTimeout(() => postMessage({ events, before, after: [c.effectiveType, c.rtt, c.downlink] }), ${ms});`;
  const w = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
  const worker = new Promise((res) => { w.onmessage = (e) => res(e.data); });
  return { main: await watch(navigator.connection), worker: await worker };
}"""


def watch_events(binary: str, profile: Path, *, headless: bool) -> dict:
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=[FORCE])
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            return page.evaluate(EVENTS_SCRIPT, 4000)
        finally:
            launch.stop()


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    self_check()

    forced = dump(args.binary, None, headless=headless, extra_args=[FORCE])
    if forced["context_errors"]:
        raise GuardFailure(f"precondition contexts failed: {forced['context_errors']}")
    host = forced["contexts"]["main"]["navigator"].get("connection") or {}
    host_ect_header = forced["headers"].get("navigation_with_hints", {}).get("ect")
    if host.get("effectiveType") != "slow-2g" or host_ect_header != "slow-2g":
        raise GuardFailure(f"vacuous: without a profile {FORCE} did not reach the page (navigator.connection {host}, ECT header {host_ect_header!r})")
    print(f"PASS: precondition: the forced estimate reaches the page without a profile ({host.get('effectiveType')}, rtt {host.get('rtt')}, downlink {host.get('downlink')}; ECT header {host_ect_header})")

    m = multiplier(base["seed"], HOST)
    candidates = []
    for ect, rtt, downlink in CANDIDATES:
        kbps = round(downlink * 1000)
        expected = (ect, reported_rtt(rtt, m), reported_downlink(kbps, m, renderer=True))
        if expected != (host.get("effectiveType"), host.get("rtt"), host.get("downlink")):
            candidates.append(((ect, rtt, downlink, kbps), expected))
    if not candidates:
        raise GuardFailure(f"vacuous: every candidate reports what the host does ({host})")
    (ect, rtt, downlink, kbps), (_, js_rtt, js_downlink) = candidates[0]
    header_downlink = reported_downlink(kbps, m, renderer=False)
    if header_downlink != js_downlink:
        raise GuardFailure(f"model: header downlink {header_downlink} != JS {js_downlink} for {kbps} kbps")
    print(f"profile estimate {ect}, {rtt} ms, {downlink} Mbps; factor {m:.2f} for {HOST}: page values rtt {js_rtt}, downlink {js_downlink}")

    path = write_profile(base, work, "connection", {"effective_type": ect, "rtt": rtt, "downlink": downlink})
    d = dump(args.binary, str(path), headless=headless, extra_args=[FORCE])
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "navigator.connection.effectiveType", ect, ALL_CONTEXTS)
    expect_everywhere(d, "navigator.connection.rtt", js_rtt, ALL_CONTEXTS)
    expect_everywhere(d, "navigator.connection.downlink", js_downlink, ALL_CONTEXTS)
    expect_everywhere(d, "navigator.connection.saveData", False, ALL_CONTEXTS)
    expect_everywhere(d, "navigator.connection.type", None, ALL_CONTEXTS, label="navigator.connection.type (unexposed on desktop)")
    expect_everywhere(d, "navigator.connection.downlinkMax", None, ALL_CONTEXTS, label="navigator.connection.downlinkMax (unexposed on desktop)")
    for slot in BROWSER_SLOTS + RENDERER_SLOTS:
        expect_hint(d, slot, ect, js_rtt, js_downlink)
    for slot in WORKER_SLOTS:
        sent = {k: v for k, v in d["headers"].get(slot, {}).items() if k in ("ect", "rtt", "downlink", "save-data")}
        if sent:
            raise GuardFailure(f"{slot}: worker request carries network hints {sent}; stock sends none")
    print(f"PASS: worker requests carry no network hints ({', '.join(WORKER_SLOTS)})")

    # The worker is a blob: worker, whose URL has no host and so another
    # noise factor; only its stability is asserted here. The dump above
    # checked the values of workers loaded from 127.0.0.1.
    seen = watch_events(args.binary, path, headless=headless)
    want = [ect, js_rtt, js_downlink]
    for ctx, r in seen.items():
        if r["events"] or r["before"] != r["after"] or (ctx == "main" and r["before"] != want):
            raise GuardFailure(f"{ctx}: change events {r['events']}, values {r['before']} -> {r['after']}, main expected {want} throughout")
    print("PASS: no change event and no value change in a window or a worker over 4 s")

    expect_refused(args.binary, write_profile(base, work, "no-connection", None), work, "network.connection")
    expect_refused(args.binary, write_profile(base, work, "ect-mismatch", {"effective_type": "4g", "rtt": rtt, "downlink": downlink}), work, "network.connection.effective_type")
    expect_refused(args.binary, write_profile(base, work, "downlink-off-grid", {"effective_type": ect, "rtt": rtt, "downlink": downlink + 0.01}), work, "network.connection.downlink")


finish(main)
