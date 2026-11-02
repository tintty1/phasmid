"""Guard for patches/phasmid/platform/media-devices.patch.

navigator.mediaDevices.enumerateDevices() lists the profile's devices, never
the host's, with stock Chrome's blanking and ID hashing. The profile's counts
and labels are chosen to differ from the host's. Checks 1 to 6 run on the
profile under test with labels of its OS (CLAIM for linux, WIN_CLAIM for
windows) and that OS's model of the audio list (check 2 for linux, check 7
for windows); with a linux profile, check 7 runs on its Windows variant:

  0. precondition, no profile: the host's own list after permission is not
     the one under test, and Chrome's fake capture devices
     (--use-fake-device-for-media-stream) change it, so check 4 is not
     vacuous;
  1. before permission, in the main frame and same- and cross-origin iframes:
     one blank entry per kind that has a device, in stock order; with only
     camera permission (CDP Browser.grantPermissions) the cameras are
     labelled and the audio kinds stay blank, as in stock;
  2. after camera and microphone permission: the virtual default audio
     devices first (deviceId "default", the output's groupId "default"),
     then media_devices.labels.* in order (linux; windows: check 7's
     list); 64-hex deviceIds, the same in a
     same-origin iframe and different in a cross-origin one (allow="camera;
     microphone"); a cross-origin iframe without that permission policy stays
     blank; an input shares the group of the output with its label and a
     camera that of the microphone named after it (GuessVideoGroupID);
  3. deviceIds are equal across two launches with fresh user data
     directories and the same profile, and differ for another seed;
  4. the host's devices do not reach the page: with the fake capture devices
     the list and the IDs are unchanged, and no devicechange fires;
  5. a claimed camera opens no host device: getUserMedia rejects with
     NotReadableError, as stock Linux Chrome does for a busy camera; a kind
     the profile lacks rejects with NotFoundError even when the host has it;
     an audio getUserMedia opens the default input labelled as listed;
  6. a device without a label, or (linux) the virtual default listed as a
     label, stops the browser naming media_devices.labels.<kind>;
  7. a windows profile lists its audio kinds as AudioManagerWin does: "Default
     - <first device>" (deviceId "default") and "Communications - <first
     device>" (deviceId "communications") before the real devices, both in
     the first device's group; an input shares the group of the output on
     the same adapter (the endpoint name's parentheses), any other input has
     its own; a "Default - ..." or "Communications - ..." label stops the
     browser naming media_devices.labels.<kind>.
"""

from __future__ import annotations

import copy
import json
import re
import socket
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Origin, Recorder
from helpers import GuardFailure, finish, load_profile, parse_args, run_binary, windows_variant

KINDS = ("audioinput", "videoinput", "audiooutput")  # stock enumeration order
HEX64 = re.compile(r"[0-9a-f]{64}")
# The virtual audio entries' deviceIds, the same in every frame (Windows adds
# "communications").
VIRTUAL_IDS = {"default", "communications"}
# Labels of a machine that is not this one: a Logitech C920 webcam with its
# microphone, a laptop's camera, and its analog card for input and output.
CLAIM = {
    "audioinput": ["HD Pro Webcam C920 Analog Stereo", "Built-in Audio Analog Stereo"],
    "audiooutput": ["Built-in Audio Analog Stereo"],
    "videoinput": ["HD Pro Webcam C920", "Integrated Camera"],
}
# Windows endpoint names, "<endpoint> (<adapter>)", for check 7.
WIN_CLAIM = {
    "audioinput": ["Microphone Array (Intel Smart Sound Technology for Digital Microphones)", "Microphone (Realtek(R) Audio)"],
    "audiooutput": ["Speakers (Realtek(R) Audio)"],
    "videoinput": ["Integrated Webcam"],
}


def output_only(claim: dict) -> dict:
    """A second claim without cameras and microphones, for check 5."""
    return {"audioinput": [], "audiooutput": list(claim["audiooutput"]), "videoinput": []}

FRAME_SETUP = """() => {
  window.__devicechanges = 0;
  navigator.mediaDevices.addEventListener("devicechange", () => window.__devicechanges++);
}"""
ENUM = """async () => (await navigator.mediaDevices.enumerateDevices()).map((d) => ({ kind: d.kind, label: d.label, deviceId: d.deviceId, groupId: d.groupId }))"""
GUM = """async (c) => {
  const t0 = performance.now();
  try {
    const s = await navigator.mediaDevices.getUserMedia(c);
    const tracks = s.getTracks().map((t) => ({ kind: t.kind, label: t.label, deviceId: t.getSettings().deviceId }));
    s.getTracks().forEach((t) => t.stop());
    return { tracks, ms: performance.now() - t0 };
  } catch (e) {
    return { error: e.name, message: e.message, ms: performance.now() - t0 };
  }
}"""
ADD_FRAME = """([src, allow]) => new Promise((res) => {
  const f = document.createElement("iframe");
  if (allow) f.allow = allow;
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Origins:
    """Origins A (main) and B (cross-origin frames) on fixed ports, so two
    launches see the same origins and therefore comparable device IDs."""

    def __init__(self):
        self.ports = (free_port(), free_port())

    def __enter__(self):
        recorder = Recorder()
        self.a = Origin(recorder, self.ports[0]).start()
        self.b = Origin(recorder, self.ports[1]).start()
        return self

    def __exit__(self, *_exc):
        self.a.stop()
        self.b.stop()


def with_devices(profile: dict, claim: dict, seed: str | None = None) -> dict:
    p = copy.deepcopy(profile)
    labels = {kind: list(v) for kind, v in claim.items() if v}
    p["media_devices"] = {kind: len(claim[kind]) for kind in KINDS}
    if labels:
        p["media_devices"]["labels"] = labels
    if seed:
        p["seed"] = seed
    return p


def write(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def probe(binary: str, profile: Path | None, origins: Origins, *, headless: bool, extra_args: list[str] | None = None, gum: bool = False) -> dict:
    """enumerateDevices() in every frame before permission, with camera
    permission only, and with camera and microphone permission; then, when
    `gum`, getUserMedia in the main frame; and the devicechange count."""
    with origins, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=extra_args or [])
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{origins.a.url}/h/guard", wait_until="domcontentloaded")
            page.evaluate(ADD_FRAME, [f"{origins.a.url}/h/same", ""])
            page.evaluate(ADD_FRAME, [f"{origins.b.url}/h/cross", "camera; microphone"])
            page.evaluate(ADD_FRAME, [f"{origins.b.url}/h/cross-blocked", ""])
            frames = {"main": page.main_frame}
            for f in page.frames:
                for name, path in (("iframe_same", "/h/same"), ("iframe_cross", "/h/cross"), ("iframe_cross_blocked", "/h/cross-blocked")):
                    if f.url.endswith(path):
                        frames[name] = f
            for f in frames.values():
                f.evaluate(FRAME_SETUP)

            def enumerate_all() -> dict:
                return {name: f.evaluate(ENUM) for name, f in frames.items()}

            out = {"pre": enumerate_all()}
            cdp = browser.new_browser_cdp_session()
            cdp.send("Browser.grantPermissions", {"origin": origins.a.url, "permissions": ["videoCapture"]})
            out["video_only"] = enumerate_all()
            cdp.send("Browser.grantPermissions", {"origin": origins.a.url, "permissions": ["videoCapture", "audioCapture"]})
            out["post"] = enumerate_all()
            if gum:
                out["gum"] = {
                    "video": page.evaluate(GUM, {"video": True}),
                    "audio": page.evaluate(GUM, {"audio": True}),
                }
            page.wait_for_timeout(1000)
            out["devicechanges"] = {name: f.evaluate("() => window.__devicechanges") for name, f in frames.items()}
            return out
        finally:
            launch.stop()


def blank(kinds_present: list[str]) -> list[dict]:
    return [{"kind": k, "label": "", "deviceId": "", "groupId": ""} for k in KINDS if k in kinds_present]


def expect_blank(results: dict, phase: str, claim: dict, contexts=None) -> None:
    present = [k for k in KINDS if claim[k]]
    bad = {ctx: r for ctx, r in results[phase].items() if (contexts is None or ctx in contexts) and r != blank(present)}
    if bad:
        raise GuardFailure(f"{phase}: expected one blank entry per claimed kind {present}, got {bad}")
    print(f"PASS: {phase}: one blank entry per claimed kind {present} in {', '.join(c for c in results[phase] if contexts is None or c in contexts)}")


def check_labelled(devices: list[dict], claim: dict, default_label: str, where: str) -> None:
    """The full list after permission, per the patch's model of the Linux
    audio managers."""
    by_kind = {k: [d for d in devices if d["kind"] == k] for k in KINDS}
    if [d["kind"] for d in devices] != [k for k in KINDS for _ in by_kind[k]]:
        raise GuardFailure(f"{where}: kinds out of stock order: {[d['kind'] for d in devices]}")
    for kind in KINDS:
        got = by_kind[kind]
        audio = kind != "videoinput"
        want = ([default_label] if audio and claim[kind] else []) + claim[kind]
        if [d["label"] for d in got] != want:
            raise GuardFailure(f"{where}: {kind} labels {[d['label'] for d in got]}, expected {want}")
        real = got[1:] if audio and got else got
        if audio and got and got[0]["deviceId"] != "default":
            raise GuardFailure(f"{where}: {kind} default entry has deviceId {got[0]['deviceId']!r}")
        if not all(HEX64.fullmatch(d["deviceId"]) and HEX64.fullmatch(d["groupId"]) for d in real):
            raise GuardFailure(f"{where}: {kind} IDs are not 64 hex digits: {real}")
        if len({d["deviceId"] for d in real}) != len(real):
            raise GuardFailure(f"{where}: {kind} deviceIds repeat: {real}")
    outputs, inputs, cameras = by_kind["audiooutput"], by_kind["audioinput"], by_kind["videoinput"]
    if outputs and outputs[0]["groupId"] != "default":
        raise GuardFailure(f"{where}: the default output's groupId is {outputs[0]['groupId']!r}, stock reports 'default'")
    if inputs and not HEX64.fullmatch(inputs[0]["groupId"]):
        raise GuardFailure(f"{where}: the default input's groupId is {inputs[0]['groupId']!r}")
    for mic in inputs[1:]:
        same_card = [o for o in outputs[1:] if o["label"] == mic["label"]]
        shared = [o for o in outputs if o["groupId"] == mic["groupId"]]
        if (same_card and shared != same_card[:1]) or (not same_card and shared):
            raise GuardFailure(f"{where}: input {mic['label']!r} has groupId {mic['groupId']}, shared with {shared}")
    for cam in cameras:
        named = [m for m in inputs[1:] if cam["label"] in m["label"]]
        if named and cam["groupId"] != named[0]["groupId"]:
            raise GuardFailure(f"{where}: camera {cam['label']!r} does not share the group of {named[0]['label']!r}")
        if not named and any(cam["groupId"] == d["groupId"] for d in devices if d is not cam):
            raise GuardFailure(f"{where}: camera {cam['label']!r} shares a group with no microphone named after it")


def check_windows_labelled(devices: list[dict], default_label: str, where: str) -> None:
    """Check 7, per AudioManagerWin and AudioManagerBase::GetGroupIDInput."""
    by_kind = {k: [d for d in devices if d["kind"] == k] for k in KINDS}
    for kind in ("audioinput", "audiooutput"):
        got = by_kind[kind]
        first = WIN_CLAIM[kind][0]
        want = [f"{default_label} - {first}", f"Communications - {first}", *WIN_CLAIM[kind]]
        if [d["label"] for d in got] != want:
            raise GuardFailure(f"{where}: windows {kind} labels {[d['label'] for d in got]}, expected {want}")
        if [d["deviceId"] for d in got[:2]] != ["default", "communications"]:
            raise GuardFailure(f"{where}: windows {kind} virtual deviceIds {[d['deviceId'] for d in got[:2]]}")
        if not all(HEX64.fullmatch(d["deviceId"]) and HEX64.fullmatch(d["groupId"]) for d in got[2:]):
            raise GuardFailure(f"{where}: windows {kind} IDs are not 64 hex digits: {got[2:]}")
        if not (got[0]["groupId"] == got[1]["groupId"] == got[2]["groupId"]):
            raise GuardFailure(f"{where}: windows {kind} virtual entries are not in the first device's group: {got[:3]}")
    inputs, outputs = by_kind["audioinput"], by_kind["audiooutput"]
    if inputs[3]["groupId"] != outputs[2]["groupId"]:
        raise GuardFailure(f"{where}: {inputs[3]['label']!r} is not in the group of {outputs[2]['label']!r}")
    if inputs[2]["groupId"] in {d["groupId"] for d in outputs}:
        raise GuardFailure(f"{where}: {inputs[2]['label']!r} shares a group with an output on another adapter")


def ids(devices: list[dict]) -> list[str]:
    return [d["deviceId"] for d in devices]


def expect_refused(binary: str, profile: dict, work: Path, name: str, field: str) -> None:
    path = write(profile, work, name)
    r = run_binary(binary, [f"--phasmid-profile={path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {name} refused at startup naming {field}")


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    origins = Origins()
    fake = ["--use-fake-device-for-media-stream"]
    family = base["os"]["family"]
    if family not in ("linux", "windows"):
        raise GuardFailure(f"vacuous: the profile claims os.family {family!r}, neither linux nor windows")
    windows_base = family == "windows"
    claim = WIN_CLAIM if windows_base else CLAIM
    claim_output_only = output_only(claim)

    # 0. Preconditions.
    host = probe(args.binary, None, origins, headless=headless)
    host_fake = probe(args.binary, None, origins, headless=headless, extra_args=fake)
    host_labels = {k: [d["label"] for d in host["post"]["main"] if d["kind"] == k] for k in KINDS}
    if any(host_labels[k][-len(claim[k]):] == claim[k] for k in KINDS if host_labels[k]):
        raise GuardFailure(f"vacuous: the host already lists the claimed labels: {host_labels}")
    if [(d["kind"], d["label"]) for d in host["post"]["main"]] == [(d["kind"], d["label"]) for d in host_fake["post"]["main"]]:
        raise GuardFailure("vacuous: the fake capture devices do not change the host's list")
    print(f"PASS: precondition: the host lists {host_labels}, and the fake capture devices change that list")
    # The virtual default device's label is the browser's localized string;
    # take it from this build's own list when the host has an audio device.
    host_defaults = [d["label"] for d in host["post"]["main"] if d["deviceId"] == "default"]
    default_label = host_defaults[0] if host_defaults else "Default"

    claim_path = write(with_devices(base, claim), work, "claim")
    first = probe(args.binary, claim_path, origins, headless=headless, gum=True)
    outputs_only = probe(args.binary, write(with_devices(base, claim_output_only), work, "output-only"), origins, headless=headless, gum=True)

    # 1. Before permission, and with camera permission only.
    expect_blank(outputs_only, "pre", claim_output_only)
    expect_blank(first, "pre", claim)
    video_only = {ctx: r for ctx, r in first["video_only"].items() if ctx != "iframe_cross_blocked"}
    for ctx, devices in video_only.items():
        cams = [d["label"] for d in devices if d["kind"] == "videoinput"]
        audio = [d for d in devices if d["kind"] != "videoinput"]
        if cams != claim["videoinput"] or audio != blank(["audioinput", "audiooutput"]):
            raise GuardFailure(f"camera permission only, {ctx}: expected labelled cameras {claim['videoinput']} and blank audio kinds, got {devices}")
    print(f"PASS: camera permission only: cameras labelled, audio kinds blank in {', '.join(video_only)}")

    # 2. After permission.
    post = first["post"]
    for ctx in ("main", "iframe_same", "iframe_cross"):
        if windows_base:
            check_windows_labelled(post[ctx], default_label, ctx)
        else:
            check_labelled(post[ctx], claim, default_label, ctx)
    if windows_base:
        print("PASS: after permission (check 7, windows profile): Default and Communications entries on the first device, adapter groups, "
              "in main, iframe_same, iframe_cross")
    else:
        print(f"PASS: after permission: Default entries, then the profile's labels, stock IDs and groups in main, iframe_same, iframe_cross "
              f"(default label {default_label!r})")
    expect_blank(first, "post", claim, contexts=("iframe_cross_blocked",))
    if ids(post["iframe_same"]) != ids(post["main"]):
        raise GuardFailure(f"same-origin iframe deviceIds differ from the main frame: {ids(post['iframe_same'])} vs {ids(post['main'])}")
    shared = (set(ids(post["iframe_cross"])) & set(ids(post["main"]))) - VIRTUAL_IDS
    if shared:
        raise GuardFailure(f"cross-origin iframe shares deviceIds with the main frame: {shared}")
    print("PASS: deviceIds equal in the same-origin iframe, all different in the cross-origin one")

    # 3. Stable per profile and origin, different per seed.
    second = probe(args.binary, claim_path, origins, headless=headless)
    if ids(second["post"]["main"]) != ids(post["main"]) or ids(second["post"]["iframe_cross"]) != ids(post["iframe_cross"]):
        raise GuardFailure(f"deviceIds changed between two launches with the same profile: {ids(post['main'])} vs {ids(second['post']['main'])}")
    print("PASS: deviceIds equal across two launches with fresh user data directories, per origin")
    other_seed = write(with_devices(base, claim, seed=format((int(base["seed"], 16) + 1) % (1 << 128), "032x")), work, "other-seed")
    third = probe(args.binary, other_seed, origins, headless=headless)
    shared = (set(ids(third["post"]["main"])) & set(ids(post["main"]))) - VIRTUAL_IDS
    if shared:
        raise GuardFailure(f"another seed gives the same deviceIds: {shared}")
    print("PASS: another seed gives different deviceIds")

    # 4. The host's devices do not reach the page.
    with_fake = probe(args.binary, claim_path, origins, headless=headless, extra_args=fake)
    if [(d["kind"], d["label"], d["deviceId"]) for d in with_fake["post"]["main"]] != [(d["kind"], d["label"], d["deviceId"]) for d in post["main"]]:
        raise GuardFailure(f"the host's device set changes the list: {with_fake['post']['main']} vs {post['main']}")
    fired = {ctx: n for r in (first, second, with_fake) for ctx, n in r["devicechanges"].items() if n}
    if fired:
        raise GuardFailure(f"devicechange fired: {fired}")
    print("PASS: the list and deviceIds do not change with the host's devices, and no devicechange fired")

    # 5. getUserMedia.
    video = first["gum"]["video"]
    if video.get("error") != "NotReadableError" or video.get("message") != "Could not start video source":
        raise GuardFailure(f"getUserMedia on the claimed camera: expected NotReadableError 'Could not start video source', got {video}")
    print(f"PASS: getUserMedia on the claimed camera rejects with NotReadableError after {video['ms']:.0f} ms")
    audio = first["gum"]["audio"]
    if "tracks" in audio:
        track = audio["tracks"][0]
        listed = next(d["label"] for d in post["main"] if d["kind"] == "audioinput")
        if track["label"] != listed or track["deviceId"] != "default":
            raise GuardFailure(f"getUserMedia audio opened {track}, expected the default input labelled {listed!r}")
        print("PASS: getUserMedia audio opens the default input, labelled as listed")
    elif audio.get("error") != "NotReadableError":
        raise GuardFailure(f"getUserMedia audio: expected the default input or NotReadableError, got {audio}")
    else:
        print("PASS: getUserMedia audio rejects with NotReadableError (the host has no default input)")
    host_has = [k for k in ("videoinput", "audioinput") if host_labels[k]]
    for kind in ("video", "audio"):
        r = outputs_only["gum"][kind]
        if r.get("error") != "NotFoundError":
            raise GuardFailure(f"getUserMedia {kind} without a claimed device: expected NotFoundError, got {r}")
    print(f"PASS: without claimed cameras and microphones getUserMedia rejects with NotFoundError (the host has {host_has or 'neither'})")

    # 6. Refusals.
    unlabelled = with_devices(base, claim)
    del unlabelled["media_devices"]["labels"]["videoinput"]
    expect_refused(args.binary, unlabelled, work, "unlabelled-camera", "media_devices.labels.videoinput")
    if not windows_base:
        listed_default = with_devices(base, {**CLAIM, "audiooutput": ["Default", "Built-in Audio Analog Stereo"]})
        expect_refused(args.binary, listed_default, work, "listed-default", "media_devices.labels.audiooutput")

    # 7. A windows profile: the profile under test (its list was checked in
    # 2), else the linux profile's Windows variant.
    win_base = base if windows_base else windows_variant(base)
    if not windows_base:
        windows = probe(args.binary, write(with_devices(win_base, WIN_CLAIM), work, "windows"), origins, headless=headless)
        expect_blank(windows, "pre", WIN_CLAIM)
        for ctx in ("main", "iframe_same", "iframe_cross"):
            check_windows_labelled(windows["post"][ctx], default_label, ctx)
        print("PASS: windows profile: Default and Communications entries on the first device, adapter groups, in main, iframe_same, iframe_cross")
    for kind, label in (("audioinput", f"Communications - {WIN_CLAIM['audioinput'][0]}"), ("audiooutput", f"Default - {WIN_CLAIM['audiooutput'][0]}")):
        bad = {**WIN_CLAIM, kind: [label, *WIN_CLAIM[kind]]}
        expect_refused(args.binary, with_devices(win_base, bad), work, f"windows-listed-virtual-{kind}", f"media_devices.labels.{kind}")


finish(main)
