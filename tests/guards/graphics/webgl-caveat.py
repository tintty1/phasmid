"""Guard for patches/phasmid/graphics/webgl-caveat.patch.

A profile claiming a hardware GPU gets the
WebGL context a page asks for with failIfMajorPerformanceCaveat, even when the
host has no usable GPU and WebGL runs on SwiftShader. --disable-gpu with
--enable-unsafe-swiftshader stands in for such a host on any machine.
Headless, the run without a profile takes --enable-gpu, as a profile run
renders: stock headless otherwise runs the whole GPU process on
--use-angle=swiftshader-webgl, which already lets the request through.

  0. precondition, no profile: a plain context is created, so WebGL runs on
     SwiftShader, and the caveat request fails with webglcontextcreationerror
     (stock), so the check below is not vacuous;
  1. with the profile (a hardware gpu.angle_backend): the caveat request
     returns a WebGL1 and a WebGL2 context and fires no creation error in the
     main frame, a cross-origin iframe and a dedicated worker (OffscreenCanvas);
  2. with gpu.angle_backend "swiftshader": the caveat request fails again, as
     on the software device that profile claims.
"""

from __future__ import annotations

import copy
from pathlib import Path

from _gpu import expect_results, host_gpu_args, session, write_profile
from helpers import GuardFailure, GuardSkip, finish, load_profile, parse_args

FLAGS = ["--disable-gpu", "--enable-unsafe-swiftshader"]

# Requests each context kind with and without the caveat flag and records any
# creation error the caveat requests raise.
CAVEAT_SCRIPT = """async () => {
  const canvas = () => typeof document !== "undefined" ? document.createElement("canvas") : new OffscreenCanvas(16, 16);
  const out = { plain: !!canvas().getContext("webgl") };
  for (const kind of ["webgl", "webgl2"]) {
    const c = canvas();
    let message = null;
    c.addEventListener("webglcontextcreationerror", (e) => { message = e.statusMessage; });
    out[kind] = { created: !!c.getContext(kind, { failIfMajorPerformanceCaveat: true }), message };
  }
  return out;
}"""


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    if base["gpu"]["angle_backend"] == "swiftshader":
        raise GuardSkip("the profile claims a software GPU; the patch changes nothing for it")
    work = Path(args.work_dir)
    headless = not args.headful

    plain = session(args.binary, None, headless=headless, extra_args=FLAGS + host_gpu_args(headless), script=CAVEAT_SCRIPT)

    if all(isinstance(r, dict) and r.get("plain") and (r.get("webgl") or {}).get("created") for r in plain["results"].values()):
        raise GuardFailure("vacuous: without a profile the caveat request already succeeds, so WebGL does not run "
                           "on the SwiftShader fallback here")

    def refused(r: dict) -> str | None:
        if not r.get("plain"):
            return f"no plain WebGL context, so WebGL is not on SwiftShader: {r}"
        for kind in ("webgl", "webgl2"):
            if r[kind]["created"] or not r[kind]["message"]:
                return f"{kind}: the caveat request was not refused with a creation error: {r[kind]}"
        return None

    try:
        expect_results(plain["results"], "precondition: without a profile SwiftShader refuses the caveat request", refused)
    except GuardFailure as exc:
        raise GuardFailure(f"vacuous: {exc}") from exc

    run = session(args.binary, write_profile(base, work, "hardware"), headless=headless, extra_args=FLAGS, script=CAVEAT_SCRIPT)

    def created(r: dict) -> str | None:
        for kind in ("webgl", "webgl2"):
            if not r[kind]["created"] or r[kind]["message"]:
                return f"{kind}: the caveat request did not return a context cleanly: {r[kind]}"
        return None

    expect_results(run["results"], f"gpu.angle_backend {base['gpu']['angle_backend']}: the caveat request returns a context", created)

    software = copy.deepcopy(base)
    software["gpu"]["angle_backend"] = "swiftshader"
    # A SwiftShader claim names its own driver (SwANGLE); leave the GL identity
    # and driver for the browser to derive.
    for field in ("gl_vendor", "gl_renderer", "vendor_name", "device_name", "driver_vendor", "driver_version"):
        software["gpu"].pop(field, None)
    soft = session(args.binary, write_profile(software, work, "software"), headless=headless, extra_args=FLAGS, script=CAVEAT_SCRIPT)
    expect_results(soft["results"], "gpu.angle_backend swiftshader: the caveat request is refused as in stock", refused)


finish(main)
