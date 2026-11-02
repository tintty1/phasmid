"""Shared pieces for the graphics guards (gpu-info, webgl, webgpu).

The WebGL and WebGPU guards mutate the profile only in the direction the host
can back (lower limits, fewer extensions and features, different strings), so
they need a host of the profile's own GPU class: the capture host, headful or
headless (with a profile headless renders on the host GPU, as headful does).
Any other host (Xvfb llvmpipe, X11 without a WebGPU adapter, no GPU) is a skip
with the reason, not a failure.

`session` runs one browser and evaluates a script in the main frame, a
cross-origin iframe and a dedicated worker (where a canvas is an
OffscreenCanvas); the full consistency dump covers every context for values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from consistency.browser import Launch
from consistency.server import Servers
from helpers import GuardFailure

# The one line every unbacked GPU claim logs (components/phasmid
# ReportUnbackedGpuClaim).
UNBACKED = "Phasmid: host cannot back gpu."

# Runs the source of an async function with one argument and returns its
# result, or {error} when it throws.
_RUN = """async ([src, arg]) => {
  const fn = (0, eval)("(" + src + ")");
  try { return await fn(arg); } catch (e) { return { error: String(e && e.stack || e) }; }
}"""

# The same, in a dedicated worker created from a blob.
_RUN_IN_WORKER = """async ([src, arg]) => {
  const code = `self.onmessage = async (e) => {
    const fn = (0, eval)("(" + e.data.src + ")");
    let r;
    try { r = await fn(e.data.arg); } catch (err) { r = { error: String(err && err.stack || err) }; }
    self.postMessage(r);
  };`;
  const w = new Worker(URL.createObjectURL(new Blob([code], { type: "text/javascript" })));
  return await new Promise((res) => {
    w.onmessage = (e) => res(e.data);
    w.onerror = (e) => res({ error: "worker error: " + e.message });
    w.postMessage({ src, arg });
  });
}"""

_ADD_FRAME = """(src) => new Promise((res) => {
  const f = document.createElement("iframe");
  f.onload = () => res();
  f.src = src;
  document.body.appendChild(f);
})"""

# Text of a page including every shadow root (chrome://gpu renders into them).
_PAGE_TEXT = """() => {
  const out = [];
  const walk = (n) => {
    if (n.shadowRoot) walk(n.shadowRoot);
    for (const c of n.childNodes) {
      if (c.nodeType === Node.TEXT_NODE) out.push(c.textContent);
      else walk(c);
    }
  };
  walk(document);
  return out.join("\\n");
}"""


def host_gpu_args(headless: bool) -> list[str]:
    """Switches for a run without a profile that must measure the GPU path a
    profile run takes. With a profile, headless renders on the host GPU as
    headful does (graphics/headless-gpu.patch), which stock headless does
    only with --enable-gpu; otherwise it forces SwiftShader WebGL."""
    return ["--enable-gpu"] if headless else []


def write_profile(profile: dict, work: Path, name: str) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(profile))
    return path


def checked(d: dict, what: str) -> dict:
    if d["context_errors"]:
        raise GuardFailure(f"contexts failed ({what}): {d['context_errors']}")
    return d


def session(
    binary: str,
    profile: Path | None,
    *,
    headless: bool,
    extra_args: list[str] | None = None,
    script: str | None = None,
    arg: Any = None,
    contexts: tuple[str, ...] = ("main", "iframe_cross", "worker"),
    gpu_page: bool = False,
) -> dict:
    """One browser run. Returns {"results": {context: script result},
    "system_info": CDP SystemInfo.getInfo, "gpu_page": chrome://gpu text,
    "stderr": the browser's whole stderr, every child process included}."""
    out: dict[str, Any] = {"results": {}, "system_info": None, "gpu_page": None}
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=Path(binary), headless=headless, profile=profile, extra_args=extra_args or [])
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            if script is not None:
                page.goto(f"{servers.a.url}/h/guard", wait_until="domcontentloaded")
                if "main" in contexts:
                    out["results"]["main"] = page.evaluate(_RUN, [script, arg])
                if "worker" in contexts:
                    out["results"]["worker"] = page.evaluate(_RUN_IN_WORKER, [script, arg])
                if "iframe_cross" in contexts:
                    page.evaluate(_ADD_FRAME, f"{servers.b.url}/h/guard-frame")
                    frame = next(f for f in page.frames if f.url.startswith(servers.b.url))
                    out["results"]["iframe_cross"] = frame.evaluate(_RUN, [script, arg])
            if gpu_page:
                cdp = browser.new_browser_cdp_session()
                out["system_info"] = cdp.send("SystemInfo.getInfo")
                cdp.detach()
                page.goto("chrome://gpu", wait_until="domcontentloaded")
                page.wait_for_function(f"() => ({_PAGE_TEXT})().includes('GL_RENDERER')", timeout=30_000)
                out["gpu_page"] = page.evaluate(_PAGE_TEXT)
            out["stderr"] = launch.stderr_tail(limit=10_000_000)
        finally:
            launch.stop()
    return out


def expect_results(results: dict[str, Any], label: str, check) -> None:
    """`check(result)` returns None when the context passes, else a reason."""
    bad = {}
    for ctx, result in results.items():
        if isinstance(result, dict) and "error" in result and len(result) == 1:
            bad[ctx] = result["error"]
            continue
        reason = check(result)
        if reason:
            bad[ctx] = reason
    if bad:
        raise GuardFailure(f"{label}: {bad}")
    print(f"PASS: {label} in {', '.join(results)}")


def expect_no_unbacked(stderr: str, what: str) -> None:
    lines = [line for line in stderr.splitlines() if UNBACKED in line]
    if lines:
        raise GuardFailure(f"{what}: a claim the host backs was reported as unbacked: {lines[:5]}")
    print(f"PASS: {what}: no unbacked-claim report")
