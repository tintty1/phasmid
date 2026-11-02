"""The hardware-owned WebGL surface of a schema-shaped ``gpu`` section, and its host class.

On Linux the surface is set by the driver stack, not by the GPU model:
within a host class every GPU the driver supports reports the same extensions, limits and precision formats.
The surface is what a claimed GPU must share with a capture byte for byte:
per WebGL context, the sorted extensions, the consistency probe's limits, the
shader precision formats, anisotropy, MAX_DRAW_BUFFERS_WEBGL and the context
attributes. Left out are the identity and version strings, MAX_ELEMENT_INDEX
(build-owned: ANGLE caps it per release) and preserveDrawingBuffer (the probe
asks for it).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .fpgen_filters import parse_angle, renderer_class

# The limits tests/consistency/web/probe.js (GL_PARAMS) reads, minus its four strings.
PROBE_PARAMS = (
    "MAX_TEXTURE_SIZE", "MAX_CUBE_MAP_TEXTURE_SIZE", "MAX_RENDERBUFFER_SIZE", "MAX_VIEWPORT_DIMS",
    "MAX_VERTEX_ATTRIBS", "MAX_VERTEX_UNIFORM_VECTORS", "MAX_VARYING_VECTORS", "MAX_FRAGMENT_UNIFORM_VECTORS",
    "MAX_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_COMBINED_TEXTURE_IMAGE_UNITS",
    "ALIASED_LINE_WIDTH_RANGE", "ALIASED_POINT_SIZE_RANGE",
    "RED_BITS", "GREEN_BITS", "BLUE_BITS", "ALPHA_BITS", "DEPTH_BITS", "STENCIL_BITS",
    "SAMPLES", "SAMPLE_BUFFERS", "SUBPIXEL_BITS", "IMPLEMENTATION_COLOR_READ_FORMAT", "IMPLEMENTATION_COLOR_READ_TYPE",
    "MAX_3D_TEXTURE_SIZE", "MAX_ARRAY_TEXTURE_LAYERS", "MAX_COLOR_ATTACHMENTS", "MAX_DRAW_BUFFERS", "MAX_ELEMENT_INDEX",
    "MAX_ELEMENTS_INDICES", "MAX_ELEMENTS_VERTICES", "MAX_FRAGMENT_INPUT_COMPONENTS", "MAX_FRAGMENT_UNIFORM_BLOCKS",
    "MAX_FRAGMENT_UNIFORM_COMPONENTS", "MAX_PROGRAM_TEXEL_OFFSET", "MIN_PROGRAM_TEXEL_OFFSET", "MAX_SAMPLES",
    "MAX_SERVER_WAIT_TIMEOUT", "MAX_TEXTURE_LOD_BIAS", "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS",
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS", "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS", "MAX_UNIFORM_BLOCK_SIZE",
    "MAX_UNIFORM_BUFFER_BINDINGS", "MAX_VARYING_COMPONENTS", "MAX_VERTEX_OUTPUT_COMPONENTS", "MAX_VERTEX_UNIFORM_BLOCKS",
    "MAX_VERTEX_UNIFORM_COMPONENTS", "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS", "MAX_COMBINED_UNIFORM_BLOCKS",
    "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS", "UNIFORM_BUFFER_OFFSET_ALIGNMENT", "MAX_CLIENT_WAIT_TIMEOUT_WEBGL",
)
BUILD_OWNED_PARAMS = frozenset({"MAX_ELEMENT_INDEX"})
PROBE_ATTRIBUTES = frozenset({"preserveDrawingBuffer"})


def _number(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, list):
        return [_number(x) for x in v]
    return v


def _context(ctx: dict[str, Any]) -> dict[str, Any]:
    params = ctx["params"]
    return {
        "extensions": sorted(ctx["extensions"]),
        "params": {k: _number(params[k]) for k in PROBE_PARAMS if k in params and k not in BUILD_OWNED_PARAMS},
        "precision": ctx["precision"],
        "max_anisotropy": _number(ctx["max_anisotropy"]),
        "max_draw_buffers": ctx.get("max_draw_buffers"),
        "context_attributes": {k: v for k, v in ctx["context_attributes"].items() if k not in PROBE_ATTRIBUTES},
    }


def surface(gpu: dict[str, Any]) -> dict[str, Any]:
    return {"webgl": _context(gpu["webgl"]), "webgl2": _context(gpu["webgl2"])}


def surface_sha256(gpu: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(surface(gpu), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def host_class(gpu: dict[str, Any]) -> str | None:
    """The capture's host class, from the GL strings chrome://gpu reported."""
    parts = parse_angle(gpu.get("gl_renderer", ""))
    if parts is None or gpu.get("gl_vendor") != f"Google Inc. ({parts[0]})":
        return None
    return renderer_class(gpu["gl_renderer"])
