"""Shared pieces for guard scripts.

A guard is a standalone script (see guards/__main__.py for the contract).
These helpers run the consistency harness against a binary with a profile and
let a guard assert one surface in every context without re-implementing the
dump. Every helper raises GuardFailure with a message a maintainer can act on.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consistency.compare import _get  # noqa: E402
from consistency.dump import run_dump  # noqa: E402

ALL_CONTEXTS = ("main", "iframe_same", "iframe_cross", "worker", "shared_worker", "service_worker")
WINDOW_CONTEXTS = ("main", "iframe_same", "iframe_cross")


class GuardFailure(Exception):
    pass


class GuardSkip(Exception):
    """The host cannot run this guard (for example no hardware GPU). Reported
    as a skip with the reason, never as a pass."""


def parse_args(description: str) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--binary", required=True)
    p.add_argument("--profile", default="")
    p.add_argument("--work-dir", default=tempfile.mkdtemp(prefix="phasmid-guard-"))
    p.add_argument("--headful", action="store_true")
    return p.parse_args()


def load_profile(path: str) -> dict:
    if not path:
        raise GuardFailure("this guard needs --profile")
    return json.loads(Path(path).read_text())


# How the browser logs a valid claim this host or build cannot back,
# followed by "<field>: <detail>".
UNBACKED = "Phasmid: host cannot back "

# A Windows 11 desktop's font list as a windows profile states it: the
# Windows names this Linux host has no files for.
WINDOWS_FAMILIES = ["Arial", "Calibri", "Cambria", "Consolas", "Courier New", "Georgia", "Segoe UI", "Segoe UI Emoji",
                    "Tahoma", "Times New Roman", "Verdana"]


def windows_variant(profile: dict) -> dict:
    """The profile as a Windows 11 desktop with the same hardware: the
    os.* identity, the 10 ms WASAPI base latency, BGRA8 WebGPU canvases,
    Windows font names and UI font, no Widevine, and navigator.bluetooth and
    navigator.share, which Windows Chrome always has (core/profile.patch
    refuses a windows profile without them). Every other field is the
    profile's, so the variant is valid wherever the profile is."""
    p = json.loads(json.dumps(profile))
    p["os"] = {"family": "windows", "version": "11", "ua_platform_token": "Windows NT 10.0; Win64; x64", "navigator_platform": "Win32",
               "ch_platform": "Windows", "ch_platform_version": "19.0.0", "architecture": "x86", "bitness": "64", "wow64": False,
               "model": "", "form_factors": ["Desktop"]}
    p["audio"]["base_latency"] = (p["audio"]["sample_rate"] // 100) / p["audio"]["sample_rate"]
    if p["gpu"].get("webgpu", {}).get("available"):
        p["gpu"]["webgpu"]["preferred_canvas_format"] = "bgra8unorm"
    p["fonts"] = {"families": list(WINDOWS_FAMILIES), "emoji": "Segoe UI Emoji", "ui_font": {"family": "Segoe UI", "size_px": 12}}
    p["media"]["widevine"] = {"supported": False, "robustness": []}
    p["capabilities"]["bluetooth"] = True
    p["capabilities"]["web_share"] = True
    return p


def store_root() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "phasmid" / "fonts" / "windows"


def find_store(families: list[str]) -> tuple[Path, dict]:
    """The store the browser picks by default (font_allowlist_linux.cc
    FindWindowsStore): a <build> directory whose manifest.json names that
    build, the one with most of the listed families."""
    root = store_root()
    best = None
    for d in sorted(p for p in root.glob("*") if p.is_dir()):
        try:
            manifest = json.loads((d / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        if manifest.get("os", {}).get("build") != d.name:
            continue
        names = {f.lower() for f in manifest.get("families", {})} | {f.lower() for f in manifest.get("aliases", {})}
        listed = sum(f.lower() in names for f in families)
        key = (listed, tuple(int(x) for x in d.name.split(".") if x.isdigit()))
        if best is None or key > best[0]:
            best = (key, d, manifest)
    if best is None:
        raise GuardSkip(f"no Windows font store (a <build> directory with manifest.json) under {root}; import the files and copy the "
                        f"committed manifest (profiles/data/fonts/<set>.json) there as manifest.json")
    return best[1], best[2]


def unbacked_warnings(stderr: str) -> dict[str, list[str]]:
    """The "host cannot back" lines of a browser's stderr, by field."""
    out: dict[str, list[str]] = {}
    for line in stderr.splitlines():
        if UNBACKED in line:
            field, _, detail = line.split(UNBACKED, 1)[1].partition(": ")
            out.setdefault(field, []).append(detail)
    return out


def start_and_exit(binary: str, profile: dict, work: Path, name: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Start the browser with `profile` on about:blank; --dump-dom makes a
    browser that accepts the profile exit 0 once the page has loaded."""
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return run_binary(binary, [f"--phasmid-profile={path}", *(extra_args or []), "--headless=new", "--dump-dom",
                               f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)


def expect_refused(binary: str, profile: dict, work: Path, name: str, field: str) -> None:
    """The browser stops at load, naming `field`."""
    r = start_and_exit(binary, profile, work, name)
    if r.returncode == 0 or f"'{field}'" not in r.stderr:
        raise GuardFailure(f"{name}: expected a refusal naming {field}, got exit {r.returncode}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {name} exits {r.returncode} naming {field}")


def expect_warned(r: subprocess.CompletedProcess, label: str, field: str, *needles: str) -> str:
    """The browser accepted the profile (exit 0) and logged `field` as
    unbacked exactly once, with every needle in the detail."""
    warnings = unbacked_warnings(r.stderr)
    details = warnings.get(field, [])
    if r.returncode != 0 or len(details) != 1 or not all(n in details[0] for n in needles):
        raise GuardFailure(f"{label}: expected exit 0 and one '{UNBACKED}{field}' warning containing {list(needles)}, got exit {r.returncode}, "
                           f"warnings {warnings}, stderr {r.stderr[-400:]!r}")
    print(f"PASS: {label}: starts and warns {field}: {details[0]}")
    return details[0]


def dump(binary: str, profile: str | None, *, headless: bool = True, extra_args: list[str] | None = None, env: dict | None = None) -> dict:
    return run_dump(Path(binary), headless=headless, profile=Path(profile) if profile else None, extra_args=extra_args or [], env=env or {}, timeout=120)


def expect_everywhere(d: dict, dump_path: str, expected: Any, contexts=ALL_CONTEXTS, label: str | None = None) -> None:
    """Assert a dump key equals `expected` in every given context."""
    label = label or dump_path
    bad = {}
    for ctx in contexts:
        c = d["contexts"].get(ctx)
        if not isinstance(c, dict):
            bad[ctx] = "<context missing>"
            continue
        actual = _get(c, dump_path)
        if actual != expected:
            bad[ctx] = actual
    if bad:
        raise GuardFailure(f"{label}: expected {expected!r} in every context, got {bad}")
    print(f"PASS: {label} == {expected!r} in {len(contexts)} contexts")


def expect_header(d: dict, slot: str, header: str, expected: str) -> None:
    actual = d["headers"].get(slot, {}).get(header)
    if actual != expected:
        raise GuardFailure(f"header {header} on {slot}: expected {expected!r}, got {actual!r}")
    print(f"PASS: header {header} on {slot} == {expected!r}")


def run_binary(binary: str, args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)


def finish(fn) -> None:
    """Run a guard body; print FAIL and exit 1 on GuardFailure, SKIP and exit 3
    on GuardSkip, 2 on anything else."""
    try:
        fn()
    except GuardFailure as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    except GuardSkip as exc:
        print(f"SKIP: {exc}")
        sys.exit(3)
    except Exception as exc:  # noqa: BLE001 (report, do not hide)
        print(f"ERROR: {type(exc).__name__}: {exc}")
        sys.exit(2)
    sys.exit(0)
