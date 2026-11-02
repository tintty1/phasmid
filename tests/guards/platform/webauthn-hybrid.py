"""Guard for patches/phasmid/platform/webauthn-hybrid.patch.

getClientCapabilities() hybridTransport is capabilities.bluetooth_available,
whatever adapter the host's BlueZ has or lacks. Each check runs where the
host makes it observable, and at least one always is:

  1. on a host with a present Bluetooth LE adapter and bluetoothd running
     (precondition, no profile: hybridTransport true in the main frame), a
     profile without an adapter (bluetooth_available false or absent):
     hybridTransport and passkeyPlatformAuthenticator false in the main
     frame and both iframes, the other capabilities equal the plain run's;
  2. on a host without one (precondition: hybridTransport false), a profile
     claiming an adapter, as a linux and as a windows profile: it starts with
     the capabilities.bluetooth_available warning and hybridTransport and
     passkeyPlatformAuthenticator are true in the main frame and both
     iframes, as on Windows Chrome with an adapter.
PublicKeyCredential is not exposed to workers.
"""

from __future__ import annotations

import json
from pathlib import Path

from consistency.compare import _get
from helpers import (WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, expect_warned, finish, load_profile, parse_args, start_and_exit,
                     windows_variant)

CAPS = "webauthn.getClientCapabilities"


def check_no_adapter(args, profile: dict, plain: dict) -> None:
    p = json.loads(json.dumps(profile))
    p["capabilities"].pop("bluetooth_available", None)
    path = Path(args.work_dir) / "no-adapter.json"
    path.write_text(json.dumps(p))
    d = dump(args.binary, str(path), headless=not args.headful)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, f"{CAPS}.hybridTransport", False, WINDOW_CONTEXTS, label="capabilities.bluetooth_available absent (hybridTransport)")
    uvpaa = p["webauthn"]["user_verifying_platform_authenticator"]
    expect_everywhere(d, f"{CAPS}.passkeyPlatformAuthenticator", uvpaa, WINDOW_CONTEXTS)
    others = {k: v for k, v in _get(plain["contexts"]["main"], CAPS).items() if k not in ("hybridTransport", "passkeyPlatformAuthenticator")}
    for name, want in others.items():
        expect_everywhere(d, f"{CAPS}.{name}", want, WINDOW_CONTEXTS)


def check_claimed_adapter(args, profile: dict, name: str) -> None:
    p = json.loads(json.dumps(profile))
    p["capabilities"]["bluetooth_available"] = True
    work = Path(args.work_dir)
    r = start_and_exit(args.binary, p, work, name)
    expect_warned(r, name, "capabilities.bluetooth_available", "hybridTransport")
    d = dump(args.binary, str(work / f"{name}.json"), headless=not args.headful)
    if d["context_errors"]:
        raise GuardFailure(f"{name}: contexts failed: {d['context_errors']}")
    expect_everywhere(d, f"{CAPS}.hybridTransport", True, WINDOW_CONTEXTS, label=f"{name}: capabilities.bluetooth_available true (hybridTransport)")
    expect_everywhere(d, f"{CAPS}.passkeyPlatformAuthenticator", True, WINDOW_CONTEXTS, label=f"{name}: passkeyPlatformAuthenticator")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    plain = dump(args.binary, None, headless=not args.headful)
    host = _get(plain["contexts"]["main"], f"{CAPS}.hybridTransport")
    if host is True:
        print("PASS: precondition: the host reports hybridTransport true without a profile")
        check_no_adapter(args, profile, plain)
        print("NOT OBSERVABLE: check 2 needs a host without a Bluetooth adapter")
    elif host is False:
        print("PASS: precondition: the host reports hybridTransport false without a profile")
        print("NOT OBSERVABLE: check 1 needs a host with a present Bluetooth LE adapter")
        check_claimed_adapter(args, profile, f"{profile['os']['family']}-adapter")
        if profile["os"]["family"] != "windows":
            check_claimed_adapter(args, windows_variant(profile), "windows-adapter")
    else:
        raise GuardFailure(f"vacuous: the plain run reports no hybridTransport ({host!r})")


finish(main)
