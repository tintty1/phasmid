"""Guard for the timezone export in patches/phasmid/core/profile.patch.

The browser exports TZ=<locale.timezone> to every process before ICU
initializes, so Date and Intl report the profile zone in every context,
including the DST offsets, with no window in which the host zone is visible.
The zone is chosen to differ from the host's.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import GuardFailure, dump, expect_everywhere, finish, load_profile, parse_args

CANDIDATES = [("America/New_York", 300, 240), ("Europe/Berlin", -60, -120), ("Asia/Tokyo", -540, -540)]


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    plain = dump(args.binary, None)
    host_zone = plain["contexts"]["main"]["intl"]["dateTimeFormat"]["timeZone"]
    zone, jan, jul = next(c for c in CANDIDATES if c[0] != host_zone)
    profile["locale"]["timezone"] = zone
    path = Path(args.work_dir) / "profile.json"
    path.write_text(json.dumps(profile))

    d = dump(args.binary, str(path))
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "intl.dateTimeFormat.timeZone", zone)
    expect_everywhere(d, "intl.timezoneOffsetJan", jan, label=f"January offset for {zone}")
    expect_everywhere(d, "intl.timezoneOffsetJul", jul, label=f"July offset for {zone}")


finish(main)
