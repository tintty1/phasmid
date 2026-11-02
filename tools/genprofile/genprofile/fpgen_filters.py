"""Which fpgen records are clean stock Chrome sessions on real hardware.

fpgen's collector filtered nothing: software rasterizers, Brave's farbled
WebGL, anti-detect output and zoomed pages all sit in the same tables as stock
Chrome. The checks run in the order the extractor lists them, before any
count. They take schema-shaped values
(``params`` by GL constant name, extension name lists), so the same check
runs on a capture.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# ANGLE's unmasked renderer: "ANGLE (<vendor>, <device>, <backend>)". The device
# itself may contain commas only inside parentheses (Vulkan device strings).
_ANGLE = re.compile(r"ANGLE \((?P<vendor>[^,()]+), (?P<device>.+), (?P<backend>[^,()]+)\)")
_SOFTWARE = ("SwiftShader", "llvmpipe", "softpipe", "Microsoft Basic Render Driver")
# Linux GL vendors as ANGLE names them, and the driver stack each implies.
_STACKS = {"NVIDIA Corporation": "nvidia", "NVIDIA": "nvidia", "Intel": "mesa-intel",
           "Intel Open Source Technology Center": "mesa-intel", "AMD": "mesa-amd"}

# Brave's balanced farbling subtracts 0 or 1 from exactly these WebGL2 limits
# (brave-core webgl2_rendering_context_base.cc, FarbleGLIntParameter).
BRAVE_FARBLED = (
    "MAX_VERTEX_UNIFORM_COMPONENTS", "MAX_VERTEX_UNIFORM_BLOCKS", "MAX_VERTEX_OUTPUT_COMPONENTS",
    "MAX_VARYING_COMPONENTS", "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS", "MAX_FRAGMENT_UNIFORM_COMPONENTS",
    "MAX_FRAGMENT_UNIFORM_BLOCKS", "MAX_FRAGMENT_INPUT_COMPONENTS", "MAX_UNIFORM_BUFFER_BINDINGS",
    "MAX_COMBINED_UNIFORM_BLOCKS", "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS", "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS",
)
# ES 3.0 identities ANGLE keeps on every backend; one farbled limit breaks them.
_TIMES_FOUR = (("MAX_VERTEX_UNIFORM_COMPONENTS", "MAX_VERTEX_UNIFORM_VECTORS"),
               ("MAX_FRAGMENT_UNIFORM_COMPONENTS", "MAX_FRAGMENT_UNIFORM_VECTORS"),
               ("MAX_VARYING_COMPONENTS", "MAX_VARYING_VECTORS"))
_MULTIPLE_OF_FOUR = ("MAX_VERTEX_OUTPUT_COMPONENTS", "MAX_FRAGMENT_INPUT_COMPONENTS",
                     "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS")

# Every extension Blink registers (RegisterContextExtensions in
# third_party/blink/renderer/modules/webgl/webgl_rendering_context.cc and
# webgl2_rendering_context.cc). Brave adds one invented name behind
# kWebGLBalancedFingerprintingProtection; anything outside this set is that or worse.
KNOWN_EXTENSIONS = frozenset({
    "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_clip_control", "EXT_color_buffer_float",
    "EXT_color_buffer_half_float", "EXT_conservative_depth", "EXT_depth_clamp", "EXT_disjoint_timer_query",
    "EXT_disjoint_timer_query_webgl2", "EXT_float_blend", "EXT_frag_depth", "EXT_polygon_offset_clamp",
    "EXT_render_snorm", "EXT_shader_texture_lod", "EXT_sRGB", "EXT_texture_compression_bptc",
    "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic", "EXT_texture_mirror_clamp_to_edge",
    "EXT_texture_norm16", "KHR_parallel_shader_compile", "NV_shader_noperspective_interpolation",
    "OES_draw_buffers_indexed", "OES_element_index_uint", "OES_fbo_render_mipmap", "OES_sample_variables",
    "OES_shader_multisample_interpolation", "OES_standard_derivatives", "OES_texture_float",
    "OES_texture_float_linear", "OES_texture_half_float", "OES_texture_half_float_linear",
    "OES_vertex_array_object", "OVR_multiview2", "WEBGL_blend_func_extended", "WEBGL_clip_cull_distance",
    "WEBGL_color_buffer_float", "WEBGL_compressed_texture_astc", "WEBGL_compressed_texture_etc",
    "WEBGL_compressed_texture_etc1", "WEBGL_compressed_texture_pvrtc", "WEBGL_compressed_texture_s3tc",
    "WEBGL_compressed_texture_s3tc_srgb", "WEBGL_debug_renderer_info", "WEBGL_debug_shaders",
    "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_draw_instanced_base_vertex_base_instance",
    "WEBGL_lose_context", "WEBGL_multi_draw", "WEBGL_multi_draw_instanced_base_vertex_base_instance",
    "WEBGL_polygon_mode", "WEBGL_provoking_vertex", "WEBGL_render_shared_exponent",
    "WEBGL_shader_pixel_local_storage", "WEBGL_stencil_texturing",
})
# Registered as kDraftExtension: exposed only with --enable-webgl-draft-extensions.
DRAFT_EXTENSIONS = frozenset({
    "WEBGL_draw_instanced_base_vertex_base_instance", "WEBGL_multi_draw_instanced_base_vertex_base_instance",
    "WEBGL_shader_pixel_local_storage",
})


def parse_angle(renderer: str) -> tuple[str, str, str] | None:
    """(vendor, device, backend) of an ANGLE renderer string."""
    m = _ANGLE.fullmatch(renderer or "")
    return (m["vendor"], m["device"], m["backend"]) if m else None


def is_software(renderer: str) -> bool:
    return any(s in (renderer or "") for s in _SOFTWARE)


def brave_renderer(renderer: str) -> bool:
    """Brave reports the unmasked strings as "Brave" behind kWebGLBalancedFingerprintingProtection."""
    return renderer == "Brave"


def renderer_class(renderer: str) -> str | None:
    """The host class of a Linux renderer string: driver stack, ANGLE backend and,
    on GL, the flavour (``nvidia/opengl/gles``). The flavour is the renderer's
    suffix: EGL contexts report OpenGL ES, GLX ones desktop OpenGL. None for
    software rasterizers and for anything that is not a Linux GPU string."""
    parts = parse_angle(renderer)
    if parts is None or is_software(renderer):
        return None
    vendor, device, backend = parts
    stack = _STACKS.get(vendor)
    if stack is None:
        return None
    if device.startswith("Vulkan "):
        return f"{stack}/vulkan"
    if stack == "nvidia" and (vendor != "NVIDIA Corporation" or not device.endswith("/PCIe/SSE2")):
        return None  # the proprietary GL driver's naming; nouveau and NVK name devices differently
    if backend.startswith("OpenGL ES "):
        return f"{stack}/opengl/gles"
    if backend.startswith("OpenGL "):
        return f"{stack}/opengl/gl"
    return None


def brave_identity(params: dict[str, Any]) -> list[str]:
    """The WebGL2 identities a farbled record breaks; a missing limit counts as broken."""
    broken = []
    for components, vectors in _TIMES_FOUR:
        c, v = params.get(components), params.get(vectors)
        if not isinstance(c, int) or not isinstance(v, int) or c != 4 * v:
            broken.append(components)
    for name in _MULTIPLE_OF_FOUR:
        value = params.get(name)
        if not isinstance(value, int) or value % 4:
            broken.append(name)
    return broken


def brave_mode_minus_one(family: list[tuple[dict[str, Any], int]], min_sessions: int) -> list[bool]:
    """Brave records that pass the identities: within one host class of at least
    ``min_sessions`` sessions, every farbled limit is at the class's mode or one
    below it, and at least one is one below. ``family`` is (WebGL2 params,
    sessions) per record; the result is one flag per record."""
    if sum(n for _, n in family) < min_sessions:
        return [False] * len(family)
    modes = {}
    for name in BRAVE_FARBLED:
        counts: Counter[Any] = Counter()
        for params, n in family:
            counts[params.get(name)] += n
        modes[name] = min(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))[0]
    flags = []
    for params, _ in family:
        below = [modes[n] - params[n] for n in BRAVE_FARBLED
                 if isinstance(params.get(n), int) and isinstance(modes[n], int)]
        flags.append(bool(below) and all(d in (0, 1) for d in below) and 1 in below)
    return flags


def draft_extensions(extensions: list[str]) -> list[str]:
    return sorted(set(extensions) & DRAFT_EXTENSIONS)


def unknown_extensions(extensions: list[str]) -> list[str]:
    return sorted(set(extensions) - KNOWN_EXTENSIONS)


def zoomed(dpr: Any, steps: tuple[float, ...]) -> bool:
    """A devicePixelRatio off the OS's scale steps is page zoom, not a display."""
    return dpr not in steps


def google_voices(voices: Any) -> bool:
    """Google Chrome's network voices; Brave and Chromium have none."""
    return isinstance(voices, list) and any(
        isinstance(v, dict) and str(v.get("name", "")).startswith("Google ") and v.get("localService") is False
        for v in voices)


def widevine(drm: Any) -> bool:
    return isinstance(drm, dict) and "com.widevine.alpha" in (drm.get("supported") or [])
