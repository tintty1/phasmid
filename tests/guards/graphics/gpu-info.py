"""Guard for patches/phasmid/graphics/gpu-info.patch.

The GPU identity reported outside the WebGL and WebGPU APIs follows gpu.*,
and nothing the browser decides from changes. The profile's device_id
(except on D3D11, whose renderer names it), driver_version, gl_renderer and
device_name are mutated to values the host does not have (report-only
fields, so any value is backable).

Every run uses --use-gl=disabled: the GPU process has no GL, so every WebGL
context fails after the renderer has collected the GPU info its status
message reports, on any host, headless and headful alike.

  0. precondition, no profile: webglcontextcreationerror fires in the main
     frame, a cross-origin iframe and a dedicated worker (OffscreenCanvas),
     and its statusMessage does not already carry the mutated identity;
  1. with the profile, statusMessage carries VENDOR, DEVICE, GL_VENDOR,
     GL_RENDERER and GL_VERSION (the driver version) from the profile in all
     three contexts;
  2. CDP SystemInfo.getInfo: the first device's ids, vendor and device
     strings and driver are the profile's, as are auxAttributes glVendor and
     glRenderer; glVersion stays the build's own;
  3. chrome://gpu shows the profile's DEVICE, DRIVER_VERSION and GL_RENDERER;
  4. the browser's GPU feature decisions stay the host's: CDP featureStatus and
     driverBugWorkarounds equal the plain run's;
  5. each example profile (win11-uhd600 on D3D11, linux-arch-rtx3060 on OpenGL) without
     vendor_id, device_id, vendor_name, device_name, gl_vendor and
     gl_renderer, which the browser derives from the WebGL claim, reports,
     in the status message of all three contexts, CDP
     and chrome://gpu, what the full example reports, except what its WebGL
     claim does not determine (the Linux PCI ids), which is 0 (0xffff in the
     status message) and never the host's id.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from _gpu import expect_results, session, write_profile
from helpers import GuardFailure, finish, load_profile, parse_args

from consistency.gpu_identity import derive

EXAMPLES = Path(__file__).resolve().parents[3] / "profiles" / "examples"
DERIVED_EXAMPLES = ("win11-uhd600-1440x960.json", "linux-arch-rtx3060-1080p.json")
# The fields pages cannot read that a profile may leave out.
OPTIONAL_IDENTITY = ("vendor_id", "device_id", "vendor_name", "device_name", "gl_vendor", "gl_renderer")

FLAGS = ["--use-gl=disabled"]

# Collects the creation error of a WebGL1 context, which fails under FLAGS.
STATUS_SCRIPT = """async () => {
  const canvas = typeof document !== "undefined" ? document.createElement("canvas") : new OffscreenCanvas(16, 16);
  let message = null;
  canvas.addEventListener("webglcontextcreationerror", (e) => { message = e.statusMessage; });
  const gl = canvas.getContext("webgl");
  return { created: !!gl, message };
}"""

# RTX 30-series device ids; the guard uses the first one the host is not.
DEVICE_IDS = ["0x2487", "0x2503", "0x2504", "0x2489"]


def bump_version(version: str) -> str:
    """615.71.09 -> 615.71.10: a driver version that differs, same shape."""
    head, _, last = version.rpartition(".")
    if last.isdigit():
        return f"{head}.{int(last) + 1:0{len(last)}d}" if head else str(int(last) + 1)
    return version + ".1"


def mutated_identity(profile: dict, host_device_id: int) -> dict:
    p = copy.deepcopy(profile)
    gpu = p["gpu"]
    if gpu["angle_backend"] != "d3d11":
        # A D3D11 renderer names its device id, which must match (check 5).
        gpu["device_id"] = next(d for d in DEVICE_IDS if int(d, 16) != host_device_id)
    old_driver = gpu["driver_version"]
    gpu["driver_version"] = bump_version(old_driver)
    if old_driver and old_driver in gpu["gl_renderer"]:
        gpu["gl_renderer"] = gpu["gl_renderer"].replace(old_driver, gpu["driver_version"])
    else:
        gpu["gl_renderer"] = gpu["gl_renderer"] + " (guard)"
    # A single GPU's device string is its GL_RENDERER (IdentifyActiveGPU).
    gpu["device_name"] = gpu["gl_renderer"]
    return p


def status_fields(gpu: dict) -> dict[str, str]:
    return {
        "VENDOR": f"0x{int(gpu['vendor_id'], 16):04x}",
        "DEVICE": f"0x{int(gpu['device_id'], 16):04x}",
        "GL_VENDOR": gpu["gl_vendor"],
        "GL_RENDERER": gpu["gl_renderer"],
        "GL_VERSION": gpu["driver_version"],
    }


def reported_status(identity: dict) -> tuple[dict[str, str], list[str]]:
    """The status message fields for a derived identity (ids as ints), and the
    ones it leaves out: an id of 0 prints as 0xffff and an empty string not
    at all (webgl_rendering_context_base.cc ExtractWebGLContextCreationError)."""
    fields = {
        "VENDOR": f"0x{identity['vendor_id'] or 0xffff:04x}",
        "DEVICE": f"0x{identity['device_id'] or 0xffff:04x}",
        "GL_VENDOR": identity["gl_vendor"],
        "GL_RENDERER": identity["gl_renderer"],
        "GL_VERSION": identity["driver_version"],
    }
    return {k: v for k, v in fields.items() if v}, [k for k, v in fields.items() if not v]


def gpu_line(identity: dict) -> str:
    """chrome://gpu's GPU0 entry up to DEVICE's bracket (gpu_internals_ui.cc
    GPUDeviceToString)."""
    vendor = f"0x{identity['vendor_id']:04x}" + (f" [{identity['vendor_name']}]" if identity["vendor_name"] else "")
    device = f"0x{identity['device_id']:04x}" + (f" [{identity['device_name']}]" if identity["device_name"] else "")
    return f"VENDOR= {vendor}, DEVICE={device}"


def check_derived(binary: str, work: Path, headless: bool, host_device: dict) -> None:
    """Check 5."""
    host_ids = (int(host_device.get("vendorId", 0)), int(host_device.get("deviceId", 0)))
    for name in DERIVED_EXAMPLES:
        example = json.loads((EXAMPLES / name).read_text())
        full, _ = derive(example["gpu"])
        stripped = copy.deepcopy(example)
        for key in OPTIONAL_IDENTITY:
            del stripped["gpu"][key]
        want, missing = derive(stripped["gpu"])
        same = {k for k in want if want[k] == full[k]}
        if same | {"vendor_id", "device_id"} != set(want):
            raise GuardFailure(f"vacuous: {name} derives {sorted(set(want) - same)} differently from the full example beyond the PCI ids")
        label = f"{name} without {', '.join(OPTIONAL_IDENTITY)}"
        run = session(binary, write_profile(stripped, work, f"derived-{name}"), headless=headless, extra_args=FLAGS, script=STATUS_SCRIPT, gpu_page=True)
        status, absent = reported_status(want)

        def check(r: dict) -> str | None:
            message = r.get("message") or ""
            wrong = [f"{k} = {v}" for k, v in status.items() if f"{k} = {v}" not in message]
            wrong += [f"no {k}" for k in absent if f", {k} = " in message]
            for key, host in zip(("VENDOR", "DEVICE"), host_ids):
                if host and f"{key} = 0x{host:04x}" in message and status[key] != f"0x{host:04x}":
                    wrong.append(f"the host's {key} 0x{host:04x}")
            return f"statusMessage lacks {wrong}: {message!r}" if wrong else None

        expect_results(run["results"], f"{label}: statusMessage reports the derived identity", check)
        device = first_device(run["system_info"])
        aux = run["system_info"]["gpu"].get("auxAttributes") or {}
        got = {"vendor_id": device.get("vendorId"), "device_id": device.get("deviceId"), "vendor_name": device.get("vendorString"),
               "device_name": device.get("deviceString"), "driver_vendor": device.get("driverVendor"),
               "driver_version": device.get("driverVersion"), "gl_vendor": aux.get("glVendor"), "gl_renderer": aux.get("glRenderer")}
        wrong = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
        if wrong:
            raise GuardFailure(f"{label}: CDP SystemInfo.getInfo (got, want): {wrong}")
        page = run["gpu_page"] or ""
        lacking = [s for s in (gpu_line(want), want["gl_renderer"]) if s not in page]
        if lacking:
            raise GuardFailure(f"{label}: chrome://gpu lacks {lacking}")
        print(f"PASS: {label}: CDP and chrome://gpu report the derived identity; not determined: {missing or 'nothing'}")


def first_device(system_info: dict) -> dict:
    devices = ((system_info or {}).get("gpu") or {}).get("devices") or []
    if not devices:
        raise GuardFailure(f"CDP SystemInfo.getInfo has no GPU device: {system_info!r:.300}")
    return devices[0]


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    plain = session(args.binary, None, headless=headless, extra_args=FLAGS, script=STATUS_SCRIPT, gpu_page=True)
    host_device = first_device(plain["system_info"])
    profile = mutated_identity(base, int(host_device.get("deviceId", 0)))
    want = status_fields(profile["gpu"])

    def plain_check(r: dict) -> str | None:
        if r.get("created") or not r.get("message"):
            return f"no webglcontextcreationerror status message: {r}"
        if f"DEVICE = {want['DEVICE']}" in r["message"] or f"GL_RENDERER = {want['GL_RENDERER']}" in r["message"]:
            return f"the host already reports the guard's identity: {r['message']}"
        return None

    try:
        expect_results(plain["results"], "precondition: without a profile the creation error carries the host identity", plain_check)
    except GuardFailure as exc:
        raise GuardFailure(f"vacuous: {exc}") from exc

    run = session(args.binary, write_profile(profile, work, "identity"), headless=headless, extra_args=FLAGS, script=STATUS_SCRIPT, gpu_page=True)

    def status_check(r: dict) -> str | None:
        message = r.get("message") or ""
        missing = [f"{k} = {v}" for k, v in want.items() if f"{k} = {v}" not in message]
        return f"statusMessage lacks {missing}: {message!r}" if missing else None

    expect_results(run["results"], "webglcontextcreationerror.statusMessage names the profile's GPU", status_check)

    device = first_device(run["system_info"])
    aux = run["system_info"]["gpu"].get("auxAttributes") or {}
    gpu = profile["gpu"]
    expected = {
        "vendorId": int(gpu["vendor_id"], 16),
        "deviceId": int(gpu["device_id"], 16),
        "vendorString": gpu["vendor_name"],
        "deviceString": gpu["device_name"],
        "driverVendor": gpu["driver_vendor"],
        "driverVersion": gpu["driver_version"],
    }
    wrong = {k: (device.get(k), v) for k, v in expected.items() if device.get(k) != v}
    for key, value in (("glVendor", gpu["gl_vendor"]), ("glRenderer", gpu["gl_renderer"])):
        if aux.get(key) != value:
            wrong[f"auxAttributes.{key}"] = (aux.get(key), value)
    plain_aux = plain["system_info"]["gpu"].get("auxAttributes") or {}
    if aux.get("glVersion") != plain_aux.get("glVersion"):
        wrong["auxAttributes.glVersion (build-owned)"] = (aux.get("glVersion"), plain_aux.get("glVersion"))
    if wrong:
        raise GuardFailure(f"CDP SystemInfo.getInfo (got, want): {wrong}")
    print("PASS: CDP SystemInfo.getInfo reports the profile's GPU identity and the build's GL_VERSION")

    page = run["gpu_page"] or ""
    missing = [s for s in (f"DEVICE={want['DEVICE']}", f"DRIVER_VERSION={gpu['driver_version']}", gpu["gl_renderer"]) if s not in page]
    if missing:
        raise GuardFailure(f"chrome://gpu lacks {missing}")
    print("PASS: chrome://gpu shows the profile's DEVICE, DRIVER_VERSION and GL_RENDERER")

    for key in ("featureStatus", "driverBugWorkarounds"):
        got, host = run["system_info"]["gpu"].get(key), plain["system_info"]["gpu"].get(key)
        if got != host:
            raise GuardFailure(f"{key} changed with the profile; decisions must stay the host's: {got!r:.300} vs {host!r:.300}")
    print("PASS: feature status and driver bug workarounds equal the plain run's")

    check_derived(args.binary, work, headless, host_device)


finish(main)
