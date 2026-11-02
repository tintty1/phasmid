"""Guard for patches/phasmid/platform/battery.patch.

navigator.getBattery() resolves with capabilities.battery in the main frame
and both iframes, and with nothing else, ever:

  1. precondition, no profile: getBattery() resolves in every window context
     and is absent from workers, as in stock; the guard's battery states all
     differ from what this host reports;
  2. a discharging laptop (charging false, level 0.42) and one charging below
     full (charging true, level 0.8): charging and level are the profile's in
     every window context, and the charging and discharging times are what
     stock Chrome on Linux and Windows reports when the OS has no estimate
     (Infinity both); a full battery (charging true, level 1) charges in 0 s;
     the same as a windows profile;
  3. no chargingchange, levelchange, chargingtimechange or
     dischargingtimechange event fires over 4 s;
  4. a profile without capabilities.battery and a level off the 0.01 grid
     are refused at startup, naming the field.
"""

from __future__ import annotations

import copy
import json
import math
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure, WINDOW_CONTEXTS, dump, expect_everywhere, finish, load_profile, parse_args, run_binary, windows_variant

WORKER_CONTEXTS = ("worker", "shared_worker", "service_worker")
# (charging, level) -> (chargingTime, dischargingTime), per
# BatteryStatusManagerLinux::ComputeWebBatteryStatus without a UPower estimate
# and battery_status_manager_win.cc without BatteryLifeTime.
STATES = [
    ((False, 0.42), (math.inf, math.inf)),
    ((True, 0.8), (math.inf, math.inf)),
    ((True, 1), (0, math.inf)),
]


def write_profile(base: dict, work: Path, name: str, battery: dict | None) -> Path:
    p = copy.deepcopy(base)
    if battery is None:
        p["capabilities"].pop("battery", None)
    else:
        p["capabilities"]["battery"] = battery
    path = work / f"{name}.json"
    path.write_text(json.dumps(p))
    return path


def expect_refused(binary: str, profile_path: Path, work: Path, field: str) -> None:
    r = run_binary(binary, [f"--phasmid-profile={profile_path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{profile_path.name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {profile_path.name} refused at startup naming {field}")


EVENTS_SCRIPT = """async (ms) => {
  const b = await navigator.getBattery();
  const snap = () => [b.charging, b.level, b.chargingTime, b.dischargingTime];
  const before = snap();
  let events = 0;
  for (const t of ["chargingchange", "levelchange", "chargingtimechange", "dischargingtimechange"]) {
    b.addEventListener(t, () => { events++; });
  }
  await new Promise((res) => setTimeout(res, ms));
  const again = await navigator.getBattery();
  return { events, before, after: snap(), same: again === b };
}"""


def watch_events(binary: str, profile: Path, *, headless: bool) -> dict:
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
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

    plain = dump(args.binary, None, headless=headless)
    if plain["context_errors"]:
        raise GuardFailure(f"precondition contexts failed: {plain['context_errors']}")
    expect_everywhere(plain, "navigator.has.getBattery", True, WINDOW_CONTEXTS, label="precondition: getBattery present in window contexts")
    expect_everywhere(plain, "navigator.has.getBattery", False, WORKER_CONTEXTS, label="precondition: getBattery absent from workers")
    host = plain["contexts"]["main"]["navigator"].get("battery") or {}
    host_state = (host.get("charging"), host.get("level"), host.get("chargingTime"), host.get("dischargingTime"))
    print(f"host reports charging {host_state[0]}, level {host_state[1]}, chargingTime {host_state[2]}, dischargingTime {host_state[3]}")

    tested = 0
    for (charging, level), (charging_time, discharging_time) in STATES:
        if (charging, level, charging_time, discharging_time) == host_state:
            print(f"skip state charging {charging}, level {level}: the host reports it")
            continue
        path = write_profile(base, work, f"battery-{int(charging)}-{int(level * 100)}", {"charging": charging, "level": level})
        d = dump(args.binary, str(path), headless=headless)
        if d["context_errors"]:
            raise GuardFailure(f"contexts failed: {d['context_errors']}")
        want = {"charging": charging, "chargingTime": charging_time, "dischargingTime": discharging_time, "level": level}
        expect_everywhere(d, "navigator.battery", want, WINDOW_CONTEXTS, label=f"battery charging {charging}, level {level}")
        expect_everywhere(d, "navigator.has.getBattery", False, WORKER_CONTEXTS, label="getBattery absent from workers")
        tested += 1
    if tested < 2:
        raise GuardFailure(f"vacuous: only {tested} battery states differ from the host's {host_state}")
    if base["os"]["family"] != "windows":
        path = write_profile(windows_variant(base), work, "battery-windows", {"charging": False, "level": 0.42})
        d = dump(args.binary, str(path), headless=headless)
        if d["context_errors"]:
            raise GuardFailure(f"contexts failed: {d['context_errors']}")
        want = {"charging": False, "chargingTime": math.inf, "dischargingTime": math.inf, "level": 0.42}
        expect_everywhere(d, "navigator.battery", want, WINDOW_CONTEXTS, label="windows profile: battery charging False, level 0.42")

    laptop = write_profile(base, work, "battery-events", {"charging": False, "level": 0.42})
    seen = watch_events(args.binary, laptop, headless=headless)
    if seen["events"] or seen["before"] != [False, 0.42, math.inf, math.inf] or seen["after"] != seen["before"] or not seen["same"]:
        raise GuardFailure(f"battery changed over 4 s: {seen}")
    print("PASS: no battery event and no value change over 4 s; getBattery() returns the same manager")

    expect_refused(args.binary, write_profile(base, work, "no-battery", None), work, "capabilities.battery")
    expect_refused(args.binary, write_profile(base, work, "level-off-grid", {"charging": False, "level": 0.425}), work, "capabilities.battery.level")


finish(main)
