"""Guard for patches/phasmid/graphics/webgl.patch.

Every WebGL1 and WebGL2 context reports gpu.webgl / gpu.webgl2, and Blink
enforces the claimed limits. Needs the profile's GPU class (the capture
host, headful or headless): the profile is mutated only where that host can
back it, and any other host skips. Headless, the runs without a profile take
--enable-gpu, the host GPU a profile run renders on.

  0. precondition, no profile: the host renders WebGL with the profile's own
     unmasked renderer and exposes every profile extension (else skip); the
     guard's lowered limits are within the host's (texImage2D, activeTexture,
     vertexAttribPointer at the lowered limits succeed, the drawing buffer is
     not clamped, the removed extension works);
  1. with the mutated profile, in all six contexts (OffscreenCanvas in the
     workers): UNMASKED_VENDOR/RENDERER_WEBGL are the profile's; VENDOR,
     RENDERER, VERSION and SHADING_LANGUAGE_VERSION are the build's
     constants; getSupportedExtensions() is the profile's list with one
     extension removed; every limit the patch owns, including the int64 and
     float WebGL2 ones and a lowered minimum offset and raised alignment, is
     the profile's; the twelve precision formats (LOW_INT and MEDIUM_INT at
     15/14) and MAX_TEXTURE_MAX_ANISOTROPY_EXT (8) are the profile's;
  2. framebuffer-dependent values (*_BITS, SAMPLES, SAMPLE_BUFFERS,
     IMPLEMENTATION_COLOR_READ_*) and getContextAttributes() equal the plain
     run's in every context, powerPreference split included;
  3. enforcement in the main frame, a cross-origin iframe and a dedicated
     worker: texImage2D one past MAX_TEXTURE_SIZE is INVALID_VALUE,
     activeTexture(TEXTURE0 + MAX_COMBINED_TEXTURE_IMAGE_UNITS) is
     INVALID_ENUM, vertexAttribPointer(MAX_VERTEX_ATTRIBS) is INVALID_VALUE,
     a canvas wider than MAX_VIEWPORT_DIMS gets the claimed drawingBufferWidth,
     getExtension(removed) is null, and (WebGL1) a shader requiring the
     removed GL_EXT_shader_texture_lod fails to compile;
  4. no claim was reported as unbacked, and the WebGL keys show no
     cross-context split the plain run does not have.
"""

from __future__ import annotations

import copy
from pathlib import Path

from _gpu import checked, expect_no_unbacked, expect_results, host_gpu_args, session, write_profile
from consistency.compare import _get
from consistency.contexts import cross_context_report
from helpers import ALL_CONTEXTS, GuardFailure, GuardSkip, dump, expect_everywhere, finish, load_profile, parse_args

KINDS = ("webgl", "webgl2")

# Limits the patch owns (components/phasmid profile.cc validates the same).
WEBGL_PARAMS = [
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS", "MAX_CUBE_MAP_TEXTURE_SIZE", "MAX_FRAGMENT_UNIFORM_VECTORS",
    "MAX_RENDERBUFFER_SIZE", "MAX_TEXTURE_IMAGE_UNITS", "MAX_TEXTURE_SIZE", "MAX_VARYING_VECTORS",
    "MAX_VERTEX_ATTRIBS", "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_UNIFORM_VECTORS", "SUBPIXEL_BITS",
    "MAX_VIEWPORT_DIMS", "ALIASED_LINE_WIDTH_RANGE", "ALIASED_POINT_SIZE_RANGE",
]
WEBGL2_PARAMS = [
    "MAX_3D_TEXTURE_SIZE", "MAX_ARRAY_TEXTURE_LAYERS", "MAX_COLOR_ATTACHMENTS",
    "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS", "MAX_COMBINED_UNIFORM_BLOCKS",
    "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS", "MAX_DRAW_BUFFERS", "MAX_ELEMENT_INDEX", "MAX_ELEMENTS_INDICES",
    "MAX_ELEMENTS_VERTICES", "MAX_FRAGMENT_INPUT_COMPONENTS", "MAX_FRAGMENT_UNIFORM_BLOCKS",
    "MAX_FRAGMENT_UNIFORM_COMPONENTS", "MAX_PROGRAM_TEXEL_OFFSET", "MAX_SAMPLES", "MAX_SERVER_WAIT_TIMEOUT",
    "MAX_TEXTURE_LOD_BIAS", "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS",
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS", "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS",
    "MAX_UNIFORM_BLOCK_SIZE", "MAX_UNIFORM_BUFFER_BINDINGS", "MAX_VARYING_COMPONENTS",
    "MAX_VERTEX_OUTPUT_COMPONENTS", "MAX_VERTEX_UNIFORM_BLOCKS", "MAX_VERTEX_UNIFORM_COMPONENTS",
    "MIN_PROGRAM_TEXEL_OFFSET", "UNIFORM_BUFFER_OFFSET_ALIGNMENT",
]
FRAMEBUFFER_PARAMS = [
    "RED_BITS", "GREEN_BITS", "BLUE_BITS", "ALPHA_BITS", "DEPTH_BITS", "STENCIL_BITS", "SAMPLES",
    "SAMPLE_BUFFERS", "IMPLEMENTATION_COLOR_READ_FORMAT", "IMPLEMENTATION_COLOR_READ_TYPE",
]
# Extensions the guard may hide, first one the profile lists. The probe itself
# needs WEBGL_debug_renderer_info, EXT_texture_filter_anisotropic and
# WEBGL_lose_context, so those stay.
REMOVABLE = {
    "webgl": ["EXT_shader_texture_lod", "OES_standard_derivatives", "WEBGL_multi_draw"],
    "webgl2": ["EXT_texture_norm16", "WEBGL_multi_draw", "OES_draw_buffers_indexed"],
}

NO_ERROR, INVALID_ENUM, INVALID_VALUE = 0, 0x0500, 0x0501

ENFORCE_SCRIPT = """async (arg) => {
  const mk = (w, h) => {
    if (typeof document !== "undefined") {
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      return c;
    }
    return new OffscreenCanvas(w, h);
  };
  const out = {};
  for (const kind of ["webgl", "webgl2"]) {
    const a = arg[kind];
    const gl = mk(4, 4).getContext(kind);
    if (!gl) { out[kind] = { error: "no " + kind + " context" }; continue; }
    const r = {};
    gl.getError();
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, a.maxTextureSize + 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    r.texImageAboveMax = gl.getError();
    gl.activeTexture(gl.TEXTURE0 + a.maxCombinedUnits);
    r.activeTextureAtMax = gl.getError();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.vertexAttribPointer(a.maxVertexAttribs, 1, gl.FLOAT, false, 0, 0);
    r.vertexAttribAtMax = gl.getError();
    r.removedExtensionNull = gl.getExtension(a.removed) === null;
    if (a.removed === "EXT_shader_texture_lod") {
      const s = gl.createShader(gl.FRAGMENT_SHADER);
      gl.shaderSource(s, "#extension GL_EXT_shader_texture_lod : require\\nprecision mediump float;\\nvoid main() { gl_FragColor = vec4(1.0); }");
      gl.compileShader(s);
      r.lodShaderCompiles = !!gl.getShaderParameter(s, gl.COMPILE_STATUS);
    }
    const big = mk(a.viewportWidth + 100, 1).getContext(kind);
    r.drawingBufferWidth = big ? big.drawingBufferWidth : null;
    for (const g of [gl, big]) {
      const lose = g && g.getExtension("WEBGL_lose_context");
      if (lose) lose.loseContext();
    }
    out[kind] = r;
  }
  return out;
}"""


def other_renderer(renderer: str) -> str:
    """The same ANGLE string with a different device name."""
    if renderer.startswith("ANGLE (") and renderer.count(", ") >= 2:
        vendor, device, rest = renderer[len("ANGLE ("):].split(", ", 2)
        return f"ANGLE ({vendor}, {device} Ti, {rest}"
    return renderer + " Ti"


def other_vendor(vendor: str) -> str:
    if vendor.endswith(")") and "(" in vendor:
        head, inner = vendor[:-1].split("(", 1)
        if " " in inner:
            return f"{head}({inner.split()[0]})"
    return vendor + " Inc."


def mutate(base: dict, host: dict) -> tuple[dict, dict]:
    """The guard's profile and, per kind, the extension it removed."""
    profile = copy.deepcopy(base)
    # The renamed WebGL strings below change what the GL identity derives to:
    # leave it to be derived rather than keep the capture's, which would
    # contradict them.
    for field in ("gl_vendor", "gl_renderer", "vendor_name", "device_name"):
        profile["gpu"].pop(field, None)
    removed = {}
    for kind in KINDS:
        w = profile["gpu"][kind]
        h = host[kind]["params"]
        w["unmasked_renderer"] = other_renderer(w["unmasked_renderer"])
        w["unmasked_vendor"] = other_vendor(w["unmasked_vendor"])
        choice = next((e for e in REMOVABLE[kind] if e in w["extensions"]), None)
        if choice is None:
            raise GuardFailure(f"the profile's {kind} lists none of {REMOVABLE[kind]}, so the guard has no extension to hide")
        removed[kind] = choice
        w["extensions"] = [e for e in w["extensions"] if e != removed[kind]]
        p = w["params"]
        half = h["MAX_TEXTURE_SIZE"] // 2
        p["MAX_TEXTURE_SIZE"] = half
        p["MAX_CUBE_MAP_TEXTURE_SIZE"] = min(h["MAX_CUBE_MAP_TEXTURE_SIZE"], half)
        p["MAX_RENDERBUFFER_SIZE"] = min(h["MAX_RENDERBUFFER_SIZE"], half)
        p["MAX_VIEWPORT_DIMS"] = [v // 2 for v in h["MAX_VIEWPORT_DIMS"]]
        p["MAX_VERTEX_ATTRIBS"] = 8
        p["MAX_COMBINED_TEXTURE_IMAGE_UNITS"] = 16
        p["ALIASED_LINE_WIDTH_RANGE"] = [1, 1]
        if kind == "webgl2":
            p["MAX_3D_TEXTURE_SIZE"] = h["MAX_3D_TEXTURE_SIZE"] // 2
            p["MAX_ELEMENT_INDEX"] = h["MAX_ELEMENT_INDEX"] - 1  # an int64 limit
            p["MAX_TEXTURE_LOD_BIAS"] = h["MAX_TEXTURE_LOD_BIAS"] - 1  # the float one
            # A minimum and an alignment are backed above the host's value.
            p["MIN_PROGRAM_TEXEL_OFFSET"] = h["MIN_PROGRAM_TEXEL_OFFSET"] // 2
            p["UNIFORM_BUFFER_OFFSET_ALIGNMENT"] = h["UNIFORM_BUFFER_OFFSET_ALIGNMENT"] * 2
        for shader in ("VERTEX_SHADER", "FRAGMENT_SHADER"):
            for precision in ("LOW_INT", "MEDIUM_INT"):
                w["precision"][f"{shader}.{precision}"] = {"rangeMin": 15, "rangeMax": 14, "precision": 0}
        w["max_anisotropy"] = 8
    return profile, removed


def enforce_arg(profile: dict, removed: dict) -> dict:
    arg = {}
    for kind in KINDS:
        p = profile["gpu"][kind]["params"]
        arg[kind] = {
            "maxTextureSize": p["MAX_TEXTURE_SIZE"],
            "maxCombinedUnits": p["MAX_COMBINED_TEXTURE_IMAGE_UNITS"],
            "maxVertexAttribs": p["MAX_VERTEX_ATTRIBS"],
            "viewportWidth": p["MAX_VIEWPORT_DIMS"][0],
            "removed": removed[kind],
        }
    return arg


def precondition(base: dict, host: dict, headless: bool) -> None:
    for kind in KINDS:
        section = host.get(kind) or {}
        if not section.get("supported"):
            raise GuardSkip(f"{kind} is unavailable on this host ({section}); the guard needs the profile's GPU class")
        want = base["gpu"][kind]["unmasked_renderer"]
        if section.get("unmaskedRenderer") != want:
            mode = "headless" if headless else "headful"
            raise GuardSkip(
                f"{kind} here ({mode}) renders with {section.get('unmaskedRenderer')!r}, not the profile's {want!r}: "
                "the guard mutates limits downward from the capture class and needs that host"
            )
        missing = sorted(set(base["gpu"][kind]["extensions"]) - set(section.get("extensions") or []))
        if missing:
            raise GuardSkip(f"{kind} here lacks profile extensions {missing}; the guard needs the profile's GPU class")


def expect_subset_everywhere(d: dict, path: str, expected: dict, label: str) -> None:
    bad = {}
    for ctx in ALL_CONTEXTS:
        actual = _get(d["contexts"].get(ctx, {}), path)
        diffs = {k: (actual.get(k) if isinstance(actual, dict) else None, v) for k, v in expected.items()
                 if not isinstance(actual, dict) or actual.get(k) != v}
        if diffs:
            bad[ctx] = diffs
    if bad:
        raise GuardFailure(f"{label} (got, want): {bad}")
    print(f"PASS: {label} in {len(ALL_CONTEXTS)} contexts")


def expect_like_plain(d: dict, plain: dict, path: str, label: str) -> None:
    bad = {}
    for ctx in ALL_CONTEXTS:
        got, want = _get(d["contexts"].get(ctx, {}), path), _get(plain["contexts"].get(ctx, {}), path)
        if got != want:
            bad[ctx] = (got, want)
    if bad:
        raise GuardFailure(f"{label} differs from the plain run (got, plain): {bad}")
    print(f"PASS: {label} equals the plain run in {len(ALL_CONTEXTS)} contexts")


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful

    plain = checked(dump(args.binary, None, headless=headless, extra_args=host_gpu_args(headless)), "no profile")
    host = plain["contexts"]["main"]
    precondition(base, host, headless)
    profile, removed = mutate(base, host)
    arg = enforce_arg(profile, removed)

    plain_enforce = session(args.binary, None, headless=headless, extra_args=host_gpu_args(headless), script=ENFORCE_SCRIPT, arg=arg)

    def plain_check(r: dict) -> str | None:
        for kind in KINDS:
            k = r.get(kind) or {}
            if (k.get("texImageAboveMax"), k.get("activeTextureAtMax"), k.get("vertexAttribAtMax")) != (NO_ERROR,) * 3:
                return f"{kind}: the host rejects the guard's lowered limits: {k}"
            if k.get("removedExtensionNull") or k.get("lodShaderCompiles") is False:
                return f"{kind}: {removed[kind]} does not work on the host: {k}"
            if not k.get("drawingBufferWidth") or k["drawingBufferWidth"] <= arg[kind]["viewportWidth"]:
                return f"{kind}: the host clamps the drawing buffer already: {k}"
        return None

    try:
        expect_results(plain_enforce["results"], "precondition: the guard's lowered limits are within the host's", plain_check)
    except GuardFailure as exc:
        raise GuardFailure(f"vacuous: {exc}") from exc

    path = write_profile(profile, work, "webgl")
    d = checked(dump(args.binary, str(path), headless=headless), "mutated profile")
    for kind in KINDS:
        w = profile["gpu"][kind]
        expect_everywhere(d, f"{kind}.unmaskedVendor", w["unmasked_vendor"], label=f"{kind} UNMASKED_VENDOR_WEBGL")
        expect_everywhere(d, f"{kind}.unmaskedRenderer", w["unmasked_renderer"], label=f"{kind} UNMASKED_RENDERER_WEBGL")
        for name in ("VENDOR", "RENDERER", "VERSION", "SHADING_LANGUAGE_VERSION"):
            expect_everywhere(d, f"{kind}.params.{name}", host[kind]["params"][name], label=f"{kind} {name} (build constant)")
        expect_everywhere(d, f"{kind}.extensions", sorted(w["extensions"]), label=f"{kind} extensions without {removed[kind]}")
        owned = WEBGL_PARAMS + (WEBGL2_PARAMS if kind == "webgl2" else [])
        expect_subset_everywhere(d, f"{kind}.params", {k: w["params"][k] for k in owned}, f"{kind} limits ({len(owned)})")
        expect_everywhere(d, f"{kind}.precision", w["precision"], label=f"{kind} shader precision formats")
        expect_everywhere(d, f"{kind}.maxAnisotropy", w["max_anisotropy"], label=f"{kind} MAX_TEXTURE_MAX_ANISOTROPY_EXT")
        for name in FRAMEBUFFER_PARAMS:
            expect_like_plain(d, plain, f"{kind}.params.{name}", f"{kind} {name}")
        expect_like_plain(d, plain, f"{kind}.contextAttributes", f"{kind} getContextAttributes()")

    run = session(args.binary, path, headless=headless, script=ENFORCE_SCRIPT, arg=arg)

    def enforced(r: dict) -> str | None:
        for kind in KINDS:
            k = r.get(kind) or {}
            a = arg[kind]
            p = profile["gpu"][kind]["params"]
            want = {
                "texImageAboveMax": INVALID_VALUE,
                "activeTextureAtMax": INVALID_ENUM,
                "vertexAttribAtMax": INVALID_VALUE,
                "removedExtensionNull": True,
                "drawingBufferWidth": min(a["viewportWidth"], p["MAX_TEXTURE_SIZE"], p["MAX_RENDERBUFFER_SIZE"]),
            }
            if a["removed"] == "EXT_shader_texture_lod":
                want["lodShaderCompiles"] = False
            wrong = {key: (k.get(key), value) for key, value in want.items() if k.get(key) != value}
            if wrong:
                return f"{kind} (got, want): {wrong}"
        return None

    expect_results(run["results"], "Blink enforces the claimed limits and hides the removed extension", enforced)
    expect_no_unbacked(run["stderr"], "mutated profile")

    splits = {k: v for k, v in cross_context_report(d, baseline=plain)["disagreements"].items() if k.startswith(KINDS)}
    if splits:
        raise GuardFailure(f"WebGL cross-context splits the plain run does not have: {dict(list(splits.items())[:5])}")
    print("PASS: no WebGL cross-context split beyond the plain run's")


finish(main)
