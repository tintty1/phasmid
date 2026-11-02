"""Guard for patches/phasmid/graphics/webgl-fallback.patch and
patches/phasmid/graphics/headless-gpu.patch.

With a profile, a host without a usable
GPU still offers WebGL, rendered on SwiftShader. --disable-gpu stands in for
such a host. Headless, the run without a profile takes --enable-gpu, as a
profile run renders: stock headless otherwise forces SwiftShader WebGL.

  0. precondition, no profile, --disable-gpu: no WebGL context in the main
     frame (stock behaviour without --enable-unsafe-swiftshader), so the
     check below is not vacuous;
  1. with the profile, --disable-gpu: getContext('webgl') succeeds in all six
     contexts (OffscreenCanvas in the workers) and UNMASKED_RENDERER_WEBGL is
     the profile's, so the WebGL patch still applies on SwiftShader.

With a profile, headless renders as headful does. Headless only (headful has
nothing to compare against):

  2. precondition, no profile, --enable-gpu: the GPU process renders on a
     hardware ANGLE backend (CDP glImplementationParts names no SwiftShader),
     and without the switch it renders on SwiftShader (stock headless), else
     the host has no GPU and this part is reported as not observable;
  3. with the profile and no switch, headless renders as with --enable-gpu,
     the path a headful run takes: glImplementationParts equals the profile
     run's with --enable-gpu, and in all six contexts the WebGL1 and WebGL2
     extension lists and readPixels hashes equal that run's; when a display
     is available (DISPLAY or WAYLAND_DISPLAY), they also equal a headful
     profile run's.
"""

from __future__ import annotations

import os

from _gpu import checked, host_gpu_args
from consistency.compare import _get
from helpers import ALL_CONTEXTS, GuardFailure, GuardSkip, dump, expect_everywhere, finish, load_profile, parse_args

NO_GPU = ["--disable-gpu"]
ENABLE_GPU = ["--enable-gpu"]
# Keys whose value follows the GL backend the GPU process renders on.
GL_PATH_KEYS = ("webgl.extensions", "webgl2.extensions", "webgl.readPixels", "webgl2.readPixels")


def gl_path(d: dict) -> str:
    aux = ((d.get("system_info") or {}).get("gpu") or {}).get("auxAttributes") or {}
    return aux.get("glImplementationParts") or ""


def expect_same_gl_path(d: dict, ref: dict, label: str) -> None:
    if gl_path(d) != gl_path(ref):
        raise GuardFailure(f"{label}: glImplementationParts {gl_path(d)!r}, want {gl_path(ref)!r}")
    bad = {}
    for ctx in ALL_CONTEXTS:
        for key in GL_PATH_KEYS:
            got, want = _get(d["contexts"].get(ctx, {}), key), _get(ref["contexts"].get(ctx, {}), key)
            if got != want:
                bad[f"{ctx}.{key}"] = (got, want)
    if bad:
        raise GuardFailure(f"{label} (got, want): {bad}")
    print(f"PASS: {label}: {gl_path(d)}, same WebGL extensions and readPixels in {len(ALL_CONTEXTS)} contexts")


def check_headless_gpu(args) -> None:
    plain_gpu = checked(dump(args.binary, None, headless=True, extra_args=ENABLE_GPU), "no profile, --enable-gpu")
    if "swiftshader" in gl_path(plain_gpu) or not gl_path(plain_gpu):
        print(f"NOT OBSERVABLE: checks 2-3: without a profile headless --enable-gpu renders on {gl_path(plain_gpu)!r}: "
              "the host has no usable GPU, so headless and headful both fall back to SwiftShader")
        return
    plain = checked(dump(args.binary, None, headless=True), "no profile")
    if "swiftshader" not in gl_path(plain):
        raise GuardFailure(f"vacuous: without a profile stock headless renders on {gl_path(plain)!r}, not SwiftShader")
    print(f"PASS: precondition: without a profile headless renders on {gl_path(plain)!r}, with --enable-gpu on "
          f"{gl_path(plain_gpu)!r}")

    run = checked(dump(args.binary, args.profile, headless=True), "profile, headless")
    ref = checked(dump(args.binary, args.profile, headless=True, extra_args=ENABLE_GPU), "profile, headless, --enable-gpu")
    expect_same_gl_path(run, ref, "headless with the profile renders as with --enable-gpu")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("NOT OBSERVABLE: check 3 against headful: no display (DISPLAY, WAYLAND_DISPLAY)")
        return
    headful = checked(dump(args.binary, args.profile, headless=False), "profile, headful")
    expect_same_gl_path(run, headful, "headless with the profile renders as headful")


def main() -> None:
    args = parse_args(__doc__)
    profile = load_profile(args.profile)
    headless = not args.headful

    plain = dump(args.binary, None, headless=headless, extra_args=NO_GPU + host_gpu_args(headless))
    if _get(plain["contexts"]["main"], "webgl.supported"):
        raise GuardSkip("without a profile --disable-gpu still offers WebGL on this host; the fallback is not observable")
    print("PASS: precondition: no profile, --disable-gpu: no WebGL context")

    d = dump(args.binary, args.profile, headless=headless, extra_args=NO_GPU)
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed: {d['context_errors']}")
    expect_everywhere(d, "webgl.supported", True, ALL_CONTEXTS, label="WebGL context with --disable-gpu")
    expect_everywhere(d, "webgl.unmaskedRenderer", profile["gpu"]["webgl"]["unmasked_renderer"], ALL_CONTEXTS,
                      label="gpu.webgl.unmasked_renderer on SwiftShader")

    if headless:
        check_headless_gpu(args)


finish(main)
