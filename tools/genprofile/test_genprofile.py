"""Unit tests for the sample profile generator. No browser, no network."""

from __future__ import annotations

import copy
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from genprofile.__main__ import main
from genprofile.captures import REPO, for_os, gpu_key, validate
from genprofile.claims import IDENTITY_FIELDS, claims, derive, device_ids, extract, renderers
from genprofile.fpgen_filters import renderer_class
from genprofile.generate import GenerationError, MissingCaptures, dumps, generate
from genprofile.surface import host_class, surface_sha256

HERE = Path(__file__).parent
SAMPLES = sorted((REPO / "profiles" / "samples").glob("*.json"))
SEEDS = ("0a1b2c3d4e5f60718293a4b5c6d7e8f9", "5f3c9e1a7b2d4086c1e9a3f5b7d20e48", "c4e8a2f6b1d3957e0f2a4c6e8b1d3f57")
# A UA product token or a four-part Chrome version, e.g. 155.0.8059.84.
VERSION_STRING = re.compile(r"Chrome/|\b\d{2,3}\.\d+\.\d{3,5}\.\d+\b")


def _linux(seed: str) -> dict:
    return generate("linux", seed, ["en-US", "en"], "America/New_York")


@pytest.mark.parametrize("seed", SEEDS)
def test_same_seed_gives_identical_bytes(seed):
    assert dumps(_linux(seed)) == dumps(_linux(seed))


def test_identical_bytes_across_processes_and_hash_seeds(tmp_path):
    outputs = []
    for hash_seed in ("1", "2"):
        out = tmp_path / f"{hash_seed}.json"
        subprocess.run(
            [sys.executable, "-m", "genprofile", "--os", "linux", "--seed", SEEDS[0], "--timezone", "America/New_York", "-o", str(out)],
            cwd=HERE, env={**os.environ, "PYTHONHASHSEED": hash_seed}, check=True, capture_output=True,
        )
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1] == dumps(_linux(SEEDS[0])).encode()


def test_different_seeds_give_different_profiles():
    assert len({dumps(_linux(seed)) for seed in SEEDS}) == len(SEEDS)


def test_global_random_is_never_used():
    state = random.getstate()
    _linux(SEEDS[0])
    assert random.getstate() == state


@pytest.mark.parametrize("seed", SEEDS)
def test_no_version_string_in_output(seed):
    assert not VERSION_STRING.search(dumps(_linux(seed)))


@pytest.mark.parametrize("seed", SEEDS)
def test_linux_output_is_schema_valid(seed):
    assert validate(_linux(seed)) == []


def test_linux_output_follows_the_conventions():
    capture = for_os("linux")[0].profile
    for i in range(24):
        p = _linux(f"{i:032x}")
        d, w, h = p["display"], p["display"]["window"], p["hardware"]
        assert (d["avail_left"], d["avail_top"], d["avail_width"], d["avail_height"]) == (0, 0, d["width"], d["height"])
        assert (w["screen_x"], w["screen_y"]) == (d["avail_left"], d["avail_top"])
        assert w["outer_width"] <= d["avail_width"] and w["outer_height"] <= d["avail_height"]
        assert d["device_pixel_ratio"] in (1, 1.25, 1.5, 1.75, 2) and d["color_depth"] == 24
        assert h["device_memory"] in (2, 4, 8, 16, 32) and h["concurrency"] % 2 == 0
        assert h["max_touch_points"] == 0 and d["pointer"] == capture["display"]["pointer"]
        assert ("physical_memory_mb" in h) == (h["device_memory"] == capture["hardware"]["device_memory"])
        assert _without_identity(p["gpu"]) == _without_identity(capture["gpu"]) and p["fonts"] == capture["fonts"]
        assert gpu_key(p["gpu"]) in {k for c in claims(for_os("linux")[0]) for k in c.gpu_keys}


def _without_identity(gpu: dict) -> dict:
    out = copy.deepcopy(gpu)
    for path in IDENTITY_FIELDS:
        *parents, leaf = path.split(".")
        node = out
        for key in parents:
            node = node[key]
        del node[leaf]
    return out


def test_the_capture_host_claims_the_rtx_3070_ti_and_nothing_else():
    """On the RTX 3060 host: of the five GPUs fpgen shows with its surface, only the
    RTX 3070 Ti has 2 clean sessions; both its desktop device ids are Ampere, so WebGPU
    stays claimable."""
    capture = for_os("linux")[0]
    assert [(c.renderer, c.device_ids, c.weight, c.derived) for c in claims(capture)] == [
        ("ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL ES 3.2)", ("0x2504",), 1, False),
        ("ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3070 Ti/PCIe/SSE2, OpenGL ES 3.2)", ("0x2207", "0x2482"), 2, True),
    ]


def test_claims_stay_within_the_captured_host_class():
    """No claim crosses a class, and a derived gpu section is the capture's except for the identity fields."""
    for capture in for_os("linux"):
        gpu = capture.profile["gpu"]
        for claim in claims(capture):
            assert renderer_class(claim.renderer) == host_class(gpu)
            for device_id in claim.device_ids if claim.derived else ():
                derived = derive(gpu, claim, device_id)
                assert _without_identity(derived) == _without_identity(gpu)
                assert surface_sha256(derived) == surface_sha256(gpu) and host_class(derived) == host_class(gpu)
                assert derived["webgl"]["unmasked_renderer"] == derived["webgl2"]["unmasked_renderer"] == claim.renderer
                assert derived["device_name"] == derived["gl_renderer"]
                assert derived["gl_renderer"].endswith(f"OpenGL ES 3.2 NVIDIA {gpu['driver_version']})")
                assert derived["device_id"] == device_id


@pytest.mark.parametrize("change, allowed", [
    ({}, ("0x2207", "0x2482")),
    ({"driver_table": "580.xx"}, ()),
    ({"laptop": True}, ()),
    ({"device_ids": {"0x2482": "lovelace"}}, ()),
    ({"device_ids": {"0x2482": "ampere", "0x2882": "lovelace"}}, ("0x2482",)),
])
def test_a_claim_needs_the_current_driver_a_desktop_part_and_the_webgpu_architecture(change, allowed):
    capture = for_os("linux")[0]
    gpu, data = capture.profile["gpu"], extract("linux")
    entry = {**renderers(data, gpu)["ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3070 Ti/PCIe/SSE2, OpenGL ES 3.2)"],
             **change}
    assert device_ids(entry, gpu, data) == allowed
    without_webgpu = {**gpu, "webgpu": {**gpu["webgpu"], "available": False}}
    if "device_ids" in change:
        assert device_ids(entry, without_webgpu, data) == tuple(sorted(change["device_ids"]))
    assert device_ids(entry, {**gpu, "driver_version": "580.95.05"}, data) == ()


def test_the_gpu_option_selects_a_claim():
    p = generate("linux", SEEDS[0], ["en-US", "en"], "America/New_York", "0x10de:0x2482")
    assert p["gpu"]["device_id"] == "0x2482" and "RTX 3070 Ti" in p["gpu"]["webgl"]["unmasked_renderer"]
    assert "derived within host class nvidia/opengl/gles" in p["captured"]["note"]
    assert validate(p) == []
    with pytest.raises(GenerationError):
        generate("linux", SEEDS[0], ["en-US", "en"], "America/New_York", "0x10de:0x248c")  # not in the driver's table


def _no_windows_capture(monkeypatch):
    """No capture covers the OS."""
    monkeypatch.setattr("genprofile.generate.for_os", lambda family: [] if family == "windows" else for_os(family))


def test_windows_profiles_come_from_the_windows_capture():
    p = generate("windows", SEEDS[0], ["en-US", "en"], "America/New_York")
    assert p["os"]["family"] == "windows" and p["gpu"]["vendor_id"] == "0x8086"
    assert validate(p) == []


def test_missing_captures_fail_loudly_and_invent_nothing(monkeypatch):
    _no_windows_capture(monkeypatch)
    with pytest.raises(MissingCaptures) as e:
        generate("windows", SEEDS[0], ["en-US", "en"], "America/New_York")
    assert {"gpu", "fonts", "speech", "audio", "display.pointer"} <= set(e.value.fields)
    for section in ("gpu", "fonts", "speech", "audio", "media", "webauthn", "system_colors"):
        assert section not in e.value.draft
    assert not VERSION_STRING.search(dumps(e.value.draft))


def test_macos_is_not_a_family():
    with pytest.raises(GenerationError, match="os must be one of linux, windows"):
        generate("macos", SEEDS[0], ["en-US", "en"], "America/New_York")


def test_drafts_are_never_written_into_the_repository(monkeypatch):
    _no_windows_capture(monkeypatch)
    out = REPO / "profiles" / "samples" / "draft-test.json"
    rc = main(["--os", "windows", "--seed", SEEDS[0], "--timezone", "UTC", "--draft", "-o", str(out)])
    assert rc == 1 and not out.exists()


def test_samples_are_checked_in():
    assert SAMPLES, "profiles/samples has no generated profiles"


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_checked_in_samples_regenerate_byte_for_byte(path):
    sample = json.loads(path.read_text())
    regenerated = generate(sample["os"]["family"], sample["seed"], sample["locale"]["languages"],
                           sample["locale"]["timezone"], gpu_key(sample["gpu"]))
    assert dumps(regenerated) == path.read_text()
    # profiles/samples/README.md regenerates without --gpu: the claim draw must land on the same GPU.
    assert dumps(generate(sample["os"]["family"], sample["seed"], sample["locale"]["languages"],
                          sample["locale"]["timezone"])) == path.read_text()
    assert path.stem == sample["id"]
