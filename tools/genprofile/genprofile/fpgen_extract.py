"""profiles/data/fpgen/<os>.json: what the pinned fpgen model says about each host class.

    python -m genprofile.fpgen_extract --os linux --nvidia-readme /usr/share/doc/nvidia/README \\
        --dawn-gpu-info <chromium>/src/third_party/dawn/src/dawn/gpu_info.json \\
        -o ../../profiles/data/fpgen/linux.json

A maintainer command: run it again when the pin, a capture or the host's
driver changes. It reads the cached model (python -m genprofile.fpgen_model),
the host driver's README (Appendix A: which GPUs the installed driver
supports), pci.ids (device ids) and Dawn's gpu_info.json (the WebGPU
architecture a device id reports). The generator and its tests read only the
committed output.

The model data has no licence and its records are real visitors'
fingerprints, so the output holds only what many sessions share: renderer
strings with session counts and value ids, sha256 hashes of WebGL surfaces, and the modes of the
values a capture is checked against. Never a record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fpgen_filters as f
from .captures import REPO, catalog
from .fpgen_model import Model, ModelError, model, pin, support
from .generate import CONVENTIONS
from .surface import host_class, surface_sha256

DATA_DIR = REPO / "profiles" / "data" / "fpgen"
K = 3  # sessions behind anything committed
K_SURFACE_RENDERERS = 2  # a surface seen on one renderer only may be that GPU's, not the driver's
K_RENDERER = 2  # sessions of a renderer with the surface before it may be claimed

_SHADERS = {35633: "VERTEX_SHADER", 35632: "FRAGMENT_SHADER"}
_PRECISIONS = {36336: "LOW_FLOAT", 36337: "MEDIUM_FLOAT", 36338: "HIGH_FLOAT",
               36339: "LOW_INT", 36340: "MEDIUM_INT", 36341: "HIGH_INT"}
_LOCALE_CHAIN = ["location.country", "location.region", "intl", "languages", "voiceLocale", "navigator.languages",
                 "voices"]
_POINTER_CHAIN = ["matchMedia.mediaCss.pointer", "matchMedia.mediaCss.any-pointer", "matchMedia.mediaCss.hover",
                  "matchMedia.mediaCss.any-hover"]
_MEDIA_CHAIN = ["drm", "encryptedMediaCapabilities", "mediaDecoderSupport", "mimeTypes", "windowFeatures"]
# The canPlayType strings tests/consistency/capture.py maps to media.hevc and media.dolby_audio;
# fpgen probes AC-3 as audio/mp4 only.
_HEVC_MIME = 'video/mp4; codecs="hev1.1.6.L93.B0"'
_AC3_MIME = 'audio/mp4; codecs="ac-3"'
_DECODE_CODECS = {"h264": ("avc1",), "hevc": ("hev1", "hvc1"), "vp9": ("vp09", "vp9"), "av1": ("av01",)}
_FONT = re.compile(r"(\d+(?:\.\d+)?)px (.+)")
_LAPTOP = ("Laptop", "Max-Q", "Mobile")
_NVIDIA_SUFFIX = "/PCIe/SSE2"
_README_ROW = re.compile(r" (\S.*?)\s{2,}([0-9A-F]{4})((?: [0-9A-F]{4}){2})?(?:\s+\S+)?\s*")
_PCI_DEVICE = re.compile(r"\t([0-9a-f]{4})  (\S+) \[(.+)\]")


@dataclass(frozen=True)
class Record:
    """One (gpu, webgl, webgl2) path of the model: ``sessions`` identical observations."""
    ids: tuple[str, str, str]
    sessions: int
    renderer: str
    webgl: dict[str, Any] | None
    webgl2: dict[str, Any] | None

    @property
    def gpu(self) -> dict[str, Any]:
        return {"webgl": self.webgl, "webgl2": self.webgl2}


def schema_webgl(blob: Any) -> dict[str, Any] | None:
    """An fpgen WebGL blob in the shape of the schema's webgl_capture, or None when it is not a context."""
    if not isinstance(blob, dict) or not isinstance(blob.get("params"), dict):
        return None
    params = {p["name"]: p.get("value") for p in blob["params"].values() if isinstance(p, dict) and "name" in p}
    extensions = blob.get("extensions")
    constants = extensions.values() if isinstance(extensions, dict) else []
    draw_buffers = next((c.get("parameter", {}).get("value") for c in constants
                         if isinstance(c, dict) and c.get("constantName") == "MAX_DRAW_BUFFERS_WEBGL"), None)
    precision = {}
    for fmt in blob.get("shaderPrecisionFormats") or []:
        shader, prec = _SHADERS.get(fmt.get("shaderType")), _PRECISIONS.get(fmt.get("precisionType"))
        if shader and prec:
            precision[f"{shader}.{prec}"] = fmt.get("shaderPrecisionFormat")
    return {
        "extensions": list(blob.get("supportedExtensions") or []),
        "params": params,
        "precision": precision,
        "context_attributes": blob.get("contextAttributes") or {},
        "max_anisotropy": blob.get("maxAnisotropy"),
        "max_draw_buffers": draw_buffers,
    }


def voices_sha256(voices: list[dict[str, Any]]) -> str:
    """Over schema-shaped speech.voices, in getVoices() order."""
    return hashlib.sha256(json.dumps(voices, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _value_id(m: Model, node: str, known: dict[str, str], value: Any) -> str:
    ids = [v for v in m.dist(node, known) if m.val(v) == value]
    if len(ids) != 1:
        raise ModelError(f"{node}: {len(ids)} values equal {value!r}")
    return ids[0]


def base(m: Model, family: str) -> dict[str, str]:
    """The parent ids of Chrome with the OS's UA token."""
    chrome = _value_id(m, "browser", {}, "Chrome")
    return {"browser": chrome,
            "platformInfo": _value_id(m, "platformInfo", {"browser": chrome}, CONVENTIONS[family].ua_platform_token)}


def _chain(m: Model, names: list[str], known: dict[str, str], exact: bool = True) -> list[tuple[dict[str, str], float]]:
    """Every observed assignment of a chain rooted under ``known``, with its session count;
    ``exact=False`` for chains whose child tables are not normalized over their parent's sessions."""
    n = m.sessions(names[0], known)
    return [(a, support(c) if exact else c) for a, c in m.enum(names, known, n)]


def gpu_records(m: Model, known: dict[str, str]) -> list[Record]:
    n = m.sessions("gpu", known)
    records = []
    for gid, p in sorted(m.dist("gpu", known).items()):
        gpu = m.val(gid)
        renderer = gpu.get("renderer") if isinstance(gpu, dict) else None
        for a, count in m.enum(["webgl", "webgl2"], {**known, "gpu": gid}, n * p):
            records.append(Record((gid, a["webgl"], a["webgl2"]), support(count),
                                  renderer if isinstance(renderer, str) else "",
                                  schema_webgl(m.val(a["webgl"])), schema_webgl(m.val(a["webgl2"]))))
    return records


_FILTERS: tuple[tuple[str, Callable[[Record], bool]], ...] = (
    ("software renderer", lambda r: f.is_software(r.renderer)),
    ("Brave renderer string", lambda r: f.brave_renderer(r.renderer)),
    ("not a Linux GPU renderer", lambda r: f.renderer_class(r.renderer) is None),
    ("incomplete WebGL record", lambda r: r.webgl is None or r.webgl2 is None),
    ("Brave identity checks on WebGL2", lambda r: bool(f.brave_identity(r.webgl2["params"]))),  # type: ignore[index]
    ("WebGL draft extensions", lambda r: bool(f.draft_extensions(r.webgl["extensions"] + r.webgl2["extensions"]))),  # type: ignore[index]
    ("unknown extension names", lambda r: bool(f.unknown_extensions(r.webgl["extensions"] + r.webgl2["extensions"]))),  # type: ignore[index]
)


def clean(records: list[Record]) -> tuple[list[Record], list[dict[str, Any]]]:
    """The records every filter keeps, and how many sessions each filter removed, in order."""
    log = []
    for name, drop in _FILTERS:
        log.append({"filter": name, "sessions": sum(r.sessions for r in records if drop(r))})
        records = [r for r in records if not drop(r)]
    by_class: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_class[f.renderer_class(r.renderer)].append(r)  # type: ignore[index]
    farbled = set()
    for family in by_class.values():
        flags = f.brave_mode_minus_one([(r.webgl2["params"], r.sessions) for r in family], K)  # type: ignore[index]
        farbled |= {r.ids for r, flag in zip(family, flags) if flag}
    log.append({"filter": "Brave mode minus one", "sessions": sum(r.sessions for r in records if r.ids in farbled)})
    return [r for r in records if r.ids not in farbled], log


def readme_tables(text: str) -> tuple[str, dict[str, list[tuple[str, str | None]]]]:
    """The driver version and, per product name, the Appendix A tables listing it:
    (table, device id) for rows without a subsystem id, (table, None) for board-specific rows."""
    version = re.search(r"Most Recent Driver Version: (\S+)", text)
    if version is None:
        raise ModelError("the NVIDIA README names no driver version")
    tables: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    table = None
    for line in text.splitlines():
        if "A1. CURRENT NVIDIA GPUS" in line:
            table = "current"
        elif m := re.search(r"The (\d+\.xx) driver supports the following set of GPUs", line):
            table = m[1]
        elif line.startswith("Appendix B"):
            table = None
        elif table and (row := _README_ROW.fullmatch(line)):
            tables[row[1]].append((table, None if row[3] else f"0x{row[2].lower()}"))
    if not any(t == "current" for rows in tables.values() for t, _ in rows):
        raise ModelError("the NVIDIA README has no current GPU table")
    return version[1], tables


def pci_desktop_ids(text: str, vendor: str) -> dict[str, list[str]]:
    """Desktop device ids per marketing name under one pci.ids vendor: no mobile chips
    (codename ending in M), no Mobile, Max-Q or Laptop names."""
    ids: dict[str, list[str]] = defaultdict(list)
    inside = False
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if not line.startswith("\t"):
            inside = line.startswith(f"{vendor}  ")
        elif inside and (m := _PCI_DEVICE.fullmatch(line)):
            if not m[2].endswith("M") and not any(w in m[3] for w in _LAPTOP):
                ids[m[3]].append(f"0x{m[1]}")
    return ids


def dawn_architecture(gpu_info: dict[str, Any], vendor_id: str, device_id: str) -> str | None:
    """The adapter architecture Dawn reports for a PCI id (src/dawn/gpu_info.json)."""
    for vendor in gpu_info["vendors"].values():
        if not isinstance(vendor, dict) or str(vendor.get("id", "")).lower() != vendor_id:
            continue
        for group in vendor.get("devices", []):
            mask = int(group["mask"], 16)
            for name, prefixes in group["architecture"].items():
                if any(int(device_id, 16) & mask == int(p, 16) for p in prefixes):
                    return name.lower()
    return None


@dataclass(frozen=True)
class Sources:
    readme_version: str
    readme_tables: dict[str, list[tuple[str, str | None]]]
    pci_ids: dict[str, list[str]]
    gpu_info: dict[str, Any]


def _nvidia(renderer: str, src: Sources) -> dict[str, Any]:
    """Driver support, laptop flag and device ids of a proprietary-driver renderer string."""
    product = f.parse_angle(renderer)[1].removesuffix(_NVIDIA_SUFFIX)  # type: ignore[index]
    rows = src.readme_tables.get(product, [])
    tables = [t for t, _ in rows]
    table = "current" if "current" in tables else (tables[0] if tables else None)
    pci = {i for name, ids in src.pci_ids.items() if product in (name, f"NVIDIA {name}") for i in ids}
    current = {i for t, i in rows if t == "current" and i}
    return {
        "driver_table": table,
        "laptop": any(w in product for w in _LAPTOP),
        "device_ids": {i: dawn_architecture(src.gpu_info, "0x10de", i) for i in sorted(pci & current)},
    }


def classes(clean_records: list[Record], src: Sources, family: str) -> dict[str, Any]:
    captured = defaultdict(list)
    for c in catalog():
        if c.family == family:
            captured[(host_class(c.profile["gpu"]), surface_sha256(c.profile["gpu"]))].append(c.name.split(".", 1)[0])
    grouped: dict[str, dict[str, list[Record]]] = defaultdict(lambda: defaultdict(list))
    for r in clean_records:
        grouped[f.renderer_class(r.renderer)][surface_sha256(r.gpu)].append(r)  # type: ignore[index]
    out = {}
    for cls, surfaces in sorted(grouped.items()):
        entries = []
        for sha, records in surfaces.items():
            sessions = sum(r.sessions for r in records)
            by_renderer: dict[str, list[Record]] = defaultdict(list)
            for r in records:
                by_renderer[r.renderer].append(r)
            if sessions < K or len(by_renderer) < K_SURFACE_RENDERERS:
                continue
            listed = {}
            for renderer, rs in sorted(by_renderer.items()):
                n = sum(r.sessions for r in rs)
                if n < K_RENDERER:
                    continue
                listed[renderer] = {"sessions": n, "records": [[*r.ids, r.sessions] for r in rs]}
                if cls.startswith("nvidia/opengl/"):
                    listed[renderer].update(_nvidia(renderer, src))
            entries.append({"sha256": sha, "sessions": sessions, "renderers": len(by_renderer),
                            "equals_capture": sorted(captured.get((cls, sha), [])), "by_renderer": listed})
        out[cls] = {
            "sessions": sum(r.sessions for rs in surfaces.values() for r in rs),
            "renderers": len({r.renderer for rs in surfaces.values() for r in rs}),
            "surfaces": sorted(entries, key=lambda e: (-e["sessions"], e["sha256"])),
        }
    return out


def _mode(counts: Counter[str], values: dict[str, Any], exact: bool = True) -> dict[str, Any] | None:
    """The most frequent value with its support, or None below K."""
    if not counts:
        return None
    key, n = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if n < K:
        return None
    total = sum(counts.values())
    return {"mode": values[key], "sessions": n if exact else round(n, 2), "of": total if exact else round(total, 2)}


class _Tally:
    def __init__(self) -> None:
        self.counts: dict[str, Counter[str]] = defaultdict(Counter)
        self.values: dict[str, Any] = {}

    def add(self, check: str, value: Any, n: float) -> None:
        key = json.dumps(value, sort_keys=True)
        self.counts[check][key] += n
        self.values[key] = value

    def modes(self, exact: bool) -> dict[str, Any]:
        out = {}
        for check, counts in sorted(self.counts.items()):
            if (mode := _mode(counts, self.values, exact)) is not None:
                out[check] = mode
        return out


def _pointer(primary: str, any_pointer: str, hover: str, any_hover: str) -> dict[str, Any]:
    return {"primary": primary, "hover": hover, "any_pointer": [] if any_pointer == "none" else [any_pointer],
            "any_hover": [] if any_hover == "none" else [any_hover]}


def _ui_font(styles: Any) -> dict[str, Any] | None:
    fonts = {v for s in styles or [] if isinstance(s, dict) for v in s.values()}
    m = _FONT.fullmatch(fonts.pop()) if len(fonts) == 1 else None
    return {"family": m[2], "size_px": float(m[1])} if m else None


def _schema_voices(voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"default": v.get("default"), "lang": v.get("lang"), "local_service": v.get("localService"),
             "name": v.get("name"), "voice_uri": v.get("voiceURI")} for v in voices]


def browser_checks(m: Model, known: dict[str, str], family: str) -> dict[str, Any]:
    """Modes of the subtrees under the UA token alone: touch, pointer, voices, UI font, window chrome.
    They cannot be linked to the GPU path, so Brave is removed only where it leaves a mark."""
    t = _Tally()
    for a, c in _chain(m, ["navigator.userAgent", "navigator.maxTouchPoints"], known):
        t.add("max_touch_points", m.val(a["navigator.maxTouchPoints"]), c)
    for a, c in _chain(m, _POINTER_CHAIN, known):
        t.add("pointer", _pointer(*(m.val(a[x]) for x in _POINTER_CHAIN)), c)
    for a, c in _chain(m, _LOCALE_CHAIN, known):
        voices = m.val(a["voices"])
        if f.google_voices(voices):
            t.add("voices_sha256", voices_sha256(_schema_voices(voices)), c)
    for a, c in _chain(m, ["baseFonts", "systemStyles.fonts"], known):
        t.add("ui_font", _ui_font(m.val(a["systemStyles.fonts"])), c)
    deltas: Counter[tuple[float, int, int]] = Counter()
    # The window table under a screen is not normalized over that screen's sessions (the
    # screen table has 143 of the 153, the chain sums to 142 in fractions), so these counts are inexact.
    for a, c in _chain(m, ["screen", "matchMedia.mediaCss.orientation", "window"], known, exact=False):
        w = m.val(a["window"])
        if not f.zoomed(w.get("devicePixelRatio"), CONVENTIONS[family].dpr_steps):
            deltas[(w["devicePixelRatio"], w["outerHeight"] - w["innerHeight"], w["outerWidth"] - w["innerWidth"])] += c
    out = t.modes(exact=True)
    # Outer minus inner size depends on the browser chrome the user shows (bookmark bar,
    # fullscreen), so every delta with support is recorded, not only the mode.
    out["window_deltas"] = [{"dpr": dpr, "height": h, "width": w, "sessions": round(s, 2)}
                            for (dpr, h, w), s in sorted(deltas.items()) if s >= K]
    return out


def _play(mimes: dict[str, Any], mime: str) -> Any:
    entry = mimes.get(mime)
    return entry.get("videoPlayType") if isinstance(entry, dict) else None


def _hardware_decode(mimes: dict[str, Any]) -> dict[str, bool]:
    def efficient(prefixes: tuple[str, ...]) -> bool:
        return any(cap.get("powerEfficient") is True
                   for mime, entry in mimes.items() if mime.startswith("video/") and any(f'"{p}' in mime for p in prefixes)
                   for cap in entry.get("mediaCapabilities") or []
                   if cap.get("configuration", {}).get("type") == "file" and "video" in cap.get("configuration", {}))
    return {codec: efficient(prefixes) for codec, prefixes in _DECODE_CODECS.items()}


def stack_checks(m: Model, known: dict[str, str], clean_records: list[Record]) -> dict[str, Any]:
    """Modes of the GPU path's sibling subtrees (DRM, codecs, window features) per driver stack.
    Siblings share only the renderer string with the clean WebGL records, so each string is
    weighted by its clean sessions; that is exact for strings seen once. Widevine is checked
    first; the other modes are over Widevine sessions, because Google Chrome on Linux x64
    always has the CDM."""
    weights: dict[str, float] = defaultdict(float)
    for r in clean_records:
        weights[r.ids[0]] += r.sessions
    tallies: dict[str, _Tally] = defaultdict(_Tally)
    for gid, weight in sorted(weights.items()):
        t = tallies[f.renderer_class(m.val(gid)["renderer"]).split("/")[0]]  # type: ignore[union-attr]
        for a, c in m.enum(_MEDIA_CHAIN, {**known, "gpu": gid}, weight):
            drm = m.val(a["drm"])
            t.add("widevine", f.widevine(drm), c)
            if not f.widevine(drm):
                continue
            listed, features = m.val(a["mimeTypes"]), m.val(a["windowFeatures"])
            mimes = {e["mimeType"]: e for e in listed if isinstance(e, dict) and "mimeType" in e} if isinstance(listed, list) else {}
            features = features if isinstance(features, dict) else {}
            t.add("hevc", _play(mimes, _HEVC_MIME) not in ("", None), c)
            t.add("dolby_audio", _play(mimes, _AC3_MIME) not in ("", None), c)
            t.add("hardware_decode", _hardware_decode(mimes), c)
            t.add("capabilities", {"bluetooth": features.get("bluetooth"), "web_share": features.get("share")}, c)
    return {stack: t.modes(exact=False) for stack, t in sorted(tallies.items())}


def extract(m: Model, family: str, src: Sources, sources: dict[str, Any]) -> dict[str, Any]:
    known = base(m, family)
    records = gpu_records(m, known)
    clean_records, log = clean(records)
    p = pin()
    return {
        "model": {"tag": p.tag, "archive_sha256": p.sha256},
        "base": {"browser": "Chrome", "ua_platform_token": CONVENTIONS[family].ua_platform_token,
                 "gpu_sessions": sum(r.sessions for r in records),
                 "clean_gpu_sessions": sum(r.sessions for r in clean_records)},
        "filters": log,
        "thresholds": {"k": K, "surface_renderers": K_SURFACE_RENDERERS, "renderer": K_RENDERER},
        "sources": sources,
        "classes": classes(clean_records, src, family),
        "checks": {"browser": browser_checks(m, known, family), "stacks": stack_checks(m, known, clean_records)},
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="genprofile.fpgen_extract", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--os", required=True, choices=["linux"])
    ap.add_argument("--nvidia-readme", required=True, type=Path, help="the installed driver's README")
    ap.add_argument("--pci-ids", default=Path("/usr/share/hwdata/pci.ids"), type=Path)
    ap.add_argument("--dawn-gpu-info", required=True, type=Path, help="src/dawn/gpu_info.json of the Chromium checkout")
    ap.add_argument("-o", "--out", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        readme = args.nvidia_readme.read_text()
        version, tables = readme_tables(readme)
        pci_text = args.pci_ids.read_text(errors="replace")
        pci_version = re.search(r"#\s+Version: (\S+)", pci_text)
        src = Sources(version, tables, pci_desktop_ids(pci_text, "10de"), json.loads(args.dawn_gpu_info.read_text()))
        sources = {
            "nvidia_readme": {"driver_version": version, "sha256": _sha256(args.nvidia_readme)},
            "pci_ids": {"version": pci_version[1] if pci_version else None, "sha256": _sha256(args.pci_ids)},
            "dawn_gpu_info": {"sha256": _sha256(args.dawn_gpu_info)},
        }
        data = extract(model(), args.os, src, sources)
    except (ModelError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {args.out}: {data['base']['clean_gpu_sessions']} of {data['base']['gpu_sessions']} GPU sessions clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
