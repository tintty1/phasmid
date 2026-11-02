"""Static checks for the fingerprint inspector (devtools/fingerprint-trace.patch).

Only allowlisted bindings carry the trace. The build never sets GN
`enable_blink_bindings_tracing`, which would trace every Web API call. A "*"
allowlist entry is for local wide runs only; the committed list names
interfaces.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches" / "phasmid" / "devtools" / "fingerprint-trace.patch"
ALLOWLIST = "third_party/blink/renderer/bindings/phasmid_fingerprint_allowlist.txt"
BUILD_ARG = re.compile(r"enable_blink_bindings_tracing\s*=")


def _added_file(patch: Path, path: str) -> list[str]:
    """The lines a patch adds to `path`."""
    lines, inside = [], False
    for line in patch.read_text().splitlines():
        if line.startswith("+++ "):
            inside = line[4:].split("\t")[0].strip() == f"b/{path}"
            continue
        if line.startswith("--- ") or line.startswith("diff "):
            inside = False
            continue
        if inside and line.startswith("+"):
            lines.append(line[1:])
    return lines


def _entries() -> list[str]:
    entries = []
    for line in _added_file(PATCH, ALLOWLIST):
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def test_committed_allowlist_names_interfaces():
    """The committed allowlist has no wildcard."""
    entries = _entries()
    assert entries, f"{PATCH.name} adds no entries to {ALLOWLIST}"
    assert "*" not in entries, "'*' traces every Web API; it is for local wide runs, never committed"
    bad = [e for e in entries if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*\.(\*|[A-Za-z_][A-Za-z0-9_]*)", e)]
    assert not bad, f"entries must be Interface.member or Interface.*: {bad}"
    dupes = sorted({e for e in entries if entries.count(e) > 1})
    assert not dupes, f"duplicate entries: {dupes}"


def _gn_files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT)
        # build/ holds the unpacked Chromium tree; hidden dirs hold worktrees and caches.
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not (rel == Path(".") and d == "build")
                       and d not in ("node_modules", "out")]
        out += [Path(dirpath) / f for f in filenames if f.endswith((".gn", ".gni"))]
    return out


def test_bindings_tracing_build_arg_is_never_set():
    """The build argument stays unset."""
    offenders = []
    for path in [ROOT / "flags.gn", *_gn_files(), *sorted((ROOT / "scripts").rglob("*"))]:
        if path.is_file() and BUILD_ARG.search(path.read_text(errors="replace")):
            offenders.append(str(path.relative_to(ROOT)))
    for patch in sorted((ROOT / "patches").rglob("*.patch")):
        added = [line for line in patch.read_text(errors="replace").splitlines() if line.startswith("+")]
        if any(BUILD_ARG.search(line) for line in added):
            offenders.append(str(patch.relative_to(ROOT)))
    assert not offenders, f"enable_blink_bindings_tracing must stay unset (tier 2 only): {offenders}"
