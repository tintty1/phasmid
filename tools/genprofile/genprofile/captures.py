"""The capture catalog: real-device dumps under profiles/data/captures/.

Each dump is mapped onto the schema with the harness's own
``tests/consistency/capture.py``, the same code ``consistency capture`` runs,
so there is one mapping from a dump to profile fields. The values a page
cannot read (marketing OS version, GPU ids from chrome://gpu, RAM, disk) were
given on the ``consistency capture`` command line; they are read back from the
profile that recorded that capture, ``profiles/examples/<stem>.json``, whose
``captured.dump_sha256`` must name the dump.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[3]
CAPTURES_DIR = REPO / "profiles" / "data" / "captures"
RECORDS_DIR = REPO / "profiles" / "examples"
# Family-name measurements of a capture host (fonts/family-names guard
# references): <record stem>.<browser>-family-names.json, ``resolved`` being
# every name that resolves as a CSS family in stock Chrome on that machine.
FAMILY_NAMES_DIR = REPO / "profiles" / "data" / "fonts" / "metrics"
HARNESS_CAPTURE = REPO / "tests" / "consistency" / "capture.py"

_GPU_ID_KEYS = ("vendor_id", "device_id", "vendor_name", "device_name", "driver_vendor", "driver_version",
                "gl_vendor", "gl_renderer", "gl_version")
# Sections the harness maps from the dump that the record must agree with.
_CHECKED_SECTIONS = ("gpu", "audio", "fonts", "speech", "media", "webauthn", "capabilities", "system_colors")


class CaptureError(Exception):
    pass


@dataclass(frozen=True)
class Capture:
    name: str
    family: str
    gpu: str
    date: str
    dump_sha256: str
    profile: dict[str, Any]
    battery: dict[str, Any] | None
    # media_devices.labels of the record: a dump has no capture permission, so
    # it shows no labels; the record lists what the host shows after it.
    media_device_labels: dict[str, list[str]]


@cache
def harness() -> ModuleType:
    """tests/consistency/capture.py, loaded by path as part of its package
    (it imports gpu_identity beside it): it needs only jsonschema."""
    package = HARNESS_CAPTURE.parent
    spec = importlib.util.spec_from_file_location("phasmid_harness", package / "__init__.py",
                                                  submodule_search_locations=[str(package)])
    if spec is None or spec.loader is None:
        raise CaptureError(f"cannot load {HARNESS_CAPTURE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{spec.name}.{HARNESS_CAPTURE.stem}")


def gpu_key(gpu: dict[str, Any]) -> str:
    return f"{gpu['vendor_id']}:{gpu['device_id']}"


def _load(path: Path) -> Capture:
    dump = json.loads(path.read_text())
    stem = path.name.split(".", 1)[0]
    record_path = RECORDS_DIR / f"{stem}.json"
    if not record_path.exists():
        raise CaptureError(f"{path.name}: no capture record {record_path.relative_to(REPO)}")
    record = json.loads(record_path.read_text())
    digest = hashlib.sha256(json.dumps(dump, sort_keys=True).encode()).hexdigest()
    if record.get("captured", {}).get("dump_sha256") != digest:
        raise CaptureError(f"{record_path.relative_to(REPO)}: captured.dump_sha256 does not name {path.name}")
    hardware = record["hardware"]
    profile = harness().capture(
        dump,
        profile_id=stem,
        os_version=record["os"]["version"],
        gpu_ids={k: record["gpu"][k] for k in _GPU_ID_KEYS if record["gpu"].get(k)},
        seed=record["seed"],
        physical_memory_mb=hardware.get("physical_memory_mb"),
        disk_total_bytes=hardware.get("disk_total_bytes"),
    )
    # A record may narrow fonts.families to part of what the capture host
    # resolved (the host can hold another OS's fonts), never add to it; the
    # narrowed list is the one generated profiles get. What the host resolved
    # is the dump's probe list plus, where the host was measured name by name,
    # that measurement: the dump probes a fixed list of common families.
    families = record.get("fonts", {}).get("families", [])
    resolved = set(profile["fonts"]["families"])
    for measured in sorted(FAMILY_NAMES_DIR.glob(f"{stem}.*-family-names.json")):
        resolved |= set(json.loads(measured.read_text())["resolved"])
    if not set(families) <= resolved:
        raise CaptureError(f"{record_path.relative_to(REPO)}: fonts.families lists families {path.name} does not resolve")
    profile["fonts"]["families"] = families
    for section in _CHECKED_SECTIONS:
        if profile.get(section) != record.get(section):
            raise CaptureError(f"{record_path.relative_to(REPO)}: {section} differs from what {path.name} maps to")
    return Capture(
        name=path.name,
        family=profile["os"]["family"],
        gpu=gpu_key(profile["gpu"]),
        date=dump["harness"]["started"][:10],
        dump_sha256=digest,
        profile=profile,
        battery=dump["contexts"]["main"]["navigator"].get("battery"),
        media_device_labels=record["media_devices"].get("labels", {}),
    )


@cache
def catalog() -> tuple[Capture, ...]:
    return tuple(_load(p) for p in sorted(CAPTURES_DIR.glob("*.json")) if _is_dump(p))


def _is_dump(path: Path) -> bool:
    """A harness dump, not a supplementary capture of the same host (the
    permission-granted media device list a record's labels come from)."""
    return "harness" in json.loads(path.read_text())


def for_os(family: str) -> list[Capture]:
    return [c for c in catalog() if c.family == family]


def validate(profile: dict[str, Any]) -> list[str]:
    """The schema check ``consistency validate`` runs."""
    return harness().validate(profile)
