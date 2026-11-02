"""Guard for patches/phasmid/platform/widevine.patch.

Widevine is the installed Google CDM the profile claims, or it is absent.
Needs an installed Google Chrome's CDM (default /opt/google/chrome/WidevineCdm);
a host without one skips. Checks 1 to 3 are for a linux profile; with a
windows profile check 6 takes their place, since a windows profile reports
Widevine unsupported whichever CDM directory is given.

  0. precondition, no profile: a component hint file in the user data
     directory pointing at the CDM turns Widevine on in this build (so check
     4 is not vacuous), and without the hint Widevine is absent (the build
     ships no CDM and the component updater is blocked);
  1. media.widevine.supported true, no hint file, default CDM directory: in
     the main frame and a same-origin iframe, SW_SECURE_DECODE and
     SW_SECURE_CRYPTO are granted, HW_SECURE_ALL is not, a Widevine PSSH
     yields a real license request (more than 1000 bytes, naming Linux), and
     ClearKey initData on the Widevine key system is rejected, as in stock;
  2. the same through --phasmid-widevine-cdm naming a symlink to the CDM;
  3. --phasmid-widevine-cdm naming an empty directory: the browser starts,
     logs "host cannot back media.widevine.supported" once, and Widevine is
     absent;
  4. media.widevine.supported false with the hint file present: Widevine is
     absent and ClearKey still works;
  5. robustness listed with supported false, and HW_SECURE_ALL on a linux
     or a windows profile (stock Chrome grants no HW_SECURE_* level for
     com.widevine.alpha on either), are refused at startup naming
     media.widevine.robustness;
  6. a windows profile (the profile under test when it is one, else its
     Windows variant) claiming Widevine: the browser starts, warns
     media.widevine.supported once (the Linux CDM names Linux in every
     license request), and Widevine is absent with the hint file present
     and, for a windows profile under test, with the default CDM directory
     and through --phasmid-widevine-cdm.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure, GuardSkip, expect_warned, finish, load_profile, parse_args, run_binary, start_and_exit, windows_variant

DEFAULT_CDM = Path("/opt/google/chrome/WidevineCdm")
HINT = "latest-component-updated-widevine-cdm"

SCRIPT = """async () => {
  const T = (p, ms) => Promise.race([p, new Promise((_, j) => setTimeout(() => j(new Error("timeout " + ms)), ms))]);
  const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  // A Widevine PSSH box and a W3C keyids blob.
  const pssh = b64("AAAAW3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADsIARIQ62dqu8s0Xpa7z2FmMPGj2hoNd2lkZXZpbmVfdGVzdCIQZmtqM2xqYVNkZmFsa3IzaioCSEQyAA==");
  const keyids = new TextEncoder().encode(JSON.stringify({ kids: ["ASNFZ4mrze8BI0VniavN7w"] }));
  const cfg = (robustness, initDataTypes = ["cenc"]) => [{ initDataTypes, videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"', robustness }] }];
  const grant = async (ks, c) => { try { await navigator.requestMediaKeySystemAccess(ks, c); return true; } catch (e) { return e.name; } };
  const request = async (mk, type, data) => {
    const s = mk.createSession("temporary");
    const msg = new Promise((res) => s.addEventListener("message", (e) => res(new Uint8Array(e.message)), { once: true }));
    try {
      await T(s.generateRequest(type, data), 8000);
      const bytes = await T(msg, 8000);
      return { bytes: bytes.byteLength, text: String.fromCharCode(...bytes.filter((b) => b >= 32 && b < 127)) };
    } catch (e) { return { error: e.name }; }
  };
  const out = {
    swDecode: await grant("com.widevine.alpha", cfg("SW_SECURE_DECODE")),
    swCrypto: await grant("com.widevine.alpha", cfg("SW_SECURE_CRYPTO")),
    hwAll: await grant("com.widevine.alpha", cfg("HW_SECURE_ALL")),
    clearKey: await grant("org.w3.clearkey", cfg("", ["keyids"])),
  };
  if (out.swDecode === true) {
    const access = await navigator.requestMediaKeySystemAccess("com.widevine.alpha", cfg("SW_SECURE_DECODE", ["cenc", "keyids"]));
    const mk = await access.createMediaKeys();
    out.license = await request(mk, "cenc", pssh);
    out.keyidsOnWidevine = access.getConfiguration().initDataTypes.includes("keyids") ? await request(mk, "keyids", keyids) : { error: "not granted" };
  }
  return out;
}"""

_ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""


def write_profile(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def with_widevine(profile: dict, supported: bool, robustness: list[str]) -> dict:
    p = copy.deepcopy(profile)
    p["media"]["widevine"] = {"supported": supported, "robustness": robustness}
    return p


def user_data_dir(work: Path, hint_to: Path | None) -> Path:
    udd = Path(tempfile.mkdtemp(dir=work, prefix="udd-"))
    if hint_to is not None:
        (udd / "WidevineCdm").mkdir()
        (udd / "WidevineCdm" / HINT).write_text(json.dumps({"Path": str(hint_to)}))
    return udd


def probe(binary: str, profile: Path | None, udd: Path, *, headless: bool, extra_args: list[str] | None = None) -> dict:
    """The script's result in the main frame and a same-origin iframe. The
    last --user-data-dir wins, so `udd` replaces the launcher's own."""
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=[f"--user-data-dir={udd}", *(extra_args or [])])
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            results = {"main": page.evaluate(SCRIPT)}
            page.evaluate(_ADD_FRAME, f"{servers.a.url}/h/guard-frame")
            frame = next(f for f in page.frames if f != page.main_frame)
            results["iframe_same"] = frame.evaluate(SCRIPT)
            return results
        finally:
            launch.stop()


def expect_present(results: dict, label: str) -> None:
    bad = {}
    for ctx, r in results.items():
        lic = r.get("license") or {}
        if r["swDecode"] is not True or r["swCrypto"] is not True:
            bad[ctx] = f"software robustness not granted: {r}"
        elif r["hwAll"] is True:
            bad[ctx] = "HW_SECURE_ALL granted on Linux"
        elif lic.get("bytes", 0) <= 1000 or "Linux" not in lic.get("text", ""):
            bad[ctx] = f"no real license request: {lic}"
        elif "error" not in r.get("keyidsOnWidevine", {}):
            bad[ctx] = f"ClearKey initData accepted on Widevine: {r['keyidsOnWidevine']}"
    if bad:
        raise GuardFailure(f"{label}: {bad}")
    print(f"PASS: {label}: real Widevine license request ({results['main']['license']['bytes']} bytes) in {', '.join(results)}")


def expect_absent(results: dict, label: str) -> None:
    bad = {ctx: r for ctx, r in results.items() if r["swDecode"] is True or r["swCrypto"] is True}
    if bad:
        raise GuardFailure(f"{label}: Widevine granted: {bad}")
    print(f"PASS: {label}: Widevine absent in {', '.join(results)}")


def expect_refused(binary: str, profile_path: Path, work: Path, field: str, extra_args: list[str] | None = None) -> None:
    r = run_binary(binary, [f"--phasmid-profile={profile_path}", *(extra_args or []), "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{profile_path.name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {profile_path.name} refused at startup naming {field}")


def check_linux_claims(binary: str, profile: dict, work: Path, headless: bool) -> None:
    """Checks 1 to 3, on a linux profile."""
    claims = write_profile(with_widevine(profile, True, ["SW_SECURE_DECODE"]), work, "widevine-on")
    expect_present(probe(binary, claims, user_data_dir(work, None), headless=headless), "supported true, default CDM directory")

    link = work / "cdm-link"
    link.symlink_to(DEFAULT_CDM, target_is_directory=True)
    expect_present(probe(binary, claims, user_data_dir(work, None), headless=headless, extra_args=[f"--phasmid-widevine-cdm={link}"]), "supported true, --phasmid-widevine-cdm")

    empty = work / "no-cdm"
    empty.mkdir()
    no_cdm = [f"--phasmid-widevine-cdm={empty}"]
    expect_warned(start_and_exit(binary, with_widevine(profile, True, ["SW_SECURE_DECODE"]), work, "widevine-no-cdm", no_cdm),
                  "supported true, empty --phasmid-widevine-cdm", "media.widevine.supported", "holds no Widevine CDM")
    expect_absent(probe(binary, claims, user_data_dir(work, None), headless=headless, extra_args=no_cdm), "supported true, empty --phasmid-widevine-cdm")


def check_windows_claim(binary: str, profile: dict, work: Path, headless: bool, every_directory: bool) -> None:
    """Check 6."""
    windows = with_widevine(profile if profile["os"]["family"] == "windows" else windows_variant(profile), True, ["SW_SECURE_DECODE"])
    expect_warned(start_and_exit(binary, windows, work, "widevine-windows"), "windows profile claiming Widevine", "media.widevine.supported",
                  "os.family windows")
    path = write_profile(windows, work, "widevine-windows")
    expect_absent(probe(binary, path, user_data_dir(work, DEFAULT_CDM), headless=headless), "windows profile claiming Widevine, hint file present")
    if every_directory:
        expect_absent(probe(binary, path, user_data_dir(work, None), headless=headless), "windows profile claiming Widevine, default CDM directory")
        link = work / "cdm-link-windows"
        link.symlink_to(DEFAULT_CDM, target_is_directory=True)
        expect_absent(probe(binary, path, user_data_dir(work, None), headless=headless, extra_args=[f"--phasmid-widevine-cdm={link}"]),
                      "windows profile claiming Widevine, --phasmid-widevine-cdm")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    if not (DEFAULT_CDM / "manifest.json").is_file():
        raise GuardSkip(f"no installed Google Chrome Widevine CDM at {DEFAULT_CDM}")
    family = profile["os"]["family"]
    if family not in ("linux", "windows"):
        raise GuardFailure(f"vacuous: the profile claims os.family {family!r}, neither linux nor windows")

    expect_present(probe(args.binary, None, user_data_dir(work, DEFAULT_CDM), headless=headless), "precondition: no profile, hint file present")
    expect_absent(probe(args.binary, None, user_data_dir(work, None), headless=headless), "precondition: no profile, no hint file")

    if family == "linux":
        check_linux_claims(args.binary, profile, work, headless)
    else:
        print("NOTE: checks 1 to 3 (Widevine granted) are for a linux profile; check 6 covers this windows one")

    absent = write_profile(with_widevine(profile, False, []), work, "widevine-off")
    results = probe(args.binary, absent, user_data_dir(work, DEFAULT_CDM), headless=headless)
    expect_absent(results, "supported false, hint file present")
    if any(r["clearKey"] is not True for r in results.values()):
        raise GuardFailure(f"ClearKey unavailable with Widevine off: {results}")
    print("PASS: ClearKey still available with Widevine off")

    expect_refused(args.binary, write_profile(with_widevine(profile, False, ["SW_SECURE_DECODE"]), work, "robustness-without-widevine"), work, "media.widevine.robustness")
    expect_refused(args.binary, write_profile(with_widevine(profile, True, ["HW_SECURE_ALL"]), work, "hardware-robustness"), work, "media.widevine.robustness")
    if family == "linux":
        expect_refused(args.binary, write_profile(with_widevine(windows_variant(profile), True, ["HW_SECURE_ALL"]), work, "windows-hardware-robustness"),
                       work, "media.widevine.robustness")

    check_windows_claim(args.binary, profile, work, headless, every_directory=family == "windows")


finish(main)
