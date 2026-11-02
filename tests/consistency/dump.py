"""Run the probe in every context against one binary and return a dump."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from .browser import NO_MEDIA_EMULATION, Launch, binary_version
from .server import Servers

DUMP_VERSION = 1

# Request headers that are fingerprint surfaces. Everything else (host,
# connection, accept-encoding, cookies) is noise for this purpose.
HEADER_SURFACES = [
    "user-agent",
    "accept",
    "accept-language",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-model",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-wow64",
    "sec-ch-ua-form-factors",
    "sec-ch-device-memory",
    "device-memory",
    "sec-ch-dpr",
    "dpr",
    "sec-ch-viewport-width",
    "viewport-width",
    "sec-ch-viewport-height",
    "sec-ch-prefers-color-scheme",
    "sec-ch-prefers-reduced-motion",
    "sec-ch-prefers-reduced-transparency",
    "downlink",
    "rtt",
    "ect",
    "save-data",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "priority",
    "dnt",
    "sec-gpc",
]

HEADER_SLOTS = {
    "navigation": "/index.html",
    "navigation_with_hints": "/index.html",
    "frame_same": "/frame.html",
    "frame_cross": "/frame.html",
    "worker_script": "/worker.js",
    "service_worker_script": "/sw.js",
    "fetch_main": "/h/fetch-main",
    "fetch_iframe_same": "/h/fetch-iframe_same",
    "fetch_iframe_cross": "/h/fetch-iframe_cross",
    "fetch_worker": "/h/fetch-worker",
    "fetch_shared_worker": "/h/fetch-shared_worker",
    "fetch_service_worker": "/h/fetch-service_worker",
}


def _select_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: headers[k] for k in HEADER_SURFACES if k in headers}


def run_dump(
    binary: Path,
    *,
    headless: bool = True,
    profile: Path | None = None,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    stun: str | None = None,
    timeout: float = 90.0,
) -> dict:
    started = time.time()
    with Servers() as servers, sync_playwright() as pw:
        launch = Launch(binary=binary, headless=headless, profile=profile, extra_args=extra_args or [], env=env or {})
        try:
            browser = launch.start(pw)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.emulate_media(**NO_MEDIA_EMULATION)

            url = f"{servers.a.url}/index.html?cross={servers.b.url}&timeout={int(timeout * 1000 * 0.8)}"
            if stun:
                url += f"&stun={stun}"
            # First navigation stores the Accept-CH grant; the second sends the
            # high-entropy hints. Headers are recorded for both.
            page.goto(url, wait_until="domcontentloaded")
            first_nav = servers.recorder.find("/index.html", servers.a.url)
            page.goto(url, wait_until="domcontentloaded")
            second_nav = servers.recorder.find("/index.html", servers.a.url)

            page.wait_for_function("() => window.__phasmid && window.__phasmid.done", timeout=timeout * 1000)
            state = page.evaluate("() => window.__phasmid")

            # Browser-side GPU truth: what chrome://gpu shows, via CDP. The
            # profile's vendor/device IDs and driver strings come from here.
            try:
                cdp = browser.new_browser_cdp_session()
                system_info = cdp.send("SystemInfo.getInfo")
                cdp.detach()
            except Exception as exc:  # noqa: BLE001 (older binaries may lack the domain)
                system_info = {"error": str(exc)}

            headers: dict[str, dict] = {}
            headers["navigation"] = _select_headers(first_nav.headers) if first_nav else {"error": "not recorded"}
            headers["navigation_with_hints"] = _select_headers(second_nav.headers) if second_nav else {"error": "not recorded"}
            for slot, path in HEADER_SLOTS.items():
                if slot.startswith("navigation"):
                    continue
                origin = servers.b.url if slot.endswith("cross") else servers.a.url
                req = servers.recorder.find(path, origin)
                headers[slot] = _select_headers(req.headers) if req else {"error": "not recorded"}

            dump = {
                "dump_version": DUMP_VERSION,
                "harness": {
                    "started": datetime.fromtimestamp(started, timezone.utc).isoformat(),
                    "binary": str(binary),
                    "binary_version": binary_version(binary),
                    "devtools_version": launch.devtools_version,
                    "command": launch.command,
                    "headless": headless,
                    "profile": str(profile) if profile else None,
                    "env": env or {},
                    "origins": {"a": servers.a.url, "b": servers.b.url},
                    "duration_s": round(time.time() - started, 2),
                },
                "contexts": state["results"],
                "context_errors": state["errors"],
                "headers": headers,
                "system_info": system_info,
                "stderr_tail": launch.stderr_tail(),
            }
        finally:
            launch.stop()
    return dump


def write_dump(dump: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dump, indent=1, sort_keys=True) + "\n")
