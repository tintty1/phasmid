"""Guard for patches/phasmid/fonts/availability.patch.

Only font files that publish a family in fonts.families are reachable, on
every path and in every context; a claim the host's fonts cannot back runs
with a warning. The guard keeps the profile's families the host publishes and drops one more that is certainly
installed (DejaVu Sans), so every host family outside that list must behave
as uninstalled. Host facts come from fc-list.

  0. precondition, no profile: this build reaches the dropped families:
     present by width in every context, local() loads their faces, they are
     in queryLocalFonts(), and code points only they cover draw a glyph
     (otherwise checks 1 and 2 are vacuous);
  1. with the profile, dropped families are absent by width in main, same-
     and cross-origin iframes, dedicated, shared and service workers
     (OffscreenCanvas in the workers), while listed families stay present;
     a dropped name fontconfig resolves to a listed family on the kept
     files (Arial to Liberation Sans) is a metric alias, present in stock
     Chrome with those files too, and is left out;
  2. local() of dropped PostScript and full names fails and of listed faces
     loads, in the three window contexts and a dedicated worker;
     queryLocalFonts() (permission granted over CDP) lists no dropped face
     in the main frame and same-origin iframe; code points only dropped
     fonts cover measure like a code point no font has, on canvas and in a
     worker's OffscreenCanvas (fallback reaches no dropped font);
  3. fonts.default_serif set to another listed family, with fonts.emoji set
     to the family the host's fontconfig picks for emoji: the browser
     starts, and serif and the standard font measure as that family in
     windows and the dedicated worker;
  4. started with one "host cannot back" warning naming the field: a family
     no host font publishes (fonts.families, naming it), a list of which no
     family is on the host (fonts.families, all of them absent: the emptied
     allowlist still loads), an emoji family the host does not pick
     (fonts.emoji); refused at startup naming the field: a default family
     outside the list (fonts.default_sans) and a Linux ui_font other than
     Arial at 16 px (fonts.ui_font);
  5. a windows profile goes through the same filter: the Windows families
     the host publishes are present by width in every context, the dropped
     family is absent; in shared and service workers, which get no generic
     font settings, every generic is the last-resort Arial and Arial is
     therefore undetectable by width, as in Windows Chrome 155.

With a windows profile under test (fonts from the Windows font store),
checks 1 to 5, which
split the host's fontconfig files, give way to W; fonts/windows_metrics.py
measures the same profile's text against Windows Chrome 156:

  W. precondition, no profile: the host families outside the profile's list
     are present by width in every context and local() loads their faces;
     with the profile they are absent by width in all six contexts,
     local() of their faces fails in the window contexts and a dedicated
     worker, and queryLocalFonts() lists none of them, while the profile's
     families the store holds and the probe measures are present in every
     context (Arial and its aliases excepted in shared and service workers,
     as in check 5).
     Skips without a Windows font store.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import (ALL_CONTEXTS, WINDOWS_FAMILIES, GuardFailure, GuardSkip, dump, expect_warned, find_store, finish, load_profile, parse_args,
                     run_binary, start_and_exit, windows_variant)

DROP = "DejaVu Sans"
SCALABLE_FORMATS = ("TrueType", "CFF")
EMOJI = 0x1F600
TEXT = "The quick brown fox jumps over the lazy dog 0123456789"
WINDOW_FRAMES = ("main", "iframe_same", "iframe_cross")
NO_SETTINGS_WORKERS = ("shared_worker", "service_worker")

# Runs in a window (document present) or a dedicated worker.
PROBE = """async (a) => {
  const out = { local: {}, widths: {}, generic: {} };
  for (const n of a.names) {
    try { await new FontFace("phasmid-guard", `local("${n}")`).load(); out.local[n] = true; }
    catch (e) { out.local[n] = e.name; }
  }
  const doc = typeof document !== "undefined";
  const c = doc ? document.createElement("canvas") : new OffscreenCanvas(64, 64);
  const x = c.getContext("2d");
  x.font = `48px "${a.family}"`;
  for (const cp of a.cps.concat([a.uncovered])) out.widths[cp] = x.measureText(String.fromCodePoint(cp)).width;
  for (const g of ["serif", "sans-serif", "monospace", `"${a.serif}"`]) { x.font = `16px ${g}`; out.generic[g] = x.measureText(a.text).width; }
  if (doc) {
    const s = document.createElement("span");
    s.style.cssText = "position:absolute;white-space:nowrap;font-size:16px";
    s.textContent = a.text;
    document.body.appendChild(s);
    out.standard = s.getBoundingClientRect().width;
    s.style.fontFamily = `"${a.serif}"`;
    out.standardAsSerif = s.getBoundingClientRect().width;
    s.remove();
    try { out.qlf = (await window.queryLocalFonts()).map((f) => f.postscriptName); }
    catch (e) { out.qlf = e.name; }
  }
  return out;
}"""

_IN_WORKER = """(src) => new Promise((res) => {
  const w = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
  w.onmessage = (e) => res(e.data);
  w.onerror = (e) => res({ error: String(e.message) });
})"""

_ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""


def lower(s: str) -> str:
    return s.lower()


def host_fonts() -> list[dict]:
    """Every font fontconfig lists on this host, one row per face."""
    fmt = "%{file}\\t%{family}\\t%{postscriptname}\\t%{fullname}\\t%{fontformat}\\t%{scalable}\\t%{charset}\\n"
    try:
        out = subprocess.run(["fc-list", "--format", fmt], capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardSkip(f"fc-list is not available: {exc}") from exc
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        file, families, ps, full, fmt_, scalable, charset = parts
        ranges = []
        for part in charset.split():
            lo, _, hi = part.partition("-")
            ranges.append((int(lo, 16), int(hi or lo, 16)))
        rows.append({
            "file": file,
            "families": [f.strip() for f in families.split(",") if f.strip()],
            "ps": ps,
            "full": [f.strip() for f in full.split(",") if f.strip()],
            "outline": fmt_ in SCALABLE_FORMATS and scalable == "True",
            "ranges": ranges,
        })
    return rows


def covers(row: dict, cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in row["ranges"])


class Host:
    """The host's fonts split by a family list, as the patch splits them:
    a file is kept when one of its faces publishes a listed family."""

    def __init__(self, rows: list[dict], families: list[str]):
        listed = {lower(f) for f in families}
        kept_files = {r["file"] for r in rows if any(lower(f) in listed for f in r["families"])}
        self.kept = [r for r in rows if r["file"] in kept_files]
        self.dropped = [r for r in rows if r["file"] not in kept_files]
        kept_families = {lower(f) for r in self.kept for f in r["families"]}
        self.dropped_families = {f for r in self.dropped for f in r["families"] if lower(f) not in kept_families}
        kept_ps = {r["ps"] for r in self.kept}
        self.dropped_ps = {r["ps"] for r in self.dropped if r["ps"] and r["ps"] not in kept_ps}


def metric_aliases(host: Host, families: list[str], names: list[str], work: Path) -> set[str]:
    """Names among `names` that fontconfig, limited to the kept files as the
    patch limits it, resolves to a listed family other than the generic
    default (Arial to Liberation Sans): metric aliases, which stock Chrome on
    a host with only those files also measures as present."""
    rejects = "".join(f"<glob>{escape(r['file'])}</glob>" for r in host.dropped)
    conf = work / "fonts-kept.conf"
    conf.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig>'
                    '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
                    f"<selectfont><rejectfont>{rejects}</rejectfont></selectfont></fontconfig>")
    env = {**os.environ, "FONTCONFIG_FILE": str(conf)}

    def match(name: str) -> str:
        out = subprocess.run(["fc-match", "--format", "%{family[0]}", name], capture_output=True, text=True, timeout=30, env=env)
        return lower(out.stdout.strip())

    listed = {lower(f) for f in families}
    generic = {match(g) for g in ("sans-serif", "serif", "monospace")}
    return {n for n in names if (m := match(n)) in listed and m not in generic}


def write(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def probe(binary: str, profile: Path | None, args: dict, *, headless: bool) -> dict:
    """PROBE in the three window contexts and a dedicated worker, with the
    local-fonts permission granted to both origins."""
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile)
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            cdp = context.new_cdp_session(page)
            for origin in (servers.a.url, servers.b.url):
                cdp.send("Browser.grantPermissions", {"permissions": ["localFonts"], "origin": origin})
            page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
            out = {"main": page.evaluate(PROBE, args)}
            for ctx, origin in (("iframe_same", servers.a.url), ("iframe_cross", servers.b.url)):
                page.evaluate(_ADD_FRAME, f"{origin}/h/guard-{ctx}")
                frame = next(f for f in page.frames if f.url.endswith(f"/h/guard-{ctx}"))
                out[ctx] = frame.evaluate(PROBE, args)
            out["worker"] = page.evaluate(_IN_WORKER, f"({PROBE})({json.dumps(args)}).then((r) => postMessage(r), (e) => postMessage({{ error: String(e) }}));")
            for ctx, r in out.items():
                if "error" in r:
                    raise GuardFailure(f"probe failed in {ctx}: {r['error']}")
            return out
        finally:
            launch.stop()


def present(d: dict, ctx: str) -> set[str]:
    c = d["contexts"].get(ctx)
    if not isinstance(c, dict) or not isinstance(c.get("fonts", {}).get("present"), list):
        raise GuardFailure(f"{ctx}: no fonts.present in the dump")
    return {lower(f) for f in c["fonts"]["present"]}


def emoji_pick() -> str | None:
    """The family fontconfig's emoji fallback picks on this host: the first
    outline font covering U+1F600 in its order for und-Zsye."""
    fmt = "%{family[0]}\\t%{fontformat}\\t%{scalable}\\t%{charset}\\n"
    out = subprocess.run(["fc-match", "-a", "--format", fmt, ":lang=und-zsye:scalable=true"], capture_output=True, text=True, timeout=60).stdout
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[1] not in SCALABLE_FORMATS or parts[2] != "True":
            continue
        row = {"ranges": []}
        for part in parts[3].split():
            lo, _, hi = part.partition("-")
            row["ranges"].append((int(lo, 16), int(hi or lo, 16)))
        if covers(row, EMOJI):
            return parts[0]
    return None


def expect_refused(binary: str, profile: dict, work: Path, name: str, field: str) -> None:
    path = write(profile, work, name)
    r = run_binary(binary, [f"--phasmid-profile={path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
    if r.returncode == 0 or field not in r.stderr:
        raise GuardFailure(f"{name}: expected a startup refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {name} refused at startup naming {field}")


def check_windows_profile(binary: str, profile: dict, work: Path, headless: bool) -> None:
    """Check W."""
    store, manifest = find_store(profile["fonts"]["families"])
    in_store = {lower(f) for f in manifest.get("families", {})} | {lower(f) for f in manifest.get("aliases", {})}
    # Arial and the names the store aliases to it (Helvetica): the last-resort
    # font of shared and service workers, undetectable by width there.
    arial = {"arial"} | {lower(a) for a, t in manifest.get("aliases", {}).items() if lower(t) == "arial"}
    rows = host_fonts()
    listed = {lower(f) for f in profile["fonts"]["families"]}
    host = Host(rows, profile["fonts"]["families"])
    stock = dump(binary, None, headless=headless)
    if stock["context_errors"]:
        raise GuardFailure(f"contexts failed without a profile: {stock['context_errors']}")
    host_probed = sorted(f for f in host.dropped_families if lower(f) not in listed and all(lower(f) in present(stock, c) for c in ALL_CONTEXTS))
    # Faces the store also holds are the profile's own (this host may have
    # Windows fonts installed); local() reaches those on Windows too.
    store_text = json.dumps(manifest)
    faces = [r for r in host.dropped if r["outline"] and r["ps"] in host.dropped_ps and r["ps"].split("-")[0] not in store_text][:4]
    names = list(dict.fromkeys(n for r in faces for n in [r["ps"], *r["full"][:1]]))
    probe_args = {"names": names, "cps": [], "uncovered": 0x10FFFD, "family": "Arial", "serif": "Times New Roman", "text": TEXT}
    base = probe(binary, None, probe_args, headless=headless)
    names = [n for n in names if all(r["local"][n] is True for r in base.values())]
    if not host_probed or not names:
        raise GuardFailure(f"vacuous: without a profile the host families outside the profile's list present everywhere are {host_probed}, "
                           f"and local() loads {names} of their faces")
    print(f"PASS: precondition: without a profile {host_probed} are present in {len(ALL_CONTEXTS)} contexts and local() loads {names}")

    path = write(profile, work, "fonts-windows-profile")
    d = dump(binary, str(path), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed with the profile: {d['context_errors']}")
    measured = {lower(f) for f in d["contexts"]["main"]["fonts"]["metrics"]}
    expected = sorted(f for f in profile["fonts"]["families"] if lower(f) in in_store and lower(f) in measured)
    if not expected:
        raise GuardFailure(f"vacuous: the probe measures none of the profile's families the store {store} holds")
    bad = {}
    for ctx in ALL_CONTEXTS:
        seen = present(d, ctx)
        want = [f for f in expected if not (ctx in NO_SETTINGS_WORKERS and lower(f) in arial)]
        lost = [f for f in want if lower(f) not in seen]
        leaked = [f for f in host_probed if lower(f) in seen]
        if lost or leaked:
            bad[ctx] = {"listed but absent": lost, "host family present": leaked}
    if bad:
        raise GuardFailure(f"windows profile: font presence does not follow fonts.families over the store: {bad}")
    print(f"PASS: windows profile: {len(expected)} listed store families present and {host_probed} absent in {len(ALL_CONTEXTS)} contexts")

    r = probe(binary, path, probe_args, headless=headless)
    loaded = {ctx: [n for n in names if res["local"][n] is True] for ctx, res in r.items()}
    if any(loaded.values()):
        raise GuardFailure(f"windows profile: local() loads host faces: {loaded}")
    print(f"PASS: windows profile: local() refuses the host faces {names} in {', '.join(r)}")
    for ctx in ("main", "iframe_same"):
        qlf = r[ctx]["qlf"]
        if not isinstance(qlf, list) or not qlf:
            raise GuardFailure(f"windows profile: queryLocalFonts() in {ctx} with the permission granted: {qlf!r}")
        leaked = sorted(ps for ps in set(qlf) & host.dropped_ps if ps.split("-")[0] not in store_text)
        if leaked:
            raise GuardFailure(f"windows profile: queryLocalFonts() in {ctx} lists host faces: {leaked[:8]}")
    print(f"PASS: windows profile: queryLocalFonts() lists {len(r['main']['qlf'])} faces and none of the host's {len(host.dropped_ps)}")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    if profile["os"]["family"] == "windows":
        check_windows_profile(args.binary, profile, work, headless)
        return
    if profile["os"]["family"] != "linux":
        raise GuardFailure(f"vacuous: the profile claims os.family {profile['os']['family']!r}, neither linux nor windows")

    rows = host_fonts()
    published = {lower(f) for r in rows for f in r["families"]}
    families = [f for f in profile["fonts"]["families"] if lower(f) in published and lower(f) != lower(DROP)]
    if lower(DROP) not in published:
        raise GuardSkip(f"the host has no {DROP} (fc-list), the family this guard drops")
    if len(families) < 10:
        raise GuardSkip(f"the host publishes only {len(families)} of the profile's families; the schema needs 10")
    host = Host(rows, families)
    mutated = copy.deepcopy(profile)
    mutated["fonts"] = {"families": families, "ui_font": {"family": "Arial", "size_px": 16}}

    # What the checks look at. Width presence uses the probe's own list;
    # local() names start with the dropped family's own faces.
    stock = dump(args.binary, None, headless=headless)
    outline_families = {lower(f) for r in host.kept if r["outline"] for f in r["families"]}
    dropped_probed = sorted(f for f in host.dropped_families if lower(f) in present(stock, "main"))
    aliased = metric_aliases(host, families, dropped_probed, work)
    if aliased:
        print(f"INFO: {sorted(aliased)} resolve to listed families by metric alias and stay present, as in stock Chrome on the kept files")
    dropped_probed = [f for f in dropped_probed if f not in aliased]
    listed_probed = sorted(f for f in families if lower(f) in present(stock, "main") and lower(f) in outline_families)
    faces = [r for r in host.dropped if r["outline"] and r["ps"] in host.dropped_ps]
    faces = [r for r in faces if DROP in r["families"]][:2] + [r for r in faces if DROP not in r["families"]][:2]
    dropped_names = list(dict.fromkeys(n for r in faces for n in [r["ps"], *r["full"][:1]]))
    kept_names = sorted({r["ps"] for r in host.kept if r["outline"] and r["ps"]})[:3]
    cps = [cp for cp in list(range(0x20, 0x3000)) + list(range(0x1F000, 0x1FB00))
           if not any(covers(r, cp) for r in host.kept) and any(r["outline"] and covers(r, cp) for r in host.dropped)]
    uncovered = next((cp for cp in (0x10FFFD, 0xFFFFD, 0x10FFF0, 0xFFFF0) if not any(covers(r, cp) for r in rows)), None)
    control = next((f for f in ("Liberation Sans", "Arial", "DejaVu Sans Mono") if f in families), families[0])
    serif = next((f for f in ("DejaVu Serif", "Liberation Serif", "FreeSerif") if f in families), None)
    if not dropped_probed or not listed_probed or not dropped_names or not kept_names or not cps or uncovered is None or serif is None:
        raise GuardFailure(f"vacuous: dropped families on the probe list {dropped_probed}, listed {listed_probed[:3]}, dropped faces {dropped_names[:4]}, kept faces {kept_names}, code points only dropped fonts cover {len(cps)}, uncovered code point {uncovered}, serif candidate {serif}")
    sample = cps[:: max(1, len(cps) // 6)][:6]
    probe_args = {"names": dropped_names + kept_names, "cps": sample, "uncovered": uncovered, "family": control, "serif": serif, "text": TEXT}

    # 0. Precondition: without a profile the dropped fonts are reachable.
    for ctx in ALL_CONTEXTS:
        missing = [f for f in dropped_probed if lower(f) not in present(stock, ctx)]
        if missing:
            raise GuardFailure(f"vacuous: without a profile {missing} are absent in {ctx}")
    print(f"PASS: precondition: without a profile {dropped_probed} are present in {len(ALL_CONTEXTS)} contexts")
    base = probe(args.binary, None, probe_args, headless=headless)
    # Names fontconfig spells differently from fc-list's indexer are not
    # observable here and are left out.
    dropped_names = [n for n in dropped_names if all(r["local"][n] is True for r in base.values())]
    kept_names = [n for n in kept_names if all(r["local"][n] is True for r in base.values())]
    sample = [cp for cp in sample if all(r["widths"][str(cp)] != r["widths"][str(uncovered)] for r in base.values())]
    if not dropped_names or not kept_names or not sample:
        raise GuardFailure(f"vacuous: without a profile local() loads dropped {dropped_names} and listed {kept_names} faces, and code points {[hex(c) for c in sample]} draw a glyph")
    qlf = base["main"]["qlf"]
    if not isinstance(qlf, list) or not set(qlf) & host.dropped_ps:
        raise GuardFailure(f"vacuous: without a profile queryLocalFonts() lists no dropped face: {qlf if not isinstance(qlf, list) else len(qlf)}")
    print(f"PASS: precondition: without a profile local() loads {dropped_names + kept_names}, queryLocalFonts() lists {len(set(qlf) & host.dropped_ps)} dropped faces, and {[hex(c) for c in sample]} draw a glyph, in {', '.join(base)}")

    # 1. Width presence in every context.
    path = write(mutated, work, "fonts-subset")
    d = dump(args.binary, str(path), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed with the profile: {d['context_errors']}")
    bad = {}
    for ctx in ALL_CONTEXTS:
        seen = present(d, ctx)
        leaked = [f for f in dropped_probed if lower(f) in seen]
        lost = [f for f in listed_probed if lower(f) not in seen]
        if leaked or lost:
            bad[ctx] = {"dropped but present": leaked, "listed but absent": lost}
    if bad:
        raise GuardFailure(f"font presence does not follow fonts.families: {bad}")
    print(f"PASS: {dropped_probed} absent and {len(listed_probed)} listed families present in {len(ALL_CONTEXTS)} contexts")

    # 2. local(), queryLocalFonts() and fallback.
    r = probe(args.binary, path, dict(probe_args, cps=sample), headless=headless)
    bad = {}
    for ctx, res in r.items():
        loaded = [n for n in dropped_names if res["local"][n] is True]
        refused = [n for n in kept_names if res["local"][n] is not True]
        drawn = [hex(cp) for cp in sample if res["widths"][str(cp)] != res["widths"][str(uncovered)]]
        if loaded or refused or drawn:
            bad[ctx] = {"local() loads dropped": loaded, "local() fails listed": refused, "fallback draws": drawn}
    if bad:
        raise GuardFailure(f"local() or fallback reaches fonts outside fonts.families: {bad}")
    print(f"PASS: local() refuses {dropped_names} and loads {kept_names}; {[hex(c) for c in sample]} reach no font, in {', '.join(r)}")
    for ctx in ("main", "iframe_same"):
        qlf = r[ctx]["qlf"]
        if not isinstance(qlf, list) or not qlf:
            raise GuardFailure(f"queryLocalFonts() in {ctx} with the permission granted: {qlf!r}")
        leaked = sorted(set(qlf) & host.dropped_ps)
        if leaked:
            raise GuardFailure(f"queryLocalFonts() in {ctx} lists dropped faces: {leaked[:8]}")
    if r["iframe_cross"]["qlf"] != base["iframe_cross"]["qlf"]:
        raise GuardFailure(f"queryLocalFonts() in the cross-origin iframe: {r['iframe_cross']['qlf']!r}, without a profile {base['iframe_cross']['qlf']!r}")
    print(f"PASS: queryLocalFonts() lists {len(r['main']['qlf'])} faces and none of the {len(host.dropped_ps)} dropped ones; cross-origin iframe as without a profile ({base['iframe_cross']['qlf']!r})")

    # 3. Generic families follow fonts.default_serif; a correct emoji claim starts.
    pick = emoji_pick()
    generic = copy.deepcopy(mutated)
    generic["fonts"]["default_serif"] = serif
    if pick and lower(pick) in {lower(f) for f in families}:
        generic["fonts"]["emoji"] = pick
    g = probe(args.binary, write(generic, work, "fonts-default-serif"), dict(probe_args, cps=[]), headless=headless)
    want = g["main"]["generic"][f'"{serif}"']
    if want == r["main"]["generic"]["serif"]:
        raise GuardFailure(f"vacuous: {serif} measures like the default serif ({want})")
    bad = {ctx: res["generic"]["serif"] for ctx, res in g.items() if res["generic"]["serif"] != res["generic"][f'"{serif}"']}
    bad.update({f"{ctx} standard": res["standard"] for ctx, res in g.items() if "standard" in res and res["standard"] != res["standardAsSerif"]})
    if bad:
        raise GuardFailure(f"serif does not follow fonts.default_serif={serif} ({want}): {bad}")
    print(f"PASS: fonts.default_serif={serif}: serif and the standard font measure {want} in {', '.join(g)}" + (f"; fonts.emoji={pick} accepted" if "emoji" in generic["fonts"] else ""))

    # 4. Unbacked claims run with a warning; broken ones are refused.
    missing = copy.deepcopy(mutated)
    missing["fonts"]["families"] = families + ["Phasmid Guard Missing Sans"]
    expect_warned(start_and_exit(args.binary, missing, work, "family-not-installed"), "family-not-installed", "fonts.families",
                  f"1 of {len(families) + 1} families have no font on this host and are absent (Phasmid Guard Missing Sans)")
    nothing = copy.deepcopy(mutated)
    nothing["fonts"]["families"] = [f"Phasmid Guard Missing {i}" for i in range(10)]
    expect_warned(start_and_exit(args.binary, nothing, work, "no-family-installed"), "no-family-installed", "fonts.families", "10 of 10 families")
    wrong_emoji = copy.deepcopy(mutated)
    wrong_emoji["fonts"]["emoji"] = control
    expect_warned(start_and_exit(args.binary, wrong_emoji, work, "emoji-not-picked"), "emoji-not-picked", "fonts.emoji", control)
    outside = copy.deepcopy(mutated)
    outside["fonts"]["default_sans"] = DROP
    expect_refused(args.binary, outside, work, "default-not-listed", "fonts.default_sans")
    ui = copy.deepcopy(mutated)
    ui["fonts"]["ui_font"] = {"family": "Segoe UI", "size_px": 16}
    expect_refused(args.binary, ui, work, "ui-font-not-arial", "fonts.ui_font")

    # 5. A windows profile: the same filter over the host's files.
    win = windows_variant(profile)
    win_present = sorted(f for f in WINDOWS_FAMILIES if lower(f) in published and lower(f) in present(stock, "main"))
    if not win_present:
        print(f"INFO: the host publishes none of {WINDOWS_FAMILIES} that the probe measures; check 5 covers only the dropped family")
    d = dump(args.binary, str(write(win, work, "fonts-windows")), headless=headless)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed with a windows profile: {d['context_errors']}")
    bad = {}
    for ctx in ALL_CONTEXTS:
        seen = present(d, ctx)
        expected = win_present
        if ctx in NO_SETTINGS_WORKERS and "Arial" in win_present:
            # No generic font settings reach shared and service workers
            # (WebSharedWorkerImpl and the service worker start pass empty
            # GenericFontFamilySettings), so on Windows every generic is the
            # last-resort font, Arial, and Arial measures like the bases, as
            # Windows Chrome reports there.
            expected = [f for f in win_present if f != "Arial"]
            fonts = d["contexts"][ctx]["fonts"]
            bases = {b: fonts["baseline"][b] for b in ("monospace", "sans-serif", "serif")}
            widths = {f: fonts["metrics"][f]["width"] for f in ("sans-serif", "serif", "monospace", "Arial")}
            if len({tuple(v) for v in bases.values()}) != 1 or len(set(widths.values())) != 1:
                bad[ctx] = {"generics are not all the last-resort Arial": {"baseline": bases, "16px widths": widths}}
                continue
            if "arial" in seen:
                bad[ctx] = {"Arial present though every generic is Arial": True}
                continue
        lost = [f for f in expected if lower(f) not in seen]
        leaked = [f for f in (DROP,) if lower(f) in seen]
        if lost or leaked:
            bad[ctx] = {"listed but absent": lost, "dropped but present": leaked}
    if bad:
        raise GuardFailure(f"windows profile: font presence does not follow fonts.families: {bad}")
    print(f"PASS: windows profile: {win_present} present and {DROP} absent in {len(ALL_CONTEXTS)} contexts (in shared and service workers "
          f"every generic is the last-resort Arial, as on Windows, so Arial is not detectable there)")


finish(main)
