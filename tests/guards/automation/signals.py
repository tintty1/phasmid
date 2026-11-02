"""Guard for patches/phasmid/automation/signals.patch.

  1. precondition: without a profile, --enable-automation turns
     navigator.webdriver on in the main frame (otherwise the rest is vacuous);
  2. with the profile and --enable-automation, navigator.webdriver is false in
     the main frame and both iframes, and stays absent in the three worker
     kinds (WorkerNavigator has no webdriver attribute in stock Chrome);
  3. headful only (the infobar does not exist in headless): without a profile
     --enable-automation shrinks innerHeight (the automation infobar; the
     precondition), and with the profile innerHeight is the same with and
     without --enable-automation.
"""

from __future__ import annotations

from consistency.compare import _MISSING, _get
from helpers import ALL_CONTEXTS, WINDOW_CONTEXTS, GuardFailure, dump, expect_everywhere, finish, parse_args

AUTOMATION = ["--enable-automation"]


def checked(d: dict, what: str) -> dict:
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed ({what}): {d['context_errors']}")
    return d


def inner_height(d: dict) -> int:
    return _get(d["contexts"]["main"], "screen.innerHeight")


def main() -> None:
    args = parse_args(__doc__)
    if not args.profile:
        raise GuardFailure("this guard needs --profile")
    headless = not args.headful

    plain = checked(dump(args.binary, None, headless=headless, extra_args=AUTOMATION), "no profile, --enable-automation")
    if _get(plain["contexts"]["main"], "navigator.webdriver") is not True:
        raise GuardFailure("vacuous: without a profile, --enable-automation did not set navigator.webdriver in the main frame")
    print("PASS: precondition, without a profile --enable-automation sets navigator.webdriver")

    auto = checked(dump(args.binary, args.profile, headless=headless, extra_args=AUTOMATION), "profile, --enable-automation")
    expect_everywhere(auto, "navigator.webdriver", False, contexts=WINDOW_CONTEXTS, label="navigator.webdriver with --enable-automation")
    workers = [c for c in ALL_CONTEXTS if c not in WINDOW_CONTEXTS]
    exposed = {c: _get(auto["contexts"].get(c, {}), "navigator.webdriver") for c in workers}
    exposed = {c: v for c, v in exposed.items() if v is not _MISSING and v is not None}
    if exposed:
        raise GuardFailure(f"navigator.webdriver exposed in workers, unlike stock Chrome: {exposed}")
    print(f"PASS: navigator.webdriver absent in {len(workers)} worker contexts")

    if headless:
        print("SKIP: automation infobar check needs --headful (headless has no infobar)")
        return
    plain_off = checked(dump(args.binary, None, headless=False), "no profile, no automation")
    if not inner_height(plain) < inner_height(plain_off):
        raise GuardFailure(f"vacuous: without a profile --enable-automation did not shrink innerHeight ({inner_height(plain_off)} -> {inner_height(plain)}); no infobar to suppress")
    print(f"PASS: precondition, automation infobar shrinks innerHeight without a profile ({inner_height(plain_off)} -> {inner_height(plain)})")
    off = checked(dump(args.binary, args.profile, headless=False), "profile, no automation")
    if inner_height(auto) != inner_height(off):
        raise GuardFailure(f"innerHeight with the profile: {inner_height(off)} without --enable-automation, {inner_height(auto)} with it (automation infobar shown)")
    print(f"PASS: innerHeight with the profile is {inner_height(off)} with and without --enable-automation")


finish(main)
