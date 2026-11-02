"""Guard for patches/phasmid/graphics/webgpu.patch.

requestAdapter() resolves to the profile's adapter in every context, backed
by the host's hardware adapter where there is one and by the SwiftShader
fallback where there is none.

Hardware part, on a host with the profile's WebGPU adapter (the capture
host class: Wayland, NVIDIA 535.183 or later, headful; headless renders WebGL on
the host GPU but has no hardware WebGPU adapter); elsewhere it is skipped
and the fallback part still runs:

  0. precondition, no profile: the host's adapter is the profile's (vendor,
     architecture, subgroup sizes), it has every profile feature and limit,
     the feature the guard drops can be required, and a required limit one
     past the host's is rejected (that rejection is stock Dawn's text);
  1. with the profile mutated (one feature dropped; maxTextureDimension2D,
     maxBufferSize and maxStorageBuffersInFragmentStage lowered a tier;
     architecture renamed), in all six contexts: adapter info (vendor,
     architecture, subgroup sizes, isFallbackAdapter false, device and
     description empty), the sorted features and every limit are the
     profile's, getPreferredCanvasFormat() is the profile's and
     wgslLanguageFeatures stays the build's (the plain run's);
  2. in the main frame, a cross-origin iframe and a dedicated worker:
     requiring the dropped feature is a TypeError; a required
     maxTextureDimension2D one past the claim is rejected with the stock
     message carrying the claimed numbers, and the claim itself is accepted;
     a compute shader reads a subgroup_size within the claimed range;
  3. no claim was reported as unbacked;
  4. gpu.webgpu.available false: no adapter in any context, and
     getPreferredCanvasFormat() still answers;
  5. a Linux profile claiming bgra8unorm is refused at startup, naming
     gpu.webgpu.preferred_canvas_format (Linux Chrome prefers rgba8unorm, so
     the format cannot be mutated on this platform).

Fallback part, with --use-angle=swiftshader (the GL a host under Xvfb or
without a GPU gets; with a profile headless renders on the host GPU too, so
headless takes the switch as headful does):

  6. precondition, no profile: no adapter in this mode (stock refuses the
     fallback), and with --enable-unsafe-webgpu the SwiftShader adapter
     exists (isFallbackAdapter true); its features and limits are the
     backing, and the profile claims at least one feature and one maximum
     limit beyond them;
  7. with the profile, in all six contexts: an adapter whose info (vendor,
     architecture, device, description, subgroup sizes, isFallbackAdapter
     false), sorted features and every limit are the profile's;
  8. in the main frame, a cross-origin iframe and a dedicated worker:
     forceFallbackAdapter gets null; requiring a claimed feature the
     backing lacks rejects with OperationError "Disallowed feature
     requested.", a backed one succeeds; a required limit within the claim
     but beyond the backing rejects with "Disallowed limit requested.", the
     backing's value succeeds, one past the claim gets Dawn's message with
     the claimed numbers; a compute pass runs on the device;
  9. startup reports gpu.webgpu.adapter (the fallback backs the claim) and
     the unbacked feature as unbacked.
"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from _gpu import checked, expect_no_unbacked, expect_results, host_gpu_args, session, write_profile
from consistency.compare import _get
from helpers import ALL_CONTEXTS, GuardFailure, GuardSkip, dump, expect_everywhere, finish, load_profile, parse_args, run_binary, unbacked_warnings

# Features the guard may drop, first one the profile lists. Not "subgroups":
# the subgroup check needs it.
DROPPABLE = ["float32-blendable", "texture-compression-bc-sliced-3d", "timestamp-query", "dual-source-blending"]
LIMIT = "maxTextureDimension2D"

SCRIPT = """async (arg) => {
  const out = { hasGpu: !!navigator.gpu };
  if (!navigator.gpu) return out;
  out.preferredCanvasFormat = navigator.gpu.getPreferredCanvasFormat();
  const first = await navigator.gpu.requestAdapter();
  out.adapter = !!first;
  if (!first) return out;
  const attempt = async (desc) => {
    const adapter = await navigator.gpu.requestAdapter();
    try {
      const device = await adapter.requestDevice(desc);
      device.destroy();
      return { ok: true };
    } catch (e) {
      return { name: e.name, message: e.message };
    }
  };
  out.droppedFeature = await attempt({ requiredFeatures: [arg.dropped] });
  out.aboveLimit = await attempt({ requiredLimits: { [arg.limit]: arg.limitValue + 1 } });
  out.atLimit = await attempt({ requiredLimits: { [arg.limit]: arg.limitValue } });
  const adapter = await navigator.gpu.requestAdapter();
  const device = await adapter.requestDevice({ requiredFeatures: ["subgroups"] });
  const module = device.createShaderModule({ code: "enable subgroups;\\n" +
    "@group(0) @binding(0) var<storage, read_write> o: array<u32, 1>;\\n" +
    "@compute @workgroup_size(64) fn main(@builtin(subgroup_size) s: u32) { o[0] = s; }" });
  const pipeline = device.createComputePipeline({ layout: "auto", compute: { module, entryPoint: "main" } });
  const buffer = device.createBuffer({ size: 4, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC });
  const read = device.createBuffer({ size: 4, usage: GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST });
  const group = device.createBindGroup({ layout: pipeline.getBindGroupLayout(0), entries: [{ binding: 0, resource: { buffer } }] });
  const encoder = device.createCommandEncoder();
  const pass = encoder.beginComputePass();
  pass.setPipeline(pipeline);
  pass.setBindGroup(0, group);
  pass.dispatchWorkgroups(1);
  pass.end();
  encoder.copyBufferToBuffer(buffer, 0, read, 0, 4);
  device.queue.submit([encoder.finish()]);
  await read.mapAsync(GPUMapMode.READ);
  out.subgroupSize = new Uint32Array(read.getMappedRange())[0];
  read.unmap();
  device.destroy();
  return out;
}"""


# Runs in each context of the fallback part (arg: unbacked and backed
# feature, limit name, the claim and the backing's value).
FALLBACK_SCRIPT = """async (arg) => {
  const out = { hasGpu: !!navigator.gpu };
  if (!navigator.gpu) return out;
  const first = await navigator.gpu.requestAdapter();
  out.adapter = !!first;
  if (!first) return out;
  out.info = { vendor: first.info.vendor, architecture: first.info.architecture, isFallbackAdapter: first.info.isFallbackAdapter };
  out.features = [...first.features].sort();
  out.limits = {};
  for (const k in first.limits) out.limits[k] = first.limits[k];
  out.forceFallback = !!(await navigator.gpu.requestAdapter({ forceFallbackAdapter: true }));
  const attempt = async (desc) => {
    const adapter = await navigator.gpu.requestAdapter();
    try {
      const device = await adapter.requestDevice(desc);
      device.destroy();
      return { ok: true };
    } catch (e) {
      return { name: e.name, message: e.message };
    }
  };
  if (arg) {
    out.unbackedFeature = await attempt({ requiredFeatures: [arg.unbacked] });
    out.backedFeature = await attempt({ requiredFeatures: [arg.backed] });
    out.withinClaim = await attempt({ requiredLimits: { [arg.limit]: arg.claim } });
    out.atBacking = await attempt({ requiredLimits: { [arg.limit]: arg.backing } });
    out.aboveClaim = await attempt({ requiredLimits: { [arg.limit]: arg.claim + 1 } });
    out.aboveBacking = await attempt({ requiredLimits: { [arg.limit]: arg.backing + 1 } });
  }
  const adapter = await navigator.gpu.requestAdapter();
  const device = await adapter.requestDevice();
  const module = device.createShaderModule({ code:
    "@group(0) @binding(0) var<storage, read_write> o: array<u32, 1>;\\n" +
    "@compute @workgroup_size(1) fn main() { o[0] = 42u; }" });
  const pipeline = device.createComputePipeline({ layout: "auto", compute: { module, entryPoint: "main" } });
  const buffer = device.createBuffer({ size: 4, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC });
  const read = device.createBuffer({ size: 4, usage: GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST });
  const group = device.createBindGroup({ layout: pipeline.getBindGroupLayout(0), entries: [{ binding: 0, resource: { buffer } }] });
  const encoder = device.createCommandEncoder();
  const pass = encoder.beginComputePass();
  pass.setPipeline(pipeline);
  pass.setBindGroup(0, group);
  pass.dispatchWorkgroups(1);
  pass.end();
  encoder.copyBufferToBuffer(buffer, 0, read, 0, 4);
  device.queue.submit([encoder.finish()]);
  await read.mapAsync(GPUMapMode.READ);
  out.computed = new Uint32Array(read.getMappedRange())[0];
  read.unmap();
  device.destroy();
  return out;
}"""

# The fallback part's extra switches: SwiftShader GL, as on a host under Xvfb
# or without a GPU, headful and headless alike.
FALLBACK_GL = ["--use-angle=swiftshader"]
UNSAFE = ["--enable-unsafe-webgpu"]
REQUEST_DEVICE = "Failed to execute 'requestDevice' on 'GPUAdapter': "


def lowered_limits(host: dict) -> dict:
    """One tier down for a uint32 limit, the uint64 one and a compat one."""
    out = {LIMIT: host[LIMIT] // 2}
    two_gib = 2 * 1024 * 1024 * 1024
    out["maxBufferSize"] = two_gib if host["maxBufferSize"] > two_gib else host["maxBufferSize"] // 2
    if host["maxStorageBuffersInFragmentStage"] > 10:
        out["maxStorageBuffersInFragmentStage"] = 10
    return out


def precondition(base: dict, plain: dict, dropped: str, headless: bool) -> dict:
    """The host adapter as the plain dump's main frame reports it."""
    claimed = base["gpu"]["webgpu"]
    if not claimed.get("available"):
        raise GuardSkip("the profile claims no WebGPU adapter; the guard needs one that does")
    section = plain["contexts"]["main"].get("webgpu") or {}
    adapter = section.get("adapter")
    if not adapter:
        mode = "headless" if headless else "headful"
        raise GuardSkip(f"no WebGPU adapter on this host ({mode}): X11, Xvfb and headless have none; run --headful on the capture host class (Wayland, NVIDIA)")
    if adapter.get("isFallbackAdapter"):
        raise GuardSkip("the host adapter is the fallback adapter, not the profile's GPU class")
    for key, field in (("vendor", "vendor"), ("architecture", "architecture"), ("subgroupMinSize", "subgroup_min_size"), ("subgroupMaxSize", "subgroup_max_size")):
        if adapter.get(key) != claimed[field]:
            raise GuardSkip(f"the host adapter's {key} is {adapter.get(key)!r}, the profile's {claimed[field]!r}: not the profile's GPU class")
    missing = sorted(set(claimed["features"]) - set(adapter.get("features") or []))
    if missing:
        raise GuardSkip(f"the host adapter lacks profile features {missing}")
    # An alignment (min*) is backed at or above the host's, every other limit
    # at or below.
    beyond = {}
    for key, value in claimed["limits"].items():
        have = adapter["limits"].get(key)
        if have is None or (value < have if key.startswith("min") else value > have):
            beyond[key] = (value, have)
    if beyond:
        raise GuardSkip(f"the host adapter cannot back profile limits (profile, host): {beyond}")
    if dropped not in adapter["features"] or "subgroups" not in adapter["features"]:
        raise GuardSkip(f"the host adapter lacks {dropped!r} or 'subgroups'")
    return adapter


def check_hardware(args, base: dict, work: Path, headless: bool) -> None:
    """Checks 0 to 5."""
    dropped = next((f for f in DROPPABLE if f in base["gpu"]["webgpu"].get("features", [])), None)
    if dropped is None:
        raise GuardSkip(f"the profile lists none of {DROPPABLE}, so the guard has no feature to drop")
    plain = checked(dump(args.binary, None, headless=headless, extra_args=host_gpu_args(headless)), "no profile")
    host = precondition(base, plain, dropped, headless)

    profile = copy.deepcopy(base)
    claimed = profile["gpu"]["webgpu"]
    claimed["features"] = [f for f in claimed["features"] if f != dropped]
    claimed["limits"].update(lowered_limits(host["limits"]))
    claimed["architecture"] = "turing" if claimed["architecture"] != "turing" else "ampere"

    plain_run = session(args.binary, None, headless=headless, extra_args=host_gpu_args(headless), script=SCRIPT, arg={"dropped": dropped, "limit": LIMIT, "limitValue": host["limits"][LIMIT]})
    stock = plain_run["results"].get("main") or {}

    def plain_check(r: dict) -> str | None:
        if not r.get("adapter"):
            return f"no adapter: {r}"
        if r.get("droppedFeature") != {"ok": True} or r.get("atLimit") != {"ok": True}:
            return f"the host refuses what the guard requires: {r}"
        if (r.get("aboveLimit") or {}).get("name") != "OperationError":
            return f"a required limit past the host's was not an OperationError: {r.get('aboveLimit')}"
        return None

    try:
        expect_results(plain_run["results"], "precondition: the host backs the guard's claims and rejects a limit past its own", plain_check)
    except GuardFailure as exc:
        raise GuardFailure(f"vacuous: {exc}") from exc

    path = write_profile(profile, work, "webgpu")
    d = checked(dump(args.binary, str(path), headless=headless), "mutated profile")
    info = {
        "vendor": claimed["vendor"],
        "architecture": claimed["architecture"],
        "device": "",
        "description": "",
        "subgroupMinSize": claimed["subgroup_min_size"],
        "subgroupMaxSize": claimed["subgroup_max_size"],
        "isFallbackAdapter": False,
    }
    for key, value in info.items():
        expect_everywhere(d, f"webgpu.adapter.{key}", value, label=f"GPUAdapterInfo.{key}")
    expect_everywhere(d, "webgpu.adapter.features", sorted(claimed["features"]), label=f"adapter features without {dropped}")
    bad = {}
    for ctx in ALL_CONTEXTS:
        limits = _get(d["contexts"].get(ctx, {}), "webgpu.adapter.limits")
        wrong = {k: (limits.get(k) if isinstance(limits, dict) else None, v) for k, v in claimed["limits"].items()
                 if not isinstance(limits, dict) or limits.get(k) != v}
        if wrong:
            bad[ctx] = wrong
    if bad:
        raise GuardFailure(f"adapter limits (got, want): {bad}")
    print(f"PASS: all {len(claimed['limits'])} adapter limits are the profile's in {len(ALL_CONTEXTS)} contexts")
    expect_everywhere(d, "webgpu.preferredCanvasFormat", claimed["preferred_canvas_format"], label="getPreferredCanvasFormat()")
    expect_everywhere(d, "webgpu.wgslLanguageFeatures", _get(plain["contexts"]["main"], "webgpu.wgslLanguageFeatures"),
                      label="wgslLanguageFeatures (the build's)")

    claim = claimed["limits"][LIMIT]
    host_value = host["limits"][LIMIT]
    stock_message = (stock.get("aboveLimit") or {}).get("message", "")
    want_message = stock_message.replace(f"({host_value + 1})", f"({claim + 1})").replace(f"({host_value})", f"({claim})")
    run = session(args.binary, path, headless=headless, script=SCRIPT, arg={"dropped": dropped, "limit": LIMIT, "limitValue": claim})

    def behaves(r: dict) -> str | None:
        if not r.get("adapter"):
            return f"no adapter: {r}"
        if (r.get("droppedFeature") or {}).get("name") != "TypeError":
            return f"requiring the dropped {dropped!r} was not a TypeError: {r.get('droppedFeature')}"
        if r.get("aboveLimit") != {"name": "OperationError", "message": want_message}:
            return f"{LIMIT} {claim + 1}: got {r.get('aboveLimit')}, stock rejects with {want_message!r}"
        if r.get("atLimit") != {"ok": True}:
            return f"{LIMIT} {claim} was not accepted: {r.get('atLimit')}"
        size = r.get("subgroupSize")
        if not isinstance(size, int) or not claimed["subgroup_min_size"] <= size <= claimed["subgroup_max_size"]:
            return f"subgroup_size in WGSL is {size}, the claim {claimed['subgroup_min_size']}..{claimed['subgroup_max_size']}"
        return None

    expect_results(run["results"], "requestDevice and WGSL follow the claimed adapter", behaves)
    expect_no_unbacked(run["stderr"], "mutated profile")

    none = copy.deepcopy(base)
    none["gpu"]["webgpu"] = {"available": False, "preferred_canvas_format": base["gpu"]["webgpu"]["preferred_canvas_format"]}
    d = checked(dump(args.binary, str(write_profile(none, work, "no-webgpu")), headless=headless), "available false")
    expect_everywhere(d, "webgpu.adapter", None, label="gpu.webgpu.available false: requestAdapter() resolves null")
    expect_everywhere(d, "webgpu.preferredCanvasFormat", none["gpu"]["webgpu"]["preferred_canvas_format"],
                      label="getPreferredCanvasFormat() without an adapter")

    if base["os"]["family"] == "linux":
        wrong = copy.deepcopy(base)
        wrong["gpu"]["webgpu"]["preferred_canvas_format"] = "bgra8unorm"
        bad_path = write_profile(wrong, work, "bgra-on-linux")
        r = run_binary(args.binary, [f"--phasmid-profile={bad_path}", "--headless=new", f"--user-data-dir={tempfile.mkdtemp(dir=work)}", "--no-first-run", "about:blank"], timeout=60)
        if r.returncode == 0 or "gpu.webgpu.preferred_canvas_format" not in r.stderr:
            raise GuardFailure(f"a Linux profile claiming bgra8unorm: exit {r.returncode}, stderr {r.stderr[-300:]!r}")
        print(f"PASS: a Linux profile claiming bgra8unorm exits {r.returncode} naming gpu.webgpu.preferred_canvas_format")


def check_fallback(args, base: dict, work: Path, headless: bool) -> None:
    """Checks 6 to 9."""
    extra = FALLBACK_GL
    mode = ("headless" if headless else "headful") + " with " + " ".join(FALLBACK_GL)
    claimed = base["gpu"]["webgpu"]
    if not claimed.get("available"):
        raise GuardSkip("the profile claims no WebGPU adapter; the guard needs one that does")

    plain = session(args.binary, None, headless=headless, extra_args=extra, script=FALLBACK_SCRIPT)
    if any((r or {}).get("adapter") for r in plain["results"].values()):
        print(f"SKIP: fallback part: without a profile this mode ({mode}) has an adapter: {plain['results']}")
        return
    print(f"PASS: precondition: no profile, {mode}: no WebGPU adapter")
    unsafe = session(args.binary, None, headless=headless, extra_args=extra + UNSAFE, script=FALLBACK_SCRIPT)
    backing = unsafe["results"].get("main") or {}
    if not backing.get("adapter") or not (backing.get("info") or {}).get("isFallbackAdapter"):
        raise GuardFailure(f"vacuous: with --enable-unsafe-webgpu ({mode}) there is no SwiftShader adapter to back the claim: {backing}")
    unbacked = next((f for f in claimed["features"] if f not in backing["features"]), None)
    backed = next((f for f in DROPPABLE if f in claimed["features"] and f in backing["features"]), None)
    limit = next((k for k in [LIMIT, *sorted(claimed["limits"])]
                  if not k.startswith("min") and claimed["limits"][k] > backing["limits"].get(k, claimed["limits"][k])), None)
    if not unbacked or not backed or not limit:
        raise GuardFailure(f"vacuous: the profile claims no feature or maximum limit beyond the SwiftShader adapter "
                           f"(unbacked {unbacked}, backed {backed}, limit {limit})")
    claim, have = claimed["limits"][limit], backing["limits"][limit]
    print(f"PASS: precondition: the SwiftShader adapter backs {len(backing['features'])} features; the profile also claims "
          f"{unbacked!r} and {limit} {claim} (SwiftShader {have})")
    probe = session(args.binary, None, headless=headless, extra_args=extra + UNSAFE, script=FALLBACK_SCRIPT,
                    arg={"unbacked": backed, "backed": backed, "limit": limit, "claim": have, "backing": have}, contexts=("main",))
    stock_message = ((probe["results"].get("main") or {}).get("aboveBacking") or {}).get("message", "")
    if f"({have + 1})" not in stock_message or f"({have})" not in stock_message:
        raise GuardFailure(f"vacuous: Dawn's message for {limit} {have + 1} on SwiftShader names neither number: {stock_message!r}")
    want_message = stock_message.replace(f"({have + 1})", f"({claim + 1})").replace(f"({have})", f"({claim})")

    path = write_profile(base, work, "webgpu-fallback")
    d = checked(dump(args.binary, str(path), headless=headless, extra_args=extra), f"profile, {mode}")
    info = {
        "vendor": claimed["vendor"],
        "architecture": claimed["architecture"],
        "device": "",
        "description": "",
        "subgroupMinSize": claimed["subgroup_min_size"],
        "subgroupMaxSize": claimed["subgroup_max_size"],
        "isFallbackAdapter": False,
    }
    for key, value in info.items():
        expect_everywhere(d, f"webgpu.adapter.{key}", value, label=f"{mode}: GPUAdapterInfo.{key}")
    expect_everywhere(d, "webgpu.adapter.features", sorted(claimed["features"]), label=f"{mode}: adapter features are the profile's")
    bad = {}
    for ctx in ALL_CONTEXTS:
        limits = _get(d["contexts"].get(ctx, {}), "webgpu.adapter.limits")
        wrong = {k: (limits.get(k) if isinstance(limits, dict) else None, v) for k, v in claimed["limits"].items()
                 if not isinstance(limits, dict) or limits.get(k) != v}
        if wrong:
            bad[ctx] = wrong
    if bad:
        raise GuardFailure(f"{mode}: adapter limits (got, want): {bad}")
    print(f"PASS: {mode}: all {len(claimed['limits'])} adapter limits are the profile's in {len(ALL_CONTEXTS)} contexts")

    run = session(args.binary, path, headless=headless, extra_args=extra, script=FALLBACK_SCRIPT,
                  arg={"unbacked": unbacked, "backed": backed, "limit": limit, "claim": claim, "backing": have})

    def behaves(r: dict) -> str | None:
        if not r.get("adapter"):
            return f"no adapter: {r}"
        if r.get("forceFallback"):
            return "forceFallbackAdapter got an adapter; the claimed hardware offers none"
        refusals = (("unbackedFeature", "Disallowed feature requested."), ("withinClaim", "Disallowed limit requested."))
        for key, text in refusals:
            if r.get(key) != {"name": "OperationError", "message": REQUEST_DEVICE + text}:
                return f"{key}: got {r.get(key)}, want OperationError {text!r}"
        for key in ("backedFeature", "atBacking"):
            if r.get(key) != {"ok": True}:
                return f"{key} was not accepted: {r.get(key)}"
        if r.get("aboveClaim") != {"name": "OperationError", "message": want_message}:
            return f"{limit} {claim + 1}: got {r.get('aboveClaim')}, want {want_message!r}"
        if r.get("computed") != 42:
            return f"a compute pass on the device wrote {r.get('computed')}, not 42"
        return None

    expect_results(run["results"], f"{mode}: requestDevice refuses what the fallback cannot back, and the device works", behaves)
    warnings = unbacked_warnings(run["stderr"])
    for field in ("gpu.webgpu.adapter", f"gpu.webgpu.features.{unbacked}"):
        if field not in warnings:
            raise GuardFailure(f"{mode}: no '{field}' unbacked-claim report; got {sorted(warnings)}")
    print(f"PASS: {mode}: gpu.webgpu.adapter and gpu.webgpu.features.{unbacked} reported as unbacked")


def main() -> None:
    args = parse_args(__doc__)
    base = load_profile(args.profile)
    work = Path(args.work_dir)
    headless = not args.headful
    try:
        check_hardware(args, base, work, headless)
    except GuardSkip as exc:
        print(f"SKIP: hardware part: {exc}")
    check_fallback(args, base, work, headless)


finish(main)
