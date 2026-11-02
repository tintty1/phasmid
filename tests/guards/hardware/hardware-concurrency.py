"""Guard for patches/phasmid/hardware/hardware-concurrency.patch.

navigator.hardwareConcurrency equals hardware.concurrency in the main frame,
both iframes, and all three worker kinds. The value is chosen to differ from
the host so the check cannot pass by accident.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from helpers import GuardFailure, dump, expect_everywhere, finish, load_profile, parse_args


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    host = os.cpu_count() or 0
    want = 6 if host != 6 else 10
    profile["hardware"]["concurrency"] = want
    path = Path(args.work_dir) / "profile.json"
    path.write_text(json.dumps(profile))
    d = dump(args.binary, str(path))
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "navigator.hardwareConcurrency", want)


finish(main)
