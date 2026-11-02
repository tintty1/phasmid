"""Guard for patches/phasmid/audio/webaudio.patch.

Realtime values. The profile claims a sample rate the host uses with
neither output (44100, 48000 or 96000), the platform's hardware buffer (512
frames, the PulseAudio default, for a Linux profile; the 10 ms WASAPI period,
rate / 100 frames, for a Windows one), the host's channel count (no
capture of another device exists) and an output latency of 24 ms. Each run uses
--autoplay-policy=no-user-gesture-required so every frame's AudioContext
runs, once with --disable-audio-output (the fake stream every host has) and
once with the host's real output when the host has a working one.

  0. precondition: without a profile the context runs on the fake stream
     and its clock advances, and the claimed rate differs from the host's
     (else "vacuous");
  1. in the main frame and in same- and cross-origin iframes: sampleRate,
     baseLatency and destination.maxChannelCount equal the profile at
     construction, outputLatency is 0 at construction and the profile's
     24 ms (8 ms quantum) once running, currentTime advances with wall time
     (within 10%), getOutputTimestamp().contextTime <= currentTime, an
     AudioWorklet sees the profile's sampleRate, and an explicit
     {sampleRate: 22050} is honoured;
  2. full dumps: every worker kind still reports Web Audio unavailable, and
     with audio.noise false every offline key equals the run without a
     profile in all window contexts.

audio.noise, full dumps with seeds S1 and S2 and S1 again after a restart:

  3. the offline render changes, deterministically, while every invariant of
     the audioInvariants probe section holds as without noise: the leading
     zeros (compressor pre-delay) and the metered reduction are unchanged,
     the analyser tail equals the buffer, a page buffer plays back exactly,
     silence and constants stay exact; the CreepJS sum moves by less than
     1e-3; S2 renders differently from S1, and S1 renders identically after
     a restart.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.diff import flatten
from consistency.server import Servers
from helpers import WINDOW_CONTEXTS, GuardFailure, dump, finish, load_profile, parse_args

SEED_1 = "5b1d7c0e9a3f4d2c8e6b1a0f7c3d9e21"
SEED_2 = "0123456789abcdef0123456789abcdef"  # another compressor offset pair than SEED_1
FRAMES = 512  # a Linux profile's PulseAudio buffer
OUTPUT_LATENCY = 0.024
AUTOPLAY = "--autoplay-policy=no-user-gesture-required"
WORKERS = ("worker", "shared_worker", "service_worker")

REALTIME = """async () => {
  const ac = new AudioContext();
  const out = {
    sampleRate: ac.sampleRate,
    baseLatency: ac.baseLatency,
    maxChannelCount: ac.destination.maxChannelCount,
    outputLatencyAtConstruction: ac.outputLatency,
  };
  if (ac.state !== "running") await Promise.race([ac.resume(), new Promise((r) => setTimeout(r, 3000))]);
  out.state = ac.state;
  // The clock starts with the first device callback, and a newly opened
  // device may take a moment to settle; time it after that.
  const startBy = performance.now() + 3000;
  while (ac.currentTime === 0 && performance.now() < startBy) await new Promise((r) => setTimeout(r, 20));
  await new Promise((r) => setTimeout(r, 500));
  const t0 = performance.now();
  const c0 = ac.currentTime;
  await new Promise((r) => setTimeout(r, 1000));
  out.wallSeconds = (performance.now() - t0) / 1000;
  out.contextSeconds = ac.currentTime - c0;
  out.outputLatency = ac.outputLatency;
  out.timestampBehindClock = ac.getOutputTimestamp().contextTime <= ac.currentTime;
  const code = 'registerProcessor("p", class extends AudioWorkletProcessor {' +
    ' constructor() { super(); this.port.postMessage(sampleRate); } process() { return true; } });';
  await ac.audioWorklet.addModule(URL.createObjectURL(new Blob([code], { type: "text/javascript" })));
  const node = new AudioWorkletNode(ac, "p");
  out.workletSampleRate = await new Promise((res) => {
    node.port.onmessage = (e) => res(e.data);
    setTimeout(() => res(null), 3000);
  });
  await ac.close();
  const explicit = new AudioContext({ sampleRate: 22050 });
  out.explicitSampleRate = explicit.sampleRate;
  await explicit.close();
  return out;
}"""

RUN = """async (src) => {
  try { return await (0, eval)("(" + src + ")")(); } catch (e) { return { error: String(e && e.stack || e) }; }
}"""

ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""


def write_profile(profile: dict, work: Path, name: str, audio: dict, seed: str = SEED_1) -> Path:
    p = json.loads(json.dumps(profile))
    p["audio"].update(audio)
    p["seed"] = seed
    path = work / f"{name}.json"
    path.write_text(json.dumps(p))
    return path


def realtime(binary: str, profile: Path | None, headless: bool, fake_output: bool) -> dict:
    extra = [AUTOPLAY] + (["--disable-audio-output"] if fake_output else [])
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=extra)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            page.evaluate(ADD_FRAME, f"{servers.a.url}/h/guard-frame")
            page.evaluate(ADD_FRAME, f"{servers.b.url}/h/guard-frame")
            same = next(f for f in page.frames if f != page.main_frame and f.url.startswith(servers.a.url))
            cross = next(f for f in page.frames if f.url.startswith(servers.b.url))
            return {"main": page.evaluate(RUN, REALTIME), "iframe_same": same.evaluate(RUN, REALTIME), "iframe_cross": cross.evaluate(RUN, REALTIME)}
        finally:
            launch.stop()


def running(result: dict) -> bool:
    return result.get("state") == "running" and result.get("contextSeconds", 0) > 0.5


def expect_realtime(results: dict, want: dict, label: str) -> None:
    for ctx, r in results.items():
        if "error" in r:
            raise GuardFailure(f"{label}, {ctx}: {r['error']}")
        for key in ("sampleRate", "baseLatency", "maxChannelCount", "workletSampleRate"):
            expected = want["sampleRate"] if key == "workletSampleRate" else want[key]
            if r.get(key) != expected:
                raise GuardFailure(f"{label}, {ctx}: {key} is {r.get(key)!r}, the profile says {expected!r}")
        if r.get("outputLatencyAtConstruction") != 0:
            raise GuardFailure(f"{label}, {ctx}: outputLatency at construction is {r.get('outputLatencyAtConstruction')!r}, stock reports 0")
        if r.get("explicitSampleRate") != 22050:
            raise GuardFailure(f"{label}, {ctx}: an explicit sampleRate 22050 became {r.get('explicitSampleRate')!r}")
        if not running(r):
            raise GuardFailure(f"{label}, {ctx}: the context does not run: state {r.get('state')!r}, clock advanced {r.get('contextSeconds')!r} s")
        ratio = r["contextSeconds"] / r["wallSeconds"]
        if not 0.9 <= ratio <= 1.1:
            raise GuardFailure(f"{label}, {ctx}: currentTime advanced {r['contextSeconds']:.3f} s in {r['wallSeconds']:.3f} s of wall time")
        if r.get("outputLatency") != want["outputLatency"]:
            raise GuardFailure(f"{label}, {ctx}: running outputLatency is {r.get('outputLatency')!r}, expected {want['outputLatency']!r}")
        if not r.get("timestampBehindClock"):
            raise GuardFailure(f"{label}, {ctx}: getOutputTimestamp().contextTime is ahead of currentTime")
    print(f"PASS: {label}: rate, latencies, channels, worklet rate, explicit rate and a running clock in {', '.join(results)}")


def checked_dump(binary: str, profile: Path | None, headless: bool, what: str) -> dict:
    d = dump(binary, str(profile) if profile else None, headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed ({what}): {d['context_errors']}")
    return d


def offline_keys(d: dict, ctx: str) -> dict:
    c = d["contexts"][ctx]
    audio = {k: v for k, v in c["audio"].items() if k.startswith(("offline", "analyser", "deterministic", "copyFromChannel"))}
    return flatten({"audio": audio, "audioInvariants": c["audioInvariants"]})


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    # 0. Precondition: the host's own values, on the fake output every host
    # has and on its real output when that works.
    host = realtime(args.binary, None, headless, fake_output=True)
    if any("error" in r or not running(r) for r in host.values()):
        raise GuardFailure(f"without a profile the AudioContext does not run: {host}")
    host_output = realtime(args.binary, None, headless, fake_output=False)
    output_works = all("error" not in r and running(r) for r in host_output.values())
    host_rates = {host["main"]["sampleRate"]} | ({host_output["main"]["sampleRate"]} if output_works else set())
    rate = next((r for r in (44100, 48000, 96000) if r not in host_rates), None)
    if rate is None:
        raise GuardFailure(f"vacuous: every claimable sample rate is one the host uses: {sorted(host_rates)}")
    channels = host["main"]["maxChannelCount"]
    frames = rate // 100 if profile["os"]["family"] == "windows" else FRAMES
    claim = {"sample_rate": rate, "base_latency": frames / rate, "max_channel_count": channels, "output_latency": OUTPUT_LATENCY, "noise": False}
    print(f"PASS: precondition: the host runs at {sorted(host_rates)} Hz; the profile claims {rate} Hz")
    want = {
        "sampleRate": rate,
        "baseLatency": frames / rate,
        "maxChannelCount": channels,
        "outputLatency": round(OUTPUT_LATENCY / 0.008) * 0.008,
    }
    claimed = write_profile(profile, work, "audio", claim)

    # 1. Realtime values, fake output, then the host's output when it works.
    expect_realtime(realtime(args.binary, claimed, headless, fake_output=True), want, "--disable-audio-output")
    if output_works:
        expect_realtime(realtime(args.binary, claimed, headless, fake_output=False), want, "host audio output")
    else:
        print("NOTE: the host has no working audio output; only the fake stream was checked")

    # 2. Workers and the offline render with noise off.
    r0 = checked_dump(args.binary, None, headless, "no profile")
    r1 = checked_dump(args.binary, claimed, headless, "audio.noise false")
    for ctx in WORKERS:
        audio = r1["contexts"][ctx]["audio"]
        if audio.get("offlineAudioContext") != "unavailable" or audio.get("audioContext") != "unavailable":
            raise GuardFailure(f"{ctx}: Web Audio became available: {audio}")
    print(f"PASS: Web Audio stays unavailable in {', '.join(WORKERS)}")
    for ctx in WINDOW_CONTEXTS:
        a, b = offline_keys(r0, ctx), offline_keys(r1, ctx)
        diff = {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
        if diff:
            raise GuardFailure(f"audio.noise false changed the offline render in {ctx}: {diff}")
    print(f"PASS: audio.noise false leaves every offline key unchanged in {', '.join(WINDOW_CONTEXTS)}")

    # 3. Noise on.
    n1 = checked_dump(args.binary, write_profile(profile, work, "noise-s1", dict(claim, noise=True)), headless, "audio.noise S1")
    n2 = checked_dump(args.binary, write_profile(profile, work, "noise-s2", dict(claim, noise=True), SEED_2), headless, "audio.noise S2")
    n1_again = checked_dump(args.binary, write_profile(profile, work, "noise-s1-again", dict(claim, noise=True)), headless, "audio.noise S1 again")
    for ctx in WINDOW_CONTEXTS:
        base, noised = r0["contexts"][ctx], n1["contexts"][ctx]
        if noised["audio"].get("offlineHash") == base["audio"].get("offlineHash"):
            raise GuardFailure(f"vacuous: audio.noise true left the offline render unchanged in {ctx}")
        for key in ("deterministic", "copyFromChannelMatchesGetChannelData", "analyserRepeatReadIdentical"):
            if not noised["audio"].get(key):
                raise GuardFailure(f"{ctx}: audio.{key} is {noised['audio'].get(key)!r} with noise on")
        for key, value in base["audioInvariants"].items():
            if key == "sum4500to5000":
                moved = abs(noised["audioInvariants"][key] - value)
                if not 0 < moved < 1e-3:
                    raise GuardFailure(f"{ctx}: the CreepJS sum moved by {moved!r}, expected more than 0 and under 1e-3")
            elif noised["audioInvariants"].get(key) != value:
                raise GuardFailure(f"{ctx}: audioInvariants.{key} is {noised['audioInvariants'].get(key)!r}, without noise {value!r}")
        if n2["contexts"][ctx]["audio"].get("offlineHash") == noised["audio"].get("offlineHash"):
            raise GuardFailure(f"{ctx}: seeds S1 and S2 render the same")
        if offline_keys(n1_again, ctx) != offline_keys(n1, ctx):
            raise GuardFailure(f"{ctx}: the same seed rendered differently after a restart")
    print(f"PASS: audio.noise changes the render, keeps every detector invariant, depends on the seed and survives a restart in {', '.join(WINDOW_CONTEXTS)}")


finish(main)
