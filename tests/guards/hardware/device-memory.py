"""Guard for patches/phasmid/hardware/device-memory.patch.

navigator.deviceMemory equals hardware.device_memory in every context, and
the Device-Memory and Sec-CH-Device-Memory request headers carry the same
value. The bucket is chosen to differ from the host's.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import GuardFailure, dump, expect_everywhere, expect_header, finish, load_profile, parse_args


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)

    # Precondition: learn the host bucket from a plain run, then pick another.
    plain = dump(args.binary, None)
    host = plain["contexts"]["main"]["navigator"].get("deviceMemory")
    want = 4 if host != 4 else 16
    profile["hardware"]["device_memory"] = want
    # Without physical_memory_mb both RAM and the heap limit follow
    # device_memory; a kept js_heap_size_limit would be refused at startup.
    profile["hardware"].pop("physical_memory_mb", None)
    profile["hardware"].pop("js_heap_size_limit", None)
    path = Path(args.work_dir) / "profile.json"
    path.write_text(json.dumps(profile))

    d = dump(args.binary, str(path))
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "navigator.deviceMemory", want)
    for slot in ("navigation_with_hints", "fetch_main"):
        expect_header(d, slot, "device-memory", f"{want}")
        expect_header(d, slot, "sec-ch-device-memory", f"{want}")


finish(main)
