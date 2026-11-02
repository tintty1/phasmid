"""Guard for patches/phasmid/platform/bluetooth-share.patch.

A windows profile has navigator.bluetooth and navigator.share as Windows
Chrome does (win11-uhd600 capture: present in the main frame and both
iframes, absent from workers; canShare true, false in the cross-origin
iframe), and neither shows the host:

The windows profile is the profile under test when it is one, else the
linux profile as a Windows 11 desktop (helpers.windows_variant).

  0. precondition: without a profile, and with a linux profile under test
     with that profile, neither API exists in any context, so check 1 is not
     vacuous;
  1. the windows profile without a Bluetooth adapter: navigator.bluetooth,
     navigator.share and canShare exist in the window contexts only,
     canShare() is true in the main frame and the same-origin iframe and
     false in the cross-origin one, getAvailability() is false everywhere,
     whatever adapter the host's BlueZ has;
  2. the same profile claiming an adapter: getAvailability() is true in the
     main frame and the same-origin iframe and false in the cross-origin one
     (its permissions policy blocks Bluetooth, as in the capture);
  3. with a user activation in the main frame: requestDevice() without a
     claimed adapter rejects with NotFoundError "Bluetooth adapter not
     available.", as on Windows without one, and share() rejects with
     AbortError "Share canceled", as a dismissed share sheet does; the
     renderer survives both (no binder would end it).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure, WINDOW_CONTEXTS, dump, expect_everywhere, finish, load_profile, parse_args, windows_variant

WORKERS = ("worker", "shared_worker", "service_worker")
APIS = ("bluetooth", "share", "canShare")

GESTURE_SCRIPT = """() => new Promise((resolve) => {
  const b = document.createElement("button");
  b.id = "go";
  b.textContent = "go";
  b.style.cssText = "position:fixed;left:0;top:0;width:200px;height:60px";
  b.onclick = async () => {
    const out = {};
    const run = async (name, fn) => {
      try { await fn(); out[name] = { resolved: true }; }
      catch (e) { out[name] = { name: e.name, message: e.message }; }
    };
    await run("requestDevice", () => navigator.bluetooth.requestDevice({ acceptAllDevices: true }));
    window.__phasmidGestureDone = out;
  };
  document.body.appendChild(b);
  resolve();
})"""

SHARE_SCRIPT = """() => {
  const b = document.getElementById("go");
  b.onclick = async () => {
    try { await navigator.share({ title: "x", url: "https://example.com/" }); window.__phasmidShare = { resolved: true }; }
    catch (e) { window.__phasmidShare = { name: e.name, message: e.message }; }
  };
}"""


def write(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def gestures(binary: str, profile: Path, *, headless: bool) -> dict:
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            page.evaluate(GESTURE_SCRIPT)
            page.click("#go")
            page.wait_for_function("() => window.__phasmidGestureDone", timeout=10000)
            out = page.evaluate("() => window.__phasmidGestureDone")
            page.evaluate(SHARE_SCRIPT)
            page.click("#go")
            page.wait_for_function("() => window.__phasmidShare", timeout=10000)
            out["share"] = page.evaluate("() => window.__phasmidShare")
            out["alive"] = page.evaluate("() => 1 + 1") == 2
            return out
        finally:
            launch.stop()


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    family = base["os"]["family"]
    if family not in ("linux", "windows"):
        raise GuardFailure(f"vacuous: the profile claims os.family {family!r}, neither linux nor windows")

    runs = [("no profile", dump(args.binary, None, headless=headless))]
    if family == "linux":
        runs.append(("linux profile", dump(args.binary, args.profile, headless=headless)))
    for label, d in runs:
        if d["context_errors"]:
            raise GuardFailure(f"{label}: contexts failed: {d['context_errors']}")
        for api in APIS:
            expect_everywhere(d, f"navigator.has.{api}", False, label=f"precondition, {label}: no navigator.{api}")

    win = copy.deepcopy(base) if family == "windows" else windows_variant(base)
    win["capabilities"].pop("bluetooth_available", None)
    d = dump(args.binary, str(write(win, work, "windows-no-adapter")), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"windows profile: contexts failed: {d['context_errors']}")
    for api in APIS:
        expect_everywhere(d, f"navigator.has.{api}", True, WINDOW_CONTEXTS, label=f"windows profile: navigator.{api} in window contexts")
        expect_everywhere(d, f"navigator.has.{api}", False, WORKERS, label=f"windows profile: no navigator.{api} in workers")
    expect_everywhere(d, "navigator.canShare", True, ("main", "iframe_same"), label="windows profile: canShare() true")
    expect_everywhere(d, "navigator.canShare", False, ("iframe_cross",), label="windows profile: canShare() false cross-origin")
    expect_everywhere(d, "navigator.bluetoothAvailability", False, WINDOW_CONTEXTS, label="windows profile without an adapter: getAvailability()")

    adapter = copy.deepcopy(win)
    adapter["capabilities"]["bluetooth_available"] = True
    d = dump(args.binary, str(write(adapter, work, "windows-adapter")), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"windows profile with an adapter: contexts failed: {d['context_errors']}")
    expect_everywhere(d, "navigator.bluetoothAvailability", True, ("main", "iframe_same"), label="windows profile with an adapter: getAvailability()")
    expect_everywhere(d, "navigator.bluetoothAvailability", False, ("iframe_cross",), label="windows profile with an adapter: getAvailability() cross-origin")

    seen = gestures(args.binary, work / "windows-no-adapter.json", headless=headless)
    if seen.get("requestDevice") != {"name": "NotFoundError", "message": "Bluetooth adapter not available."}:
        raise GuardFailure(f"requestDevice() without a claimed adapter: {seen.get('requestDevice')}, expected NotFoundError 'Bluetooth adapter not available.'")
    print("PASS: requestDevice() without a claimed adapter rejects with NotFoundError 'Bluetooth adapter not available.'")
    if seen.get("share") != {"name": "AbortError", "message": "Share canceled"} or not seen.get("alive"):
        raise GuardFailure(f"share(): {seen.get('share')}, renderer alive {seen.get('alive')}; expected AbortError 'Share canceled'")
    print("PASS: share() rejects with AbortError 'Share canceled' and the renderer survives")


finish(main)
