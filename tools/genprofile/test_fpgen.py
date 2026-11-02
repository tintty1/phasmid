"""Tests for the fpgen reader and filters. No network and no real model: a synthetic model is built in tmp_path."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
import zstandard

from genprofile import fpgen_filters
from genprofile.captures import REPO, catalog, for_os
from genprofile.fpgen_extract import (DATA_DIR, K, K_RENDERER, K_SURFACE_RENDERERS, dawn_architecture,
                                      pci_desktop_ids, readme_tables, schema_webgl, voices_sha256)
from genprofile.fpgen_model import Model, ModelError, Pin, fetch, install, pin, support, verify
from genprofile.surface import PROBE_PARAMS, host_class, surface_sha256
from test_genprofile import VERSION_STRING


def _vid(i: int) -> str:
    return base64.b85encode(i.to_bytes(4, "big")).decode()


def _synthetic(tmp_path: Path) -> tuple[Path, Pin]:
    """Three nodes, 10 sessions: browser -> platform -> gpu, every count integral."""
    values = ["Chrome", "Linux", "Windows", {"renderer": "A"}, {"renderer": "B"}]
    dat, index = b"", {}
    for v in values:
        blob = json.dumps(v).encode()
        index[f"{len(dat):x}"] = len(blob)
        dat += blob
    chrome, linux, windows, a, b = (_vid(i) for i in range(len(values)))
    network = {"nodes": [
        {"name": "browser", "parentNames": [], "possibleValues": [chrome], "conditionalProbabilities": {chrome: 1.0}},
        {"name": "platform", "parentNames": ["browser"], "possibleValues": [linux, windows],
         "conditionalProbabilities": {chrome: {linux: 0.4, windows: 0.6}}},
        {"name": "gpu", "parentNames": ["browser", "platform"], "possibleValues": [a, b],
         "conditionalProbabilities": {chrome: {linux: {a: 0.75, b: 0.25}, windows: {a: 1.0}}}},
    ]}
    raw = {"fingerprint-network.json": json.dumps(network).encode(), "values.json": json.dumps(index).encode(),
           "values.dat": dat}
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as z:
        for name, data in raw.items():
            z.writestr(f"{name}.zst", zstandard.ZstdCompressor().compress(data))
    archive = tmp_path / "model-release.zip"
    archive.write_bytes(zipped.getvalue())
    with zipfile.ZipFile(archive) as z:
        files = {name: (hashlib.sha256(z.read(f"{name}.zst")).hexdigest(), hashlib.sha256(data).hexdigest())
                 for name, data in raw.items()}
    return archive, Pin("test", "https://invalid.example/model.zip", archive.stat().st_size,
                        hashlib.sha256(archive.read_bytes()).hexdigest(), files)


def test_synthetic_model_reads_back_integral_counts(tmp_path):
    archive, pin = _synthetic(tmp_path)
    m = Model(install(archive, pin, tmp_path / "cache"))
    chrome = next(iter(m.dist("browser", {})))
    linux = next(v for v in m.dist("platform", {"browser": chrome}) if m.val(v) == "Linux")
    rows = sorted((m.val(a["gpu"])["renderer"], support(n))
                  for a, n in m.enum(["gpu"], {"browser": chrome, "platform": linux}, 4))
    assert rows == [("A", 3), ("B", 1)]
    assert sum(support(n) for _, n in m.enum(["platform", "gpu"], {"browser": chrome}, 10)) == 10


def test_fetch_uses_the_cache_and_never_the_network_once_installed(tmp_path):
    archive, pin = _synthetic(tmp_path)
    dest = fetch(pin, tmp_path / "cache", archive)
    assert dest == tmp_path / "cache" / pin.sha256
    assert fetch(pin, tmp_path / "cache") == dest  # the pin's URL is unreachable


def test_a_flipped_byte_in_the_archive_is_refused(tmp_path):
    archive, pin = _synthetic(tmp_path)
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 1
    archive.write_bytes(bytes(data))
    with pytest.raises(ModelError):
        install(archive, pin, tmp_path / "cache")
    assert not any((tmp_path / "cache").glob("*"))


def test_a_flipped_byte_in_the_cache_is_refused(tmp_path):
    archive, pin = _synthetic(tmp_path)
    dest = install(archive, pin, tmp_path / "cache")
    path = dest / "values.dat"
    data = bytearray(path.read_bytes())
    data[0] ^= 1
    path.write_bytes(bytes(data))
    with pytest.raises(ModelError):
        verify(dest, pin)
    with pytest.raises(ModelError):
        fetch(pin, tmp_path / "cache")


def test_session_counts_are_recovered_per_table(tmp_path):
    archive, pin = _synthetic(tmp_path)
    m = Model(install(archive, pin, tmp_path / "cache"))
    chrome = next(iter(m.dist("browser", {})))
    assert m.sessions("platform", {"browser": chrome}) == 5  # 0.4 and 0.6 are first whole at 5
    assert m.sessions("browser", {}) == 1


def test_a_fractional_count_is_refused():
    assert support(2.9999999999) == 3
    with pytest.raises(ModelError):
        support(2.5)


def _capture_gpu() -> dict:
    return for_os("linux")[0].profile["gpu"]


def test_the_capture_is_a_clean_session():
    gpu = _capture_gpu()
    assert fpgen_filters.renderer_class(gpu["gl_renderer"]) == "nvidia/opengl/gles"
    assert fpgen_filters.renderer_class(gpu["webgl"]["unmasked_renderer"]) == "nvidia/opengl/gles"
    assert fpgen_filters.brave_identity(gpu["webgl2"]["params"]) == []
    for ctx in ("webgl", "webgl2"):
        assert fpgen_filters.draft_extensions(gpu[ctx]["extensions"]) == []
        assert fpgen_filters.unknown_extensions(gpu[ctx]["extensions"]) == []
    assert not fpgen_filters.is_software(gpu["gl_renderer"])


@pytest.mark.parametrize("renderer, expected", [
    ("ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL 4.5.0)", "nvidia/opengl/gl"),
    ("ANGLE (NVIDIA, Vulkan 1.3.277 (NVIDIA Tesla P6 (0x00001BB4)), NVIDIA)", "nvidia/vulkan"),
    ("ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL ES 3.2)", "mesa-intel/opengl/gles"),
    ("ANGLE (AMD, AMD Radeon Graphics (radeonsi renoir ACO), OpenGL 4.6)", "mesa-amd/opengl/gl"),
    ("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)", None),
    ("Adreno (TM) 640", None),
    ("Brave", None),
])
def test_renderer_classes(renderer, expected):
    assert fpgen_filters.renderer_class(renderer) == expected


@pytest.mark.parametrize("name", ["MAX_VERTEX_UNIFORM_COMPONENTS", "MAX_FRAGMENT_INPUT_COMPONENTS"])
def test_one_farbled_limit_breaks_an_identity(name):
    params = dict(_capture_gpu()["webgl2"]["params"])
    params[name] -= 1
    assert fpgen_filters.brave_identity(params) == [name]


def test_mode_minus_one_flags_only_the_farbled_record():
    base = dict(_capture_gpu()["webgl2"]["params"])
    lowered = {**base, "MAX_VERTEX_UNIFORM_BLOCKS": base["MAX_VERTEX_UNIFORM_BLOCKS"] - 1}
    raised = {**base, "MAX_VERTEX_UNIFORM_BLOCKS": base["MAX_VERTEX_UNIFORM_BLOCKS"] + 1}
    family = [(base, 3), (lowered, 1), (raised, 1)]
    assert fpgen_filters.brave_mode_minus_one(family, 3) == [False, True, False]
    assert fpgen_filters.brave_mode_minus_one(family[1:], 3) == [False, False]  # below the family size


def test_probe_params_are_the_probes():
    probe = (REPO / "tests" / "consistency" / "web" / "probe.js").read_text()
    listed = re.findall(r'"([A-Z0-9_]+)"', re.search(r"const GL_PARAMS = \[(.*?)\];", probe, re.S)[1])
    assert tuple(n for n in listed if n not in ("VENDOR", "RENDERER", "VERSION", "SHADING_LANGUAGE_VERSION")) == PROBE_PARAMS


def test_the_captures_host_class():
    assert host_class(_capture_gpu()) == "nvidia/opengl/gles"
    assert host_class({**_capture_gpu(), "gl_vendor": "Google Inc. (Intel)"}) is None


def test_the_surface_ignores_identity_and_build_owned_fields():
    gpu = copy.deepcopy(_capture_gpu())
    before = surface_sha256(gpu)
    for ctx in ("webgl", "webgl2"):
        for key in ("unmasked_renderer", "unmasked_vendor", "renderer", "vendor", "version", "shading_language_version"):
            gpu[ctx][key] = "changed"
        gpu[ctx]["context_attributes"]["preserveDrawingBuffer"] = not gpu[ctx]["context_attributes"]["preserveDrawingBuffer"]
        gpu[ctx]["extensions"].reverse()
    gpu["webgl2"]["params"]["MAX_ELEMENT_INDEX"] -= 1
    gpu["device_id"], gpu["gl_renderer"], gpu["webgpu"] = "0x0000", "changed", {}
    assert surface_sha256(gpu) == before


def _mutations():
    """(label, edit of one context) for every hardware-owned field of the capture."""
    for ctx, c in ((ctx, _capture_gpu()[ctx]) for ctx in ("webgl", "webgl2")):
        yield ctx, "drops an extension", lambda c: c["extensions"].pop()
        yield ctx, "adds an extension", lambda c: c["extensions"].append("WEBGL_draw_buffers")
        for name in c["params"].keys() - {"MAX_ELEMENT_INDEX"}:
            yield ctx, name, lambda c, name=name: c["params"].__setitem__(name, [c["params"][name], 1])
        for name in c["precision"]:
            yield ctx, name, lambda c, name=name: c["precision"][name].__setitem__("precision", c["precision"][name]["precision"] + 1)
        for name in c["context_attributes"].keys() - {"preserveDrawingBuffer"}:
            yield ctx, name, lambda c, name=name: c["context_attributes"].__setitem__(name, f"{c['context_attributes'][name]}!")
        yield ctx, "max_anisotropy", lambda c: c.__setitem__("max_anisotropy", 8)
        yield ctx, "max_draw_buffers", lambda c: c.__setitem__("max_draw_buffers", 8)


def test_the_surface_changes_on_every_hardware_owned_field():
    before = surface_sha256(_capture_gpu())
    for ctx, label, mutate in _mutations():
        gpu = copy.deepcopy(_capture_gpu())
        mutate(gpu[ctx])
        assert surface_sha256(gpu) != before, f"{ctx} {label}"


# The committed extract. Re-extraction needs the cached model and the host driver's README,
# so it is the maintainer command in genprofile/fpgen_extract.py, not a test.
EXTRACT = json.loads((DATA_DIR / "linux.json").read_text())


def _entries(data: dict):
    """(label, sessions, minimum) for everything the extract commits."""
    t = data["thresholds"]
    for cls, c in data["classes"].items():
        for s in c["surfaces"]:
            yield f"{cls} surface", s["sessions"], t["k"]
            yield f"{cls} surface renderers", s["renderers"], t["surface_renderers"]
            for renderer, r in s["by_renderer"].items():
                yield renderer, r["sessions"], t["renderer"]
                yield f"{renderer} records", sum(rec[-1] for rec in r["records"]), r["sessions"]
    for scope in [data["checks"]["browser"], *data["checks"]["stacks"].values()]:
        for name, check in scope.items():
            for c in check if name == "window_deltas" else [check]:
                yield name, c["sessions"], t["k"]


def test_the_extract_uses_the_decided_thresholds():
    """A surface needs 3 clean sessions over 2 renderers, a claimed renderer 2 sessions with it."""
    assert EXTRACT["thresholds"] == {"k": 3, "surface_renderers": 2, "renderer": 2}
    assert (K, K_SURFACE_RENDERERS, K_RENDERER) == (3, 2, 2)
    assert EXTRACT["model"] == {"tag": pin().tag, "archive_sha256": pin().sha256}


def test_every_committed_entry_has_support():
    for label, sessions, minimum in _entries(EXTRACT):
        assert sessions >= minimum, label


def _lists(v):
    if isinstance(v, list):
        yield v
    for child in v.values() if isinstance(v, dict) else v if isinstance(v, list) else []:
        yield from _lists(child)


def test_the_extract_commits_no_version_and_no_record():
    assert not VERSION_STRING.search((DATA_DIR / "linux.json").read_text())
    assert max(len(v) for v in _lists(EXTRACT)) <= len(for_os("linux")[0].profile["speech"]["voices"])


def test_equals_capture_holds_when_recomputed():
    for cls, c in EXTRACT["classes"].items():
        for s in c["surfaces"]:
            assert s["equals_capture"] == sorted(
                cap.name.split(".", 1)[0] for cap in catalog()
                if cap.family == "linux" and host_class(cap.profile["gpu"]) == cls
                and surface_sha256(cap.profile["gpu"]) == s["sha256"])


@pytest.mark.parametrize("capture", [c for c in catalog() if c.family == "linux"], ids=lambda c: c.name)
def test_captures_agree_with_the_fpgen_consensus(capture):
    """A capture's browser- and driver-owned constants equal what most clean real sessions of
    its driver stack report; a mismatch means a bad capture or drift since the model."""
    p = capture.profile
    browser = EXTRACT["checks"]["browser"]
    stack = EXTRACT["checks"]["stacks"][host_class(p["gpu"]).split("/")[0]]  # type: ignore[union-attr]
    assert p["display"]["pointer"] == browser["pointer"]["mode"]
    assert p["hardware"]["max_touch_points"] == browser["max_touch_points"]["mode"]
    assert voices_sha256(p["speech"]["voices"]) == browser["voices_sha256"]["mode"]
    assert p["fonts"]["ui_font"] == browser["ui_font"]["mode"]
    assert p["media"]["widevine"]["supported"] == stack["widevine"]["mode"]
    assert p["media"]["hevc"] == stack["hevc"]["mode"]
    assert p["media"]["dolby_audio"] == stack["dolby_audio"]["mode"]
    assert p["gpu"]["hardware_decode"] == stack["hardware_decode"]["mode"]
    assert {k: p["capabilities"][k] for k in ("bluetooth", "web_share")} == stack["capabilities"]["mode"]
    # The browser chrome a user shows varies (bookmark bar, fullscreen): the captured delta
    # must be one that real sessions at that DPR show, not necessarily the most common.
    d, w = p["display"], p["display"]["window"]
    delta = {"dpr": d["device_pixel_ratio"], "height": w["outer_height"] - w["inner_height"],
             "width": w["outer_width"] - w["inner_width"]}
    assert delta in [{k: v for k, v in e.items() if k != "sessions"} for e in browser["window_deltas"]]


def _as_fpgen(ctx: dict) -> dict:
    """A capture's WebGL context in fpgen's record format."""
    shaders = {"VERTEX_SHADER": 35633, "FRAGMENT_SHADER": 35632}
    precisions = {"LOW_FLOAT": 36336, "MEDIUM_FLOAT": 36337, "HIGH_FLOAT": 36338, "LOW_INT": 36339, "MEDIUM_INT": 36340,
                  "HIGH_INT": 36341}
    extensions = {}
    if ctx["max_draw_buffers"] is not None:
        extensions["34852"] = {"name": "WEBGL_draw_buffers", "constantName": "MAX_DRAW_BUFFERS_WEBGL",
                               "parameter": {"value": ctx["max_draw_buffers"], "type": "number"}}
    return {
        "supportedExtensions": list(reversed(ctx["extensions"])),
        "contextAttributes": ctx["context_attributes"],
        "maxAnisotropy": float(ctx["max_anisotropy"]),
        "params": {str(i): {"name": k, "value": v} for i, (k, v) in enumerate(ctx["params"].items())},
        "shaderPrecisionFormats": [{"shaderType": shaders[k.split(".")[0]], "precisionType": precisions[k.split(".")[1]],
                                    "shaderPrecisionFormat": v} for k, v in ctx["precision"].items()],
        "extensions": extensions,
    }


def test_fpgen_records_map_onto_the_schema_surface():
    gpu = _capture_gpu()
    mapped = {ctx: schema_webgl(_as_fpgen(gpu[ctx])) for ctx in ("webgl", "webgl2")}
    assert surface_sha256(mapped) == surface_sha256(gpu)
    assert schema_webgl("getParameter is not a function") is None


README = """\
    Most Recent Driver Version: 615.78.08
A1. CURRENT NVIDIA GPUS

 NVIDIA GPU product                                       Device PCI ID*  VDPAU features
 -------------------------------------------------------  --------------  --------------
 NVIDIA GeForce RTX 3070 Ti                               2207            K
 NVIDIA GeForce RTX 3070 Ti                               2482            K
 NVIDIA GeForce RTX 2060 with Max-Q Design                1F12 1028 098F  J

The 580.xx driver supports the following set of GPUs:

 NVIDIA GPU product                            Device PCI ID*
 --------------------------------------------  --------------
 NVIDIA GeForce GTX 1080                       1B80

Appendix B. X Config Options
 NVIDIA GeForce RTX 9999                                  9999            K
"""
PCI_IDS = """\
#\tVersion: 2026.10.01
10de  NVIDIA Corporation
\t2207  GA102 [GeForce RTX 3070 Ti]
\t2482  GA104 [GeForce RTX 3070 Ti]
\t\t1043 87c0  A board
\t248c  GA104 [GeForce RTX 3070 Ti]
\t24e0  GA104M [Geforce RTX 3070 Ti Laptop GPU]
\t2520  GA106M [GeForce RTX 3060 Mobile / Max-Q]
10df  Emulex Corporation
\t2482  NotAGpu [GeForce RTX 3070 Ti]
"""


def test_the_driver_and_device_tables_parse():
    assert readme_tables(README) == ("615.78.08", {
        "NVIDIA GeForce RTX 3070 Ti": [("current", "0x2207"), ("current", "0x2482")],
        "NVIDIA GeForce RTX 2060 with Max-Q Design": [("current", None)],
        "NVIDIA GeForce GTX 1080": [("580.xx", "0x1b80")]})
    assert pci_desktop_ids(PCI_IDS, "10de") == {"GeForce RTX 3070 Ti": ["0x2207", "0x2482", "0x248c"]}
    gpu_info = {"vendors": {"Nvidia": {"id": "0x10DE", "devices": [
        {"mask": "0xFFFFFF00", "architecture": {"Ampere": ["0x2200", "0x2400"], "Lovelace": ["0x2800"]}}]}}}
    assert [dawn_architecture(gpu_info, "0x10de", i) for i in ("0x2482", "0x2882", "0x1b80")] == ["ampere", "lovelace", None]
