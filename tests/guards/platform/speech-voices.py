"""Guard for patches/phasmid/platform/speech-voices.patch.

speechSynthesis.getVoices() lists speech.voices, never the host's
speech-dispatcher, and speak() with a listed (network) voice behaves as
Google Chrome 156 does when its speech server is unreachable: the utterance
gets no start, end or error event, speechSynthesis stays speaking and later utterances pending,
pause() changes nothing, and cancel() ends it with "interrupted". Checked in
the main frame and same- and cross-origin iframes (speechSynthesis is not
exposed to workers).

  0. precondition, no profile: this build lists no voices (no network engine,
     speech-dispatcher off), so a list seen in check 1 comes from the patch;
     with --enable-speech-dispatcher, whether the host's speech-dispatcher
     gives voices decides if check 2 can observe a leak;
  1. speech.voices mutated (reversed, every third voice dropped, so the
     default is another voice and the list is one no build has): getVoices()
     equals it, delivered by voiceschanged, and speak() behaves as above;
  2. the same with --enable-speech-dispatcher, when the host has voices;
  3. speech.voices empty: getVoices() is empty and speak() fails at once with
     "synthesis-failed", as Chromium without voices;
  4. voices with local_service true, as Windows lists its Microsoft voices
     (the first and the last, which speak() uses, besides any the profile
     lists), on a windows profile (the profile under test when it is one,
     else its Windows variant): the browser starts with one speech.voices
     warning, getVoices() reports them local, and
     speak() with the local voice takes the same silent path as above,
     never the host's speech-dispatcher;
  5. voice_uri other than the name, default on a later voice, and a
     repeated name are refused at startup naming the field.

Skips when the host is offline: Chrome lists no network voices then.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import WINDOW_CONTEXTS, GuardFailure, GuardSkip, expect_warned, finish, load_profile, parse_args, run_binary, start_and_exit, windows_variant

HANG_MS = 3000

SCRIPT = """async ([speak, hangMs]) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const state = () => ({ speaking: speechSynthesis.speaking, pending: speechSynthesis.pending, paused: speechSynthesis.paused });
  let changed = false;
  if (!speechSynthesis.getVoices().length) {
    await new Promise((res) => {
      const t = setTimeout(res, 3000);
      speechSynthesis.addEventListener("voiceschanged", () => { changed = true; clearTimeout(t); res(); }, { once: true });
    });
  }
  const all = speechSynthesis.getVoices();
  const out = {
    online: navigator.onLine,
    changed,
    voices: all.map((v) => ({ name: v.name, lang: v.lang, voiceURI: v.voiceURI, localService: v.localService, default: v.default })),
  };
  if (!speak) return out;
  const events = [];
  const watch = (u, tag) => {
    const t0 = performance.now();
    for (const ev of ["start", "end", "error", "boundary", "pause", "resume"]) {
      u.addEventListener(ev, (e) => events.push({ u: tag, type: ev, error: e.error || null, ms: Math.round(performance.now() - t0) }));
    }
  };
  const first = new SpeechSynthesisUtterance("Phasmid speech guard.");
  first.volume = 0;
  if (all.length) first.voice = all[all.length - 1];
  watch(first, "first");
  speechSynthesis.speak(first);
  await sleep(hangMs);
  out.afterSpeak = state();
  speechSynthesis.pause();
  await sleep(300);
  out.afterPause = state();
  speechSynthesis.resume();
  await sleep(300);
  const second = new SpeechSynthesisUtterance("Second.");
  second.volume = 0;
  watch(second, "second");
  speechSynthesis.speak(second);
  await sleep(300);
  out.queued = state();
  speechSynthesis.cancel();
  await sleep(1000);
  out.afterCancel = state();
  out.events = events;
  return out;
}"""

_ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""

IDLE = {"speaking": False, "pending": False, "paused": False}


def probe(binary: str, profile: Path | None, *, headless: bool, speak: bool, extra_args: list[str] | None = None) -> dict:
    """The script's result in the main frame and a same- and a cross-origin
    iframe, run one after the other (the speech queue is the browser's)."""
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=extra_args or [])
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            results = {"main": page.evaluate(SCRIPT, [speak, HANG_MS])}
            for ctx, origin in (("iframe_same", servers.a), ("iframe_cross", servers.b)):
                page.evaluate(_ADD_FRAME, f"{origin.url}/h/guard-{ctx}")
                frame = next(f for f in page.frames if f.url.endswith(f"/h/guard-{ctx}"))
                results[ctx] = frame.evaluate(SCRIPT, [speak, HANG_MS])
            return results
        finally:
            launch.stop()


def write_profile(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile, ensure_ascii=False))
    return path


def with_voices(profile: dict, voices: list[dict]) -> dict:
    p = copy.deepcopy(profile)
    p["speech"]["voices"] = voices
    return p


def mutated(voices: list[dict]) -> list[dict]:
    kept = [v for i, v in enumerate(reversed(voices)) if i % 3 != 2]
    return [{**v, "default": i == 0} for i, v in enumerate(kept)]


def as_js(voices: list[dict]) -> list[dict]:
    return [{"name": v["name"], "lang": v["lang"], "voiceURI": v["voice_uri"], "localService": v["local_service"], "default": v["default"]} for v in voices]


def expect_voices(results: dict, expected: list[dict], label: str) -> None:
    bad = {ctx: r["voices"] for ctx, r in results.items() if r["voices"] != expected}
    if bad:
        raise GuardFailure(f"{label}: getVoices() expected {expected!r}, got {bad}")
    print(f"PASS: {label}: getVoices() lists {len(expected)} voices in {', '.join(results)}")


def expect_unreachable_engine(results: dict, label: str) -> None:
    """Google Chrome 156 with its speech server unreachable, in every frame."""
    bad = {}
    for ctx, r in results.items():
        if not r["changed"]:
            bad[ctx] = "no voiceschanged event delivered the list"
        elif r["afterSpeak"] != {**IDLE, "speaking": True}:
            bad[ctx] = f"after speak(): {r['afterSpeak']}, expected speaking and nothing else"
        elif r["afterPause"] != {**IDLE, "speaking": True}:
            bad[ctx] = f"after pause(): {r['afterPause']}, expected still speaking, not paused"
        elif r["queued"] != {**IDLE, "speaking": True, "pending": True}:
            bad[ctx] = f"after a second speak(): {r['queued']}, expected speaking and pending"
        elif r["afterCancel"] != IDLE:
            bad[ctx] = f"after cancel(): {r['afterCancel']}"
        elif [(e["u"], e["type"], e["error"]) for e in r["events"]] != [("first", "error", "interrupted")]:
            bad[ctx] = f"events {r['events']}, expected only the first utterance's 'interrupted' error at cancel()"
    if bad:
        raise GuardFailure(f"{label}: speak() does not behave as Google Chrome with its speech server unreachable: {bad}")
    print(f"PASS: {label}: speak() waits silently until cancel() ends it with 'interrupted' in {', '.join(results)}")


def expect_synthesis_failed(results: dict, label: str) -> None:
    bad = {}
    for ctx, r in results.items():
        firsts = [e for e in r["events"] if e["u"] == "first"]
        if not firsts or firsts[0]["type"] != "error" or firsts[0]["error"] != "synthesis-failed" or firsts[0]["ms"] > 500:
            bad[ctx] = r["events"]
        elif r["afterCancel"] != IDLE:
            bad[ctx] = f"after cancel(): {r['afterCancel']}"
    if bad:
        raise GuardFailure(f"{label}: expected speak() to fail at once with synthesis-failed, got {bad}")
    print(f"PASS: {label}: speak() fails at once with synthesis-failed in {', '.join(results)}")


def expect_refused(binary: str, profile_path: Path, work: Path, field: str) -> None:
    r = run_binary(binary, [f"--phasmid-profile={profile_path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{profile_path.name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {profile_path.name} refused at startup naming {field}")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    voices = profile["speech"]["voices"]
    if len(voices) < 3:
        raise GuardFailure(f"the profile lists {len(voices)} voices; the mutation needs at least 3")

    base = probe(args.binary, None, headless=headless, speak=False)
    if not base["main"]["online"]:
        raise GuardSkip("the host is offline; Chrome lists no network voices then")
    expect_voices(base, [], "precondition: no profile")
    speechd = probe(args.binary, None, headless=headless, speak=False, extra_args=["--enable-speech-dispatcher"])
    host_voices = speechd["main"]["voices"]
    print(f"precondition: the host's speech-dispatcher gives {len(host_voices)} voices")

    claimed = mutated(voices)
    if claimed == voices or not claimed:
        raise GuardFailure("the mutation did not change the voice list")
    path = write_profile(with_voices(profile, claimed), work, "voices-mutated")
    results = probe(args.binary, path, headless=headless, speak=True)
    if set(results) != set(WINDOW_CONTEXTS):
        raise GuardFailure(f"contexts probed {sorted(results)}, expected {sorted(WINDOW_CONTEXTS)}")
    expect_voices(results, as_js(claimed), "mutated speech.voices")
    expect_unreachable_engine(results, "mutated speech.voices")

    if host_voices:
        results = probe(args.binary, path, headless=headless, speak=False, extra_args=["--enable-speech-dispatcher"])
        expect_voices(results, as_js(claimed), "mutated speech.voices with --enable-speech-dispatcher")
    else:
        print("NOTE: the host's speech-dispatcher gives no voices, so a leak through --enable-speech-dispatcher is not observable here")

    empty = write_profile(with_voices(profile, []), work, "voices-empty")
    results = probe(args.binary, empty, headless=headless, speak=True)
    expect_voices(results, [], "empty speech.voices")
    expect_synthesis_failed(results, "empty speech.voices")

    local = copy.deepcopy(claimed)
    local[0]["local_service"] = True
    local[-1]["local_service"] = True
    # A windows profile's own list may already have local voices.
    win = with_voices(profile if profile["os"]["family"] == "windows" else windows_variant(profile), local)
    locals_ = [v["name"] for v in local if v["local_service"]]
    r = start_and_exit(args.binary, win, work, "voices-local")
    expect_warned(r, "local voices", "speech.voices", f"{len(locals_)} voices have local_service true ({locals_[0]} first)")
    results = probe(args.binary, work / "voices-local.json", headless=headless, speak=True, extra_args=["--enable-speech-dispatcher"])
    expect_voices(results, as_js(local), "local speech.voices")
    expect_unreachable_engine(results, "local speech.voices")
    uri = copy.deepcopy(claimed)
    uri[0]["voice_uri"] = uri[0]["name"] + " (network)"
    expect_refused(args.binary, write_profile(with_voices(profile, uri), work, "voices-uri"), work, "speech.voices[0].voice_uri")
    default = copy.deepcopy(claimed)
    default[2]["default"] = True
    expect_refused(args.binary, write_profile(with_voices(profile, default), work, "voices-default"), work, "speech.voices[2].default")
    repeated = copy.deepcopy(claimed) + [copy.deepcopy(claimed[1])]
    repeated[-1]["default"] = False
    expect_refused(args.binary, write_profile(with_voices(profile, repeated), work, "voices-repeated"), work, f"speech.voices[{len(claimed)}].name")


finish(main)
