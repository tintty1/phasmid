"""Static checks on the patch tree and profiles. No build, no browser."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "patches"
PHASMID = PATCHES / "phasmid"

HEADER_FIELDS = ["What:", "Why:", "Profile:", "Hooks:", "Contexts:", "Guard:"]


def _series(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_upstream_series_entries_exist():
    for entry in _series(PATCHES / "series"):
        assert (PATCHES / entry).is_file(), entry


def test_phasmid_series_entries_exist_and_have_headers():
    for entry in _series(PHASMID / "series"):
        path = PHASMID / entry
        assert path.is_file(), entry
        if entry.startswith("build/"):
            # Vendored build fixes from ungoogled-chromium-portablelinux keep
            # their original form; provenance is in build/README.md.
            assert (PHASMID / "build" / "README.md").is_file()
            continue
        head = "\n".join(path.read_text().splitlines()[:40])
        assert head.startswith("Phasmid: "), f"{entry}: first line must be 'Phasmid: <surface>'"
        for field in HEADER_FIELDS:
            assert field in head, f"{entry}: header lacks {field}"
        guard = re.search(r"Guard:\s+(\S+)", head)
        assert guard and (ROOT / guard.group(1)).is_file(), f"{entry}: Guard: must name an existing script"


def test_phasmid_patches_are_all_in_series():
    listed = set(_series(PHASMID / "series"))
    on_disk = {str(p.relative_to(PHASMID)) for p in PHASMID.rglob("*.patch")}
    assert on_disk == listed, f"on disk but not in series: {sorted(on_disk - listed)}; in series but missing: {sorted(listed - on_disk)}"


def test_disabled_upstream_entries_exist_upstream_and_have_reasons():
    """A skipped upstream patch is listed with evidence; it is never edited."""
    upstream = set(_series(PATCHES / "series"))
    for raw in (PHASMID / "disabled-upstream.txt").read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        assert "#" in raw, f"no reason given: {raw}"
        entry, reason = raw.split("#", 1)
        entry = entry.strip()
        assert entry in upstream, f"{entry} is not in the upstream series"
        assert len(reason.strip()) > 40, f"{entry}: the reason must cite evidence"


def test_upstream_patches_are_untouched_by_git():
    """Upstream ungoogled-chromium patches are never edited in place."""
    import subprocess

    diff = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", "patches/series", "patches/core", "patches/extra", "patches/upstream-fixes"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert diff.strip() == "", f"upstream patches modified:\n{diff}"


def test_example_profiles_validate_and_have_captures():
    from consistency.capture import validate

    examples = sorted((ROOT / "profiles" / "examples").glob("*.json"))
    assert examples
    for path in examples:
        profile = json.loads(path.read_text())
        assert validate(profile) == [], path.name
        assert profile["id"] == path.stem
        captures = list((ROOT / "profiles" / "data" / "captures").glob(f"{path.stem}.*.json"))
        assert captures, f"{path.name}: no capture dump under profiles/data/captures (never invent device data)"


def test_schema_fields_are_never_deleted():
    """Fields leave profiles/schema.json only by being listed in removed_fields.
    Compared against the schema at the previous commit when there is one."""
    import subprocess

    schema_path = "profiles/schema.json"
    try:
        old = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{schema_path}"], capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return  # first commit of the schema
    old_props = set(json.loads(old)["properties"])
    new_schema = json.loads((ROOT / schema_path).read_text())
    new_props = set(new_schema["properties"])
    removed = old_props - new_props
    assert not removed, f"schema fields deleted: {sorted(removed)}. Mark them in removed_fields instead."
