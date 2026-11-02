"""Guard for patches/phasmid/hardware/storage-quota.patch.

The reported quota is the one stock Chrome computes for a disk of
hardware.disk_total_bytes (CalculateReportedQuota in quota_manager_impl.cc):
usage + 10 GiB when the disk has at least 10 GiB, else usage + the disk
rounded up to a whole GiB. Usage stays real, so the guard asserts
quota - usage, which is also immune to the service worker registration racing
the estimate.

  0. precondition, no profile: quota - usage is what the host's volume gives
     (statvfs of the temporary directory holding the user data directory) in
     every context, so a claim below 10 GiB is observable;
  1. a claimed disk whose quota differs from the host's: navigator.storage
     .estimate() in all six contexts, and webkitTemporaryStorage
     .queryUsageAndQuota and a StorageBucket's estimate() in the window
     contexts, all report it;
  2. a second launch with the same profile reports the same values;
  3. a missing or sub-GiB hardware.disk_total_bytes is refused at startup.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import ALL_CONTEXTS, GuardFailure, dump, finish, load_profile, parse_args, run_binary

GIB = 1 << 30

# The three quota paths a window has. Each yields quota - usage.
SCRIPT = """async () => {
  const T = (p, ms) => Promise.race([p, new Promise((_, j) => setTimeout(() => j(new Error("timeout " + ms)), ms))]);
  const headroom = (q, u) => (typeof q === "number" && typeof u === "number") ? q - u : `bad: ${q}, ${u}`;
  const out = {};
  const est = await T(navigator.storage.estimate(), 5000);
  out.estimate = headroom(est.quota, est.usage);
  out.webkitTemporaryStorage = await T(new Promise((res) => navigator.webkitTemporaryStorage.queryUsageAndQuota(
      (u, q) => res(headroom(q, u)), (e) => res(`error: ${e && e.name}`))), 5000);
  try {
    const bucket = await T(navigator.storageBuckets.open("phasmid-guard"), 5000);
    const b = await T(bucket.estimate(), 5000);
    out.storageBucket = headroom(b.quota, b.usage);
  } catch (e) { out.storageBucket = `error: ${e.name}`; }
  return out;
}"""

_ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""


def reported_headroom(disk_total: int) -> int:
    """quota - usage for a disk of `disk_total` bytes, as CalculateReportedQuota computes it."""
    if disk_total >= 10 * GIB:
        return 10 * GIB
    return -(-disk_total // GIB) * GIB


def estimate_headroom(d: dict) -> dict:
    out = {}
    for ctx in ALL_CONTEXTS:
        storage = (d["contexts"].get(ctx) or {}).get("storage") or {}
        quota, usage = storage.get("quota"), storage.get("usage")
        out[ctx] = quota - usage if isinstance(quota, int) and isinstance(usage, int) else f"<no estimate: {storage}>"
    return out


def expect_headroom(d: dict, want: int, label: str) -> dict:
    if d["context_errors"]:
        raise GuardFailure(f"{label}: contexts failed: {d['context_errors']}")
    got = estimate_headroom(d)
    bad = {ctx: v for ctx, v in got.items() if v != want}
    if bad:
        raise GuardFailure(f"{label}: estimate() quota - usage expected {want} in every context, got {bad}")
    print(f"PASS: {label}: estimate() quota - usage == {want} in {len(got)} contexts")
    return got


def window_paths(binary: str, profile: Path, headless: bool) -> dict:
    """SCRIPT in the main frame, a same-origin and a cross-origin iframe."""
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            results = {"main": page.evaluate(SCRIPT)}
            page.evaluate(_ADD_FRAME, f"{servers.a.url}/h/guard-frame")
            page.evaluate(_ADD_FRAME, f"{servers.b.url}/h/guard-frame")
            frames = [f for f in page.frames if f != page.main_frame]
            results["iframe_same"] = next(f for f in frames if f.url.startswith(servers.a.url)).evaluate(SCRIPT)
            results["iframe_cross"] = next(f for f in frames if f.url.startswith(servers.b.url)).evaluate(SCRIPT)
            return results
        finally:
            launch.stop()


def expect_refused(binary: str, profile: dict, work: Path, name: str, field: str) -> None:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    r = run_binary(binary, [f"--phasmid-profile={path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {name} refused at startup naming {field}")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    # Precondition: the host's own quota, from the volume Launch puts the
    # user data directory on.
    st = os.statvfs(tempfile.gettempdir())
    host_headroom = reported_headroom(st.f_blocks * st.f_frsize)
    expect_headroom(dump(args.binary, None, headless=headless), host_headroom, "precondition: no profile, host volume")

    # A disk below 10 GiB, whose quota the host's volume does not give.
    claim = 6_500_000_000 if reported_headroom(6_500_000_000) != host_headroom else 3_500_000_000
    want = reported_headroom(claim)
    claimed = copy.deepcopy(profile)
    claimed["hardware"]["disk_total_bytes"] = claim
    path = work / "disk.json"
    path.write_text(json.dumps(claimed))

    first = expect_headroom(dump(args.binary, str(path), headless=headless), want, f"disk_total_bytes {claim}")
    results = window_paths(args.binary, path, headless)
    bad = {ctx: r for ctx, r in results.items() if any(v != want for v in r.values())}
    if bad:
        raise GuardFailure(f"disk_total_bytes {claim}: quota paths in window contexts expected {want}, got {bad}")
    print(f"PASS: disk_total_bytes {claim}: estimate, webkitTemporaryStorage and StorageBucket == {want} in {', '.join(results)}")

    second = expect_headroom(dump(args.binary, str(path), headless=headless), want, f"disk_total_bytes {claim}, second launch")
    if first != second:
        raise GuardFailure(f"quota differs across launches: {first} then {second}")
    print("PASS: quota identical across two launches")

    missing = copy.deepcopy(profile)
    missing["hardware"].pop("disk_total_bytes", None)
    expect_refused(args.binary, missing, work, "no-disk-total", "hardware.disk_total_bytes")
    tiny = copy.deepcopy(profile)
    tiny["hardware"]["disk_total_bytes"] = GIB - 1
    expect_refused(args.binary, tiny, work, "sub-gib-disk", "hardware.disk_total_bytes")


finish(main)
