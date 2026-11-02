"""Unit tests for the comparison logic of the consistency harness.

These need no browser. The end-to-end dump is exercised by CI against a real
binary; a missing binary there is a failure, never a skip.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from consistency.contexts import _partition, cross_context_report
from consistency.diff import diff_dumps, flatten, is_volatile

HERE = Path(__file__).parent


def _dump(main: dict, worker: dict | None = None, headers: dict | None = None) -> dict:
    worker = worker if worker is not None else copy.deepcopy(main)
    return {
        "dump_version": 1,
        "harness": {"binary": "x"},
        "contexts": {"main": main, "worker": worker},
        "context_errors": {},
        "headers": headers or {},
    }


def test_flatten_scalars_lists_and_nested():
    flat = flatten({"a": {"b": 1, "c": [1, 2]}, "d": [{"e": 1}, {"e": 2}]})
    assert flat == {"a.b": 1, "a.c": [1, 2], "d.__len": 2, "d.0.e": 1, "d.1.e": 2}


def test_diff_reports_changed_keys_only():
    a = _dump({"navigator": {"hardwareConcurrency": 8, "platform": "Linux x86_64"}})
    b = _dump({"navigator": {"hardwareConcurrency": 12, "platform": "Linux x86_64"}})
    d = diff_dumps(a, b)
    assert set(d) == {"contexts.main.navigator.hardwareConcurrency", "contexts.worker.navigator.hardwareConcurrency"}
    assert d["contexts.main.navigator.hardwareConcurrency"] == (8, 12)


def test_diff_collapses_lists_whose_length_changed():
    a = _dump({"speech": {"voices": [{"name": "a"}, {"name": "b"}]}})
    b = _dump({"speech": {"voices": [{"name": "a"}]}})
    d = diff_dumps(a, b)
    assert d["contexts.main.speech.voices"] == ("<list of 2>", "<list of 1>")
    assert not any(k.startswith("contexts.main.speech.voices.") for k in d)


def test_diff_ignores_volatile_unless_asked():
    a = _dump({"storage": {"usage": 1, "quota": 10}})
    b = _dump({"storage": {"usage": 2, "quota": 11}})
    assert diff_dumps(a, b) == {}
    assert len(diff_dumps(a, b, ignore_volatile=False)) == 4
    assert is_volatile("harness.started")
    assert not is_volatile("contexts.main.navigator.userAgent")


def test_cross_context_reports_split_between_contexts():
    d = _dump({"navigator": {"hardwareConcurrency": 8}}, {"navigator": {"hardwareConcurrency": 4}})
    report = cross_context_report(d)
    assert report["disagreements"] == {"navigator.hardwareConcurrency": {"main": 8, "worker": 4}}


def test_cross_context_baseline_hides_same_split_only():
    d = _dump({"x": {"k": 1, "j": 1}}, {"x": {"k": 2, "j": 2}})
    same_split = _dump({"x": {"k": 10, "j": 10}}, {"x": {"k": 20, "j": 20}})
    report = cross_context_report(d, same_split)
    assert report["disagreements"] == {}
    assert set(report["inherited_from_baseline"]) == {"x.k", "x.j"}

    # A baseline that agrees across contexts does not excuse a split.
    agreeing = _dump({"x": {"k": 1, "j": 1}})
    report = cross_context_report(d, agreeing)
    assert set(report["disagreements"]) == {"x.k", "x.j"}


def test_partition_is_shape_not_values():
    assert _partition({"main": 1, "worker": 2}) == _partition({"main": "a", "worker": "b"})
    assert _partition({"main": 1, "worker": 1}) != _partition({"main": 1, "worker": 2})


def test_header_checks_compare_sent_headers_and_flag_absent():
    main = {
        "navigator": {
            "userAgent": "UA",
            "languages": ["en-US", "en", "de"],
            "deviceMemory": 8,
            "userAgentData": {
                "brands": [{"brand": "Chromium", "version": "155"}],
                "mobile": False,
                "platform": "Windows",
                "highEntropy": {"platformVersion": "15.0.0", "architecture": "x86", "bitness": "64", "model": "", "wow64": False,
                                "fullVersionList": [{"brand": "Chromium", "version": "155.0.8059.84"}], "uaFullVersion": "155.0.8059.84"},
            },
        },
        "screen": {"devicePixelRatio": 1.25},
    }
    headers = {
        "navigation": {"user-agent": "UA", "accept-language": "en-US,en;q=0.9,de;q=0.8", "sec-ch-ua": '"Chromium";v="155"', "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'},
        "navigation_with_hints": {"user-agent": "UA", "sec-ch-ua-platform-version": '"15.0.0"', "sec-ch-device-memory": "8", "device-memory": "8", "sec-ch-dpr": "1.25", "dpr": "1.25", "sec-ch-ua-arch": '"x86"'},
        "fetch_main": {"user-agent": "WRONG"},
    }
    report = cross_context_report(_dump(main, headers=headers))
    checks = {(c["slot"], c["header"]): c for c in report["header_checks"]}
    assert checks[("navigation", "accept-language")]["ok"] is True
    assert checks[("navigation_with_hints", "sec-ch-dpr")]["ok"] is True
    assert checks[("fetch_main", "user-agent")]["ok"] is False
    assert checks[("fetch_main", "sec-ch-ua")]["ok"] is None  # not sent: parity question, not a mismatch


def test_within_context_version_rule():
    main = {
        "navigator": {
            "userAgent": "Mozilla/5.0 Chrome/155.0.0.0 Safari/537.36",
            "userAgentData": {"brands": [], "mobile": False, "platform": "Linux",
                              "highEntropy": {"fullVersionList": [{"brand": "Chromium", "version": "154.0.1.1"}, {"brand": "Not_A Brand", "version": "8.0.0.0"}], "uaFullVersion": "154.0.1.1"}},
        }
    }
    report = cross_context_report(_dump(main))
    checks = {c["check"]: c for c in report["within_context"] if c["context"] == "main"}
    assert checks["ua major == fullVersionList major"]["ok"] is False


def test_within_context_detector_invariants():
    """The canvas and audio detector invariants are asserted in every context that ran them."""
    good_canvas = {"pixelMods": 0, "clearedIsZero": True, "putRandomExact": True, "inOutStable": True}
    good_audio = {"analyserTailMatchesBuffer": True, "pageBufferPlaysExactly": True}
    main = {"canvasNoise": good_canvas, "audioInvariants": good_audio}
    worker = {"canvasNoise": dict(good_canvas, pixelMods=3, putRandomExact=False), "audioInvariants": {"offlineAudioContext": "unavailable"}}
    report = cross_context_report(_dump(main, worker))
    failed = {(c["context"], c["check"]) for c in report["within_context"] if not c["ok"]}
    assert failed == {("worker", "canvasNoise.pixelMods == 0"), ("worker", "canvasNoise.putRandomExact")}
    checked = {(c["context"], c["check"]) for c in report["within_context"]}
    assert ("main", "audioInvariants.pageBufferPlaysExactly") in checked
    assert not any(ctx == "worker" and check.startswith("audioInvariants.") for ctx, check in checked)


@pytest.mark.skipif(not os.environ.get("PHASMID_BINARY"), reason="set PHASMID_BINARY to run the end-to-end dump")
def test_end_to_end_dump(tmp_path):
    from consistency.dump import run_dump

    dump = run_dump(Path(os.environ["PHASMID_BINARY"]), headless=True, timeout=120)
    assert dump["context_errors"] == {}, dump["context_errors"]
    for ctx in ["main", "iframe_same", "iframe_cross", "worker", "shared_worker", "service_worker"]:
        assert ctx in dump["contexts"], ctx
        assert "userAgent" in dump["contexts"][ctx]["navigator"]
    assert dump["headers"]["navigation_with_hints"].get("sec-ch-ua-full-version-list")
    (tmp_path / "dump.json").write_text(json.dumps(dump))


# ---- capture and compare round trip on the in-repo capture -----------------

CAPTURE = HERE.parent / "profiles" / "data" / "captures" / "linux-arch-rtx3060-1080p.chrome153-headful.json"
EXAMPLE = HERE.parent / "profiles" / "examples" / "linux-arch-rtx3060-1080p.json"


def test_example_profiles_validate():
    from consistency.capture import validate

    for path in sorted((HERE.parent / "profiles" / "examples").glob("*.json")):
        assert validate(json.loads(path.read_text())) == [], path.name


def test_capture_roundtrip_matches_its_own_dump():
    """A profile captured from a dump must compare clean against that dump."""
    from consistency.capture import capture, validate
    from consistency.compare import compare

    dump = json.loads(CAPTURE.read_text())
    profile = capture(dump, profile_id="roundtrip", os_version="test", gpu_ids={}, seed="0" * 32, disk_total_bytes=234092900352)
    assert validate(profile) == []
    report = compare(dump, profile)
    assert report["failures"] == [], report["failures"][:5]
    assert report["passes"] > 300


def test_capture_takes_gpu_ids_from_cdp_system_info():
    from consistency.capture import capture

    dump = json.loads(CAPTURE.read_text())
    profile = capture(dump, profile_id="x", os_version="test", gpu_ids={}, seed="0" * 32, disk_total_bytes=234092900352)
    assert profile["gpu"]["vendor_id"] == "0x10de"
    assert profile["gpu"]["device_id"] == "0x2504"
    assert profile["gpu"]["driver_version"] == "615.71.09"
    assert profile["gpu"]["gl_version"].startswith("OpenGL ES")


def test_capture_keeps_the_empty_windows_vendor_string():
    """Windows GPUInfo has no vendor string (chrome://gpu prints no bracket after
    VENDOR=); the WebGL vendor must not stand in for it."""
    from consistency.capture import capture

    dump = json.loads((HERE.parent / "profiles" / "data" / "captures" / "win11-uhd600-1440x960.chrome156-headful.json").read_text())
    profile = capture(dump, profile_id="x", os_version="11", gpu_ids={}, seed="0" * 32)
    assert profile["gpu"]["vendor_name"] == ""
    assert profile["gpu"]["device_name"] == "Intel(R) UHD Graphics 600"
    assert (profile["gpu"]["vendor_id"], profile["gpu"]["device_id"]) == ("0x8086", "0x3185")
    assert profile["gpu"]["driver_version"] == "31.0.101.2140"
    assert profile["hardware"]["physical_memory_mb"] == 8013  # the heap limit inverts to what the host measured


def test_capture_derives_gpu_ids_only_where_the_renderer_determines_them():
    from consistency.capture import _gpu_ids_from_renderer

    win = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)"
    assert _gpu_ids_from_renderer(win, "windows") == {"vendor_id": "0x10de", "device_id": "0x2503"}
    warp = "ANGLE (Microsoft, Microsoft Basic Render Driver (0x0000008C) Direct3D11 vs_5_0 ps_5_0, D3D11)"
    assert _gpu_ids_from_renderer(warp, "windows") == {"vendor_id": "0x1414", "device_id": "0x008c"}
    swiftshader = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)"
    assert _gpu_ids_from_renderer(swiftshader, "linux") == {"vendor_id": "0xffff", "device_id": "0xffff"}
    vulkan = "ANGLE (NVIDIA, Vulkan 1.3.289 (NVIDIA NVIDIA GeForce RTX 3060 (0x00002504)), NVIDIA)"
    assert _gpu_ids_from_renderer(vulkan, "linux") == {"vendor_id": "0x10de", "device_id": "0x2504"}
    # OpenGL strings name no id: SystemInfo or --gpu supplies it, or it stays out.
    assert _gpu_ids_from_renderer("ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL ES 3.2)", "linux") == {}
    # DXGI reports Qualcomm as 0x4D4F4351, wider than the schema's ids.
    with pytest.raises(ValueError, match="gpu.vendor_id"):
        _gpu_ids_from_renderer("ANGLE (Qualcomm, Qualcomm GPU (0x00001234) Direct3D11 vs_5_0 ps_5_0, D3D11)", "windows")
    with pytest.raises(ValueError, match="gpu.vendor_id"):
        _gpu_ids_from_renderer("ANGLE (Acme, Acme GPU (0x00001234) Direct3D11 vs_5_0 ps_5_0, D3D11)", "windows")


def test_capture_refuses_a_macos_dump():
    """No macOS profile, from the schema or from a capture."""
    from consistency.capture import SCHEMA_PATH, _family_from_dump

    os_props = json.loads(SCHEMA_PATH.read_text())["properties"]["os"]["properties"]
    assert os_props["family"]["enum"] == ["windows", "linux"]
    assert "macOS" not in os_props["ch_platform"]["enum"] and "MacIntel" not in os_props["navigator_platform"]["enum"]
    assert _family_from_dump({"userAgentData": {"platform": "Windows"}}) == "windows"
    assert _family_from_dump({"platform": "Linux x86_64"}) == "linux"
    with pytest.raises(ValueError, match="os.family"):
        _family_from_dump({"userAgentData": {"platform": "macOS"}, "platform": "MacIntel"})


def test_capture_derives_physical_memory_from_the_heap_limit():
    from consistency.capture import physical_memory_from_dump, v8_heap_size_limit

    for mb, device_memory in ((2048, 2), (4096, 4), (7168, 8)):
        assert physical_memory_from_dump(device_memory, v8_heap_size_limit(mb)) == mb
    assert v8_heap_size_limit(2048) == 1124073472  # a 2 GB machine
    assert physical_memory_from_dump(32, 4395630592) == 32768
    assert physical_memory_from_dump(16, None) == 16384
    with pytest.raises(ValueError, match="hardware.physical_memory_mb"):
        physical_memory_from_dump(8, 2000000000)


def test_capture_disk_reproduces_the_quota_headroom():
    """The disk size is one the reported quota headroom implies."""
    from consistency.capture import GIB, disk_total_bytes_from_dump

    assert disk_total_bytes_from_dump(8 * GIB, "0" * 32) == 8 * GIB
    picked = {disk_total_bytes_from_dump(10 * GIB, f"{i:032x}") for i in range(64)}
    assert picked == {(97696368 + 1953504 * (gb - 50)) * 512 for gb in (256, 512, 1000, 2000)}
    assert disk_total_bytes_from_dump(10 * GIB, "ab" * 16) == disk_total_bytes_from_dump(10 * GIB, "ab" * 16)


def test_capture_of_a_page_visit_needs_no_operator_values():
    """A Windows dump with no chrome://gpu report, RAM or disk still captures."""
    from consistency.capture import capture, validate

    dump = json.loads(CAPTURE.read_text())
    dump.pop("system_info")
    nav = dump["contexts"]["main"]["navigator"]
    nav["platform"] = "Win32"
    nav["userAgentData"]["platform"] = "Windows"
    renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)"
    for kind in ("webgl", "webgl2"):
        dump["contexts"]["main"][kind]["unmaskedRenderer"] = renderer
    profile = capture(dump, profile_id="win", os_version="11", gpu_ids={}, seed="0" * 32)
    gpu = profile["gpu"]
    assert (gpu["vendor_id"], gpu["device_id"]) == ("0x10de", "0x2503")
    assert "driver_vendor" not in gpu and "driver_version" not in gpu and "gl_version" not in gpu
    assert profile["hardware"]["physical_memory_mb"] == 32768
    assert "derived:" in profile["captured"]["note"]
    assert validate(profile) == []


def test_capture_of_a_linux_page_visit_leaves_the_identity_out():
    """What the page visit did not measure is left out, not written as the WebGL strings, and
    the profile validates: a Linux profile's driver is optional too."""
    from consistency.capture import capture, validate

    dump = json.loads(CAPTURE.read_text())
    dump.pop("system_info")
    profile = capture(dump, profile_id="linux-visit", os_version="arch", gpu_ids={}, seed="0" * 32)
    gpu = profile["gpu"]
    assert not {"vendor_id", "device_id", "vendor_name", "device_name", "gl_vendor", "gl_renderer",
                "driver_vendor", "driver_version"} & gpu.keys()
    assert validate(profile) == []


def _without(gpu: dict, fields) -> dict:
    return {k: v for k, v in gpu.items() if k not in fields}


SIX = ("vendor_id", "device_id", "vendor_name", "device_name", "gl_vendor", "gl_renderer")


def test_gpu_identity_derives_the_windows_example():
    """The D3D11 renderer determines every identity field of the captured Windows GPU."""
    from consistency.gpu_identity import derive

    gpu = json.loads((HERE.parent / "profiles" / "examples" / "win11-uhd600-1440x960.json").read_text())["gpu"]
    full, missing = derive(gpu)
    assert missing == []
    assert derive(_without(gpu, SIX)) == (full, [])
    assert full["vendor_id"] == 0x8086 and full["device_id"] == 0x3185 and full["vendor_name"] == ""
    assert full["device_name"] == "Intel(R) UHD Graphics 600"
    assert full["gl_renderer"] == gpu["gl_renderer"] and full["driver_vendor"] == gpu["driver_vendor"]
    # Without the driver version only it, and ANGLE's suffix, are missing.
    out, missing = derive(_without(gpu, (*SIX, "driver_vendor", "driver_version")))
    assert missing == ["driver_version"]
    assert out["gl_renderer"] == gpu["webgl"]["unmasked_renderer"] and out["driver_vendor"] == "Intel"


def test_gpu_identity_derives_the_linux_example_except_the_pci_ids():
    from consistency.gpu_identity import derive

    gpu = json.loads(EXAMPLE.read_text())["gpu"]
    full, _ = derive(gpu)
    out, missing = derive(_without(gpu, SIX))
    assert missing == ["vendor_id", "device_id"]
    assert (out["vendor_id"], out["device_id"]) == (0, 0)
    assert {k: v for k, v in out.items() if k not in ("vendor_id", "device_id")} == {k: v for k, v in full.items() if k not in ("vendor_id", "device_id")}
    assert out["gl_renderer"] == gpu["gl_renderer"] and out["device_name"] == gpu["device_name"]


def test_gpu_identity_refuses_what_the_webgl_claim_contradicts():
    from consistency.gpu_identity import IdentityError, derive

    win = json.loads((HERE.parent / "profiles" / "examples" / "win11-uhd600-1440x960.json").read_text())["gpu"]
    linux = json.loads(EXAMPLE.read_text())["gpu"]
    for gpu, field, value in ((win, "device_id", "0x3184"), (win, "vendor_id", "0x10de"), (linux, "gl_vendor", "Google Inc. (Intel)"),
                              (win, "gl_renderer", win["gl_renderer"].replace("2140", "2141")),
                              (linux, "gl_renderer", linux["gl_renderer"].replace("RTX 3060", "RTX 3070"))):
        with pytest.raises(IdentityError, match=field):
            derive({**gpu, field: value})
    # Linux PCI ids come from libpci, not the GL strings: any id is accepted.
    assert derive({**linux, "device_id": "0x2503"})[0]["device_id"] == 0x2503


def test_gpu_identity_derives_the_linux_driver_from_gl_renderer():
    """The Linux example without its driver derives the captured one from
    gl_renderer; a driver gl_renderer contradicts is refused, except what
    ANGLE's Linux system info names first."""
    from consistency.gpu_identity import IdentityError, derive

    gpu = json.loads(EXAMPLE.read_text())["gpu"]
    full, _ = derive(gpu)
    out, missing = derive(_without(gpu, ("driver_vendor", "driver_version")))
    assert (out, missing) == (full, [])
    assert (out["driver_vendor"], out["driver_version"]) == (gpu["driver_vendor"], gpu["driver_version"]) == ("NVIDIA", "615.71.09")
    for field, value in (("driver_vendor", "Mesa"), ("driver_version", "580.95.05")):
        with pytest.raises(IdentityError, match=field):
            derive({**gpu, field: value})
    win = json.loads((HERE.parent / "profiles" / "examples" / "win11-uhd600-1440x960.json").read_text())["gpu"]
    with pytest.raises(IdentityError, match="driver_vendor"):
        derive({**win, "driver_vendor": "NVIDIA"})
    # NV-CONTROL names the same driver "Nvidia", which also derives gl_renderer.
    assert derive({**gpu, "driver_vendor": "Nvidia"})[0]["driver_vendor"] == "Nvidia"
    nvctrl, missing = derive({**_without(gpu, ("gl_renderer",)), "driver_vendor": "Nvidia"})
    assert nvctrl["gl_renderer"] == gpu["gl_renderer"] and "gl_renderer" not in missing
    # Without gl_renderer and the driver nothing names the driver: the WebGL
    # string's vendor part is ANGLE's, so both are blank and warned.
    out, missing = derive(_without(gpu, ("gl_renderer", "driver_vendor", "driver_version")))
    assert (out["driver_vendor"], out["driver_version"]) == ("", "")
    assert {"gl_renderer", "driver_vendor", "driver_version"} <= set(missing)
    # Without gl_renderer only the derived renderer is known: nothing to contradict.
    assert derive({**_without(gpu, ("gl_renderer", "driver_vendor")), "driver_version": "580.95.05"})[0]["driver_version"] == "580.95.05"


def test_compare_flags_every_context_for_a_wrong_value():
    from consistency.compare import compare

    dump = json.loads(CAPTURE.read_text())
    profile = json.loads(EXAMPLE.read_text())
    profile = copy.deepcopy(profile)
    profile["hardware"]["concurrency"] = 12
    report = compare(dump, profile)
    ctxs = sorted(f["context"] for f in report["failures"] if f["profile_path"] == "hardware.concurrency")
    assert ctxs == ["iframe_cross", "iframe_same", "main", "service_worker", "shared_worker", "worker"]


def test_compare_headers_follow_profile_languages():
    from consistency.compare import accept_language, compare

    assert accept_language(["en-US", "en", "de"]) == "en-US,en;q=0.9,de;q=0.8"
    dump = json.loads(CAPTURE.read_text())
    profile = copy.deepcopy(json.loads(EXAMPLE.read_text()))
    profile["locale"]["languages"] = ["de-DE", "de"]
    report = compare(dump, profile)
    header_fails = [f for f in report["failures"] if f["context"].startswith("header:") and f["dump_path"] == "accept-language"]
    assert header_fails, "accept-language mismatch must be reported"


def test_compare_quota_follows_disk_total_bytes():
    from consistency.compare import compare, reported_quota_headroom

    gib = 1 << 30
    assert reported_quota_headroom(234092900352) == 10 * gib
    assert reported_quota_headroom(10 * gib) == 10 * gib
    assert reported_quota_headroom(6_500_000_000) == 7 * gib
    assert reported_quota_headroom(4 * gib) == 4 * gib
    dump = json.loads(CAPTURE.read_text())
    profile = copy.deepcopy(json.loads(EXAMPLE.read_text()))
    assert not [f for f in compare(dump, profile)["failures"] if f["profile_path"] == "hardware.disk_total_bytes"]
    profile["hardware"]["disk_total_bytes"] = 6_500_000_000
    ctxs = sorted(f["context"] for f in compare(dump, profile)["failures"] if f["profile_path"] == "hardware.disk_total_bytes")
    assert ctxs == ["iframe_cross", "iframe_same", "main", "service_worker", "shared_worker", "worker"]


def test_compare_network_connection_and_battery():
    """The stock capture lies inside its own profile's noise envelope and is
    flagged everywhere for another estimate and another battery."""
    from consistency.compare import NOISE_FACTORS, compare, reported_downlink, reported_rtt

    assert {reported_rtt(100, f) for f in NOISE_FACTORS} == {100}
    assert 1.5 in {reported_downlink(1500, f) for f in NOISE_FACTORS}
    # On the 50 kbps grid the browser's and the renderer's arithmetic agree.
    assert all(reported_downlink(k, f) == reported_downlink(k, f, renderer=False) for k in range(0, 20001, 50) for f in NOISE_FACTORS)
    dump = json.loads(CAPTURE.read_text())
    profile = copy.deepcopy(json.loads(EXAMPLE.read_text()))
    assert not [f for f in compare(dump, profile)["failures"] if f["profile_path"].startswith(("network.connection", "capabilities.battery"))]
    profile["network"]["connection"] = {"effective_type": "3g", "rtt": 450, "downlink": 0.75}
    profile["capabilities"]["battery"] = {"charging": False, "level": 0.42}
    by_path: dict[tuple[str, str], set[str]] = {}
    for f in compare(dump, profile)["failures"]:
        by_path.setdefault((f["profile_path"], f["dump_path"]), set()).add(f["context"])
    every = {"main", "iframe_same", "iframe_cross", "worker", "shared_worker", "service_worker"}
    window = {"main", "iframe_same", "iframe_cross"}
    assert by_path[("network.connection.effective_type", "navigator.connection.effectiveType")] == every
    assert by_path[("network.connection.rtt", "navigator.connection.rtt")] == every
    assert by_path[("network.connection.downlink", "navigator.connection.downlink")] == every
    assert by_path[("network.connection.effective_type", "ect")] == {"header:navigation_with_hints", "header:fetch_main"}
    assert by_path[("capabilities.battery.charging", "navigator.battery.charging")] == window
    assert by_path[("capabilities.battery.level", "navigator.battery.level")] == window
    assert by_path[("capabilities.battery", "navigator.battery.chargingTime")] == window
