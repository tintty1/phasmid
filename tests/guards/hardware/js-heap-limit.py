"""Guard for patches/phasmid/hardware/js-heap-limit.patch.

V8 sizes its heap from the physical memory gin hands it
(ResourceConstraints::ConfigureDefaults), and performance.memory reports
V8's own Heap::MaxReserved(), so the patch changes the real limit and the
reported one together. v8_heap_size_limit() mirrors the formula
core/profile.patch validates hardware.js_heap_size_limit with.

  0. precondition, no profile: the window contexts report the formula's
     limit for the host's RAM, precisely (not bucketized), so the formula
     matches this build's V8;
  1. hardware.physical_memory_mb below 8 GB, where the limit follows RAM,
     with the matching js_heap_size_limit: every window context reports it;
  2. no physical_memory_mb and no js_heap_size_limit: the limit for
     device_memory * 1024 MB;
  3. a js_heap_size_limit V8 would not produce for the claimed RAM is
     refused at startup naming hardware.js_heap_size_limit.
Workers have no performance.memory, in stock Chrome as here.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from helpers import WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, finish, load_profile, parse_args, run_binary

MB = 1 << 20
PAGE = 256 << 10


def v8_heap_size_limit(physical_bytes: int) -> int:
    """Heap::MaxReserved() on 64-bit desktop for `physical_bytes` of RAM
    (V8 at Chromium 155: scavenger, pointer compression, 256 KB pages)."""
    old = min(max(physical_bytes // 2, 256 * MB), 4096 * MB)
    old = -(-old // PAGE) * PAGE
    semi = min(max(physical_bytes // 4 // 32, 2 * MB), 32 * MB)
    semi = -(-semi // PAGE) * PAGE
    semi = 1 << (semi - 1).bit_length()
    return old + 3 * semi


def host_physical_bytes() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise GuardFailure("no MemTotal in /proc/meminfo")


def write(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    host = v8_heap_size_limit(host_physical_bytes())
    plain = dump(args.binary, None, headless=headless)
    expect_everywhere(plain, "storage.jsHeapSizeLimit", host, WINDOW_CONTEXTS, label="precondition: no profile, V8's limit for the host's RAM")

    # 3840 MB rounds to a 4 GB device; its limit is below the 8 GB cap.
    claimed = copy.deepcopy(profile)
    want = v8_heap_size_limit(3840 * MB)
    claimed["hardware"].update(device_memory=4, physical_memory_mb=3840, js_heap_size_limit=want)
    if want == host:
        raise GuardFailure(f"vacuous: the claimed limit {want} equals the host's")
    d = dump(args.binary, str(write(claimed, work, "ram-3840")), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "storage.jsHeapSizeLimit", want, WINDOW_CONTEXTS, label="physical_memory_mb 3840")

    derived = copy.deepcopy(profile)
    derived["hardware"]["device_memory"] = 2
    derived["hardware"].pop("physical_memory_mb", None)
    derived["hardware"].pop("js_heap_size_limit", None)
    d = dump(args.binary, str(write(derived, work, "ram-2g")), headless=headless)
    expect_everywhere(d, "storage.jsHeapSizeLimit", v8_heap_size_limit(2048 * MB), WINDOW_CONTEXTS, label="device_memory 2, limit derived")

    wrong = copy.deepcopy(claimed)
    wrong["hardware"]["js_heap_size_limit"] = host
    path = write(wrong, work, "wrong-limit")
    r = run_binary(args.binary, [f"--phasmid-profile={path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or "hardware.js_heap_size_limit" not in r.stderr:
        raise GuardFailure(f"wrong js_heap_size_limit: expected a refusal naming it, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print("PASS: a js_heap_size_limit V8 would not produce is refused at startup")


finish(main)
