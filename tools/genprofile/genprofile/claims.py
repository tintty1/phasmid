"""Which GPUs a capture's host may claim.

A capture backs its own GPU. It also backs another GPU when the fpgen extract
(profiles/data/fpgen/<os>.json) shows that GPU reporting the capture's exact
WebGL surface in clean sessions: within a host class the driver, not the GPU
model, sets the extensions, limits and rasterization. A claim copies the
captured gpu section and derives only IDENTITY_FIELDS. It needs:

- a surface with the extract's minimum sessions and renderers, and the
  minimum sessions of the claimed renderer with it;
- the capture's host class, ANGLE vendor and GL version string;
- the host driver's current table, on the capture's driver branch (NVIDIA's
  README; Mesa has no such rule, so a Mesa capture claims only its own GPU);
- no laptop part: the schema has no hybrid-graphics fields;
- a desktop device id from pci.ids that the driver lists under that name;
- when the capture has WebGPU, the capture's adapter architecture: other
  architectures' WebGPU features and limits are unmeasured.

Weights are the clean fpgen sessions with the capture's surface; the
capture's own GPU counts the capture itself as one more.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from .captures import REPO, Capture
from .fpgen_filters import parse_angle, renderer_class
from .surface import host_class, surface_sha256

DATA_DIR = REPO / "profiles" / "data" / "fpgen"
# Everything else in the gpu section is the capture's.
IDENTITY_FIELDS = ("device_id", "device_name", "gl_renderer", "webgl.unmasked_renderer", "webgl2.unmasked_renderer")


class ClaimError(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    renderer: str  # the WebGL unmasked renderer
    vendor_id: str
    device_ids: tuple[str, ...]
    weight: int
    sessions: int  # clean fpgen sessions of this renderer with the capture's surface
    records: tuple[tuple[Any, ...], ...]  # (gpu, webgl, webgl2 value ids, sessions)
    derived: bool

    @property
    def gpu_keys(self) -> tuple[str, ...]:
        return tuple(f"{self.vendor_id}:{d}" for d in self.device_ids)


@cache
def extract(family: str) -> dict[str, Any] | None:
    path = DATA_DIR / f"{family}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _parts(renderer: str) -> tuple[str, str, str]:
    parts = parse_angle(renderer)
    if parts is None:
        raise ClaimError(f"not an ANGLE renderer string: {renderer!r}")
    return parts


def _branch(driver_version: str) -> str:
    return driver_version.split(".", 1)[0]


def device_ids(entry: dict[str, Any], gpu: dict[str, Any], data: dict[str, Any]) -> tuple[str, ...]:
    """The device ids a capture may claim for one renderer of its surface; () when it may not."""
    readme = data["sources"].get("nvidia_readme")
    if (readme is None or entry.get("driver_table") != "current" or entry.get("laptop") is not False
            or _branch(readme["driver_version"]) != _branch(gpu["driver_version"])):
        return ()
    webgpu = gpu.get("webgpu") or {}
    return tuple(i for i, arch in sorted((entry.get("device_ids") or {}).items())
                 if not webgpu.get("available") or arch == webgpu.get("architecture"))


def renderers(data: dict[str, Any], gpu: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The renderers of the extract that share the capture's surface, with enough sessions."""
    t = data["thresholds"]
    sha = surface_sha256(gpu)
    own = _parts(gpu["webgl"]["unmasked_renderer"])
    for s in data["classes"].get(host_class(gpu), {}).get("surfaces", []):
        if s["sha256"] == sha and s["sessions"] >= t["k"] and s["renderers"] >= t["surface_renderers"]:
            return {r: e for r, e in s["by_renderer"].items()
                    if e["sessions"] >= t["renderer"] and renderer_class(r) == host_class(gpu)
                    and (_parts(r)[0], _parts(r)[2]) == (own[0], own[2])}
    return {}


def claims(capture: Capture) -> list[Claim]:
    """The capture's own GPU first, then every GPU its host may claim, by renderer."""
    gpu = capture.profile["gpu"]
    own = gpu["webgl"]["unmasked_renderer"]
    data = extract(capture.family)
    listed = renderers(data, gpu) if data else {}
    mine = listed.get(own, {"sessions": 0, "records": []})
    out = [Claim(own, gpu["vendor_id"], (gpu["device_id"],), mine["sessions"] + 1, mine["sessions"],
                 tuple(tuple(r) for r in mine["records"]), False)]
    for renderer, entry in sorted(listed.items()):
        ids = device_ids(entry, gpu, data) if data and renderer != own else ()
        if ids:
            out.append(Claim(renderer, gpu["vendor_id"], ids, entry["sessions"], entry["sessions"],
                             tuple(tuple(r) for r in entry["records"]), True))
    return out


def _with_device(angle: str, device: str) -> str:
    vendor, _, backend = _parts(angle)
    return f"ANGLE ({vendor}, {device}, {backend})"


def derive(gpu: dict[str, Any], claim: Claim, device_id: str) -> dict[str, Any]:
    """The captured gpu section with a derived claim's identity: its renderer string
    verbatim, the device part of the GL strings replaced, the host driver suffix kept."""
    if not claim.derived or device_id not in claim.device_ids:
        raise ClaimError(f"{device_id} is not a derived device id of {claim.renderer}")
    device = _parts(claim.renderer)[1]
    out = copy.deepcopy(gpu)
    for ctx in ("webgl", "webgl2"):
        out[ctx]["unmasked_renderer"] = claim.renderer
    out["gl_renderer"] = _with_device(gpu["gl_renderer"], device)
    out["device_name"] = _with_device(gpu["device_name"], device)
    out["device_id"] = device_id
    return out


def note(capture: Capture, claim: Claim, device_id: str) -> str:
    """The provenance of a derived identity, for captured.note."""
    data = extract(capture.family)
    if data is None:
        raise ClaimError(f"no fpgen extract for os={capture.family}")
    src, model = data["sources"], data["model"]
    ids = ", ".join(f"{'/'.join(r[:3])} x{r[3]}" for r in claim.records)
    return (
        f"GPU identity {_parts(claim.renderer)[1]} ({device_id}) derived within host class "
        f"{host_class(capture.profile['gpu'])}: fpgen {model['tag']} "
        f"(archive sha256 {model['archive_sha256']}) has {claim.sessions} clean sessions of it with the "
        f"capture's WebGL surface (value ids {ids}); device id from pci.ids sha256 {src['pci_ids']['sha256']} "
        f"and the NVIDIA {src['nvidia_readme']['driver_version']} README sha256 {src['nvidia_readme']['sha256']}"
    )
