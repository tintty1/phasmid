"""The GPU identity the browser reports for a profile (chrome://gpu, CDP
SystemInfo.getInfo, a failed WebGL context's status message): the profile's
gpu.* where given, else what the WebGL claim determines. Mirrors
DeriveGpuIdentity in patches/phasmid/core/profile.patch; the guards compute
their expectations with it.
"""

from __future__ import annotations

import re

# angle::GetVendorString inverted (third_party/angle driver_utils.cpp), the
# vendor as ANGLE prints it with commas removed; ids that fit 16 bits.
ANGLE_VENDOR_IDS = {"AMD": 0x1002, "Apple": 0x106B, "ARM": 0x13B5, "Broadcom": 0x14E4, "Google": 0x1AE0, "Intel": 0x8086,
                    "Microsoft": 0x1414, "NVIDIA": 0x10DE, "Imagination Technologies": 0x1010,
                    "Samsung Electronics Co. Ltd.": 0x144D, "Vivante": 0x9999, "VMware": 0x15AD, "VirtIO": 0x1AF4}
# Qualcomm is 0x5143 except through DXGI, which reports 0x4D4F4351.
QUALCOMM_ID = 0x5143

IDENTITY_FIELDS = ("vendor_id", "device_id", "vendor_name", "device_name", "driver_vendor", "driver_version", "gl_vendor", "gl_renderer")
_HEX8 = re.compile(r"[0-9A-Fa-f]{8}")
_GL_VENDORS = {"ANGLE": "ANGLE", "Mesa": "Mesa", "INTEL": "INTEL", "NVIDIA": "NVIDIA", "ATI": "ATI", "FireGL": "FireGL",
               "Chromium": "Chromium", "APPLE": "APPLE", "AMD": "AMD", "Metal": "Apple"}


class IdentityError(ValueError):
    """A given field the WebGL claim contradicts; the browser refuses it."""


def angle_parts(renderer: str) -> list[str] | None:
    """"ANGLE (<vendor>, <renderer>, <version>)" as its three parts."""
    if not (renderer.startswith("ANGLE (") and renderer.endswith(")")):
        return None
    parts = renderer[7:-1].split(", ")
    return parts if len(parts) == 3 else None


def angle_device_id(description: str) -> tuple[int, int] | None:
    """(device id, where the adapter name ends) from "(0x%08X)"."""
    pos = description.rfind(" (0x")
    hex_id = description[pos + 4:pos + 12]
    if pos < 0 or len(description) < pos + 13 or description[pos + 12] != ")" or not _HEX8.fullmatch(hex_id):
        return None
    return int(hex_id, 16), pos


def parse_gl_driver_info(version: str, vendor: str, driver_version: str) -> tuple[str, str]:
    """gl::GLVersionInfo::ParseDriverInfo."""
    if version.startswith("OpenGL ES "):
        version = version[len("OpenGL ES "):]
    pieces = [p for p in re.split(r"[ \-()@]", version) if p.strip()]
    if len(pieces) < 2:
        return vendor, driver_version
    for i in range(1, len(pieces)):
        if pieces[i] in _GL_VENDORS:
            return _GL_VENDORS[pieces[i]], pieces[i + 1] if i + 1 < len(pieces) else driver_version
    if len(pieces) == 2:
        return vendor, pieces[1][1:] if pieces[1].startswith("V") else pieces[1]
    if pieces[1].startswith("v1.r"):
        numbers = [n for n in pieces[1][4:].split("p") if n]
        parts = [p for p in pieces[2].split(".") if p]
        if len(numbers) == 2 and len(parts) == 2:
            return "ARM", f"{numbers[0]}.{numbers[1]}.{parts[0]}"
        return vendor, driver_version
    for piece in pieces[1:]:
        if "." in piece:
            return vendor, piece
    return vendor, driver_version


def parse_angle_driver_info(gl_renderer: str) -> tuple[str, str]:
    """gl::GLVersionInfo::ExtractDriverVendorANGLE: (driver vendor, version)."""
    if not gl_renderer.startswith("ANGLE (") or len(gl_renderer) <= 7:
        return "", ""
    strings = [s.strip() for s in gl_renderer[7:-1].split(",") if s.strip()]
    if len(strings) < 3:
        return "", ""
    return parse_gl_driver_info(strings[2].strip(")"), strings[0], "")


def derive(gpu: dict) -> tuple[dict, list[str]]:
    """The eight identity fields as the browser reports them (ids as ints),
    and the fields it warns about as neither given nor determined."""
    out = {k: gpu[k] for k in IDENTITY_FIELDS if k in gpu}
    for key in ("vendor_id", "device_id"):
        if key in out:
            out[key] = int(out[key], 16)
    missing: list[str] = []
    webgl = gpu["webgl"]
    renderer = webgl["unmasked_renderer"]
    if "gl_vendor" not in gpu:
        out["gl_vendor"] = webgl["unmasked_vendor"]
    elif gpu["gl_vendor"] != webgl["unmasked_vendor"]:
        raise IdentityError("gpu.gl_vendor")
    parts = angle_parts(renderer)
    backend = gpu["angle_backend"]
    d3d11 = backend == "d3d11" and parts is not None and parts[2] == "D3D11"

    determined: dict[str, int | None] = {"vendor_id": None, "device_id": None}
    name_end = None
    if backend == "swiftshader":
        determined = {"vendor_id": 0xFFFF, "device_id": 0xFFFF}
    elif parts and (d3d11 or backend == "vulkan"):
        determined["vendor_id"] = QUALCOMM_ID if parts[0] == "Qualcomm" and not d3d11 else ANGLE_VENDOR_IDS.get(parts[0])
        found = angle_device_id(parts[1])
        if found:
            name_end = found[1]
            determined["device_id"] = found[0] if found[0] <= 0xFFFF else None
    for key, value in determined.items():
        if key in gpu:
            if d3d11 and value is not None and value != out[key]:
                raise IdentityError(f"gpu.{key}")
        elif value is not None:
            out[key] = value
        else:
            out[key] = 0
            missing.append(key)

    renderer_known = True
    if d3d11:
        assert parts is not None
        if "driver_version" not in gpu and "gl_renderer" in gpu:
            version = parse_angle_driver_info(gpu["gl_renderer"])[1]
            if version:
                out["driver_version"] = version
        version = out.get("driver_version", "")
        expected = f"ANGLE ({parts[0]}, {parts[1]}, D3D11{'-' + version if version else ''})"
        if "gl_renderer" not in gpu:
            out["gl_renderer"] = expected
        elif gpu["gl_renderer"] != expected:
            raise IdentityError("gpu.gl_renderer")
    elif "gl_renderer" in gpu:
        full, head = gpu["gl_renderer"], renderer[:-1]
        if parts and full != renderer and not (full.endswith(")") and len(full) > len(renderer) and full.startswith(head)
                                               and full[len(head)] in " -"):
            raise IdentityError("gpu.gl_renderer")
    elif backend == "opengl" and parts and parts[2].startswith("OpenGL ES ") and gpu.get("driver_vendor") in ("NVIDIA", "Nvidia") and gpu.get("driver_version"):
        out["gl_renderer"] = f"{renderer[:-1]} NVIDIA {gpu['driver_version']})"
    else:
        out["gl_renderer"] = renderer
        renderer_known = False
        missing.append("gl_renderer")

    # The WebGL fallback lacks the driver part: its parse would name ANGLE's
    # vendor, not the driver, so nothing is determined then.
    vendor, version = parse_angle_driver_info(out["gl_renderer"]) if renderer_known else ("", "")
    if backend == "swiftshader":
        vendor = "SwANGLE"
    # A given driver must be Chrome's parse of a known GL_RENDERER, or on
    # Linux what ANGLE's system info names first (SystemInfo_linux.cpp):
    # "Nvidia" from NV-CONTROL with the same version, "AMD (Brahma)" or
    # "AMD (Catalyst)" on an AMD GPU with the kernel module's version.
    given_vendor = gpu.get("driver_vendor")
    amd_system_info = (not d3d11 and given_vendor in ("AMD (Brahma)", "AMD (Catalyst)")
                       and (out["vendor_id"] == 0x1002 or bool(parts and parts[0].startswith(("AMD", "ATI")))))
    nvctrl = not d3d11 and given_vendor == "Nvidia" and vendor == "NVIDIA"
    if (given_vendor is not None and vendor and given_vendor != vendor
            and not amd_system_info and not nvctrl):
        raise IdentityError("gpu.driver_vendor")
    if "driver_version" in gpu and version and gpu["driver_version"] != version and not amd_system_info:
        raise IdentityError("gpu.driver_version")
    if "driver_vendor" not in gpu:
        out["driver_vendor"] = vendor
        if not vendor:
            missing.append("driver_vendor")
    if "driver_version" not in gpu and not out.get("driver_version"):
        out["driver_version"] = version
        if not version:
            missing.append("driver_version")
    if "vendor_name" not in gpu:
        out["vendor_name"] = "" if d3d11 else out["gl_vendor"]
    if "device_name" not in gpu:
        out["device_name"] = parts[1][:name_end] if d3d11 and parts and name_end is not None else out["gl_renderer"]
    return out, missing
