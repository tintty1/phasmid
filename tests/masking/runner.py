"""Run the site checks against one binary in one mode.

Every site gets its own browser process and user data directory, so no site
sees another's cookies or storage. The browser is started by
``consistency.browser.Launch`` (manual launch, fixed debugging port, CDP
attach, the default context, no init scripts), which keeps Playwright's
launcher switches out of what the sites measure.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from playwright.async_api import Browser, Playwright, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consistency.browser import NO_MEDIA_EMULATION, Launch  # noqa: E402
from results import SuiteResult  # noqa: E402

from .core import SiteResult  # noqa: E402
from .sites import load  # noqa: E402

MODES = ("headful", "headless", "xvfb")
REMOTE_MODE = "docker"
SITE_TIMEOUT = 120.0
XVFB_SCREEN = "1920x1080x24"


@dataclass
class RunConfig:
    binary: Path
    mode: str
    label: str
    out: Path
    sites: list[str]
    profile: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    site_timeout: float = SITE_TIMEOUT
    # A vendor-hosted browser (masking.remote): called once per site visit.
    remote: Callable[[], Any] | None = None


class Xvfb:
    """An Xvfb server on the first free display number, for the run's lifetime."""

    def __init__(self, screen: str = XVFB_SCREEN) -> None:
        self.screen = screen
        self.display: int | None = None
        self.process: subprocess.Popen | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("Xvfb") is not None

    def start(self, timeout: float = 15.0) -> str:
        display = 99
        while Path(f"/tmp/.X11-unix/X{display}").exists() or Path(f"/tmp/.X{display}-lock").exists():
            display += 1
        self.display = display
        self.process = subprocess.Popen(
            ["Xvfb", f":{display}", "-screen", "0", self.screen, "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while not Path(f"/tmp/.X11-unix/X{display}").exists():
            if self.process.poll() is not None:
                raise RuntimeError(f"Xvfb :{display} exited with {self.process.returncode}")
            if time.monotonic() > deadline:
                self.stop()
                raise TimeoutError(f"Xvfb :{display} did not create its socket")
            time.sleep(0.1)
        return f":{display}"

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def make_launch(config: RunConfig, display: str | None) -> Any:
    """Build the launcher for one site visit in the configured mode.

    Under Xvfb the browser is pinned to X11: on a host that also runs a
    Wayland session, Chrome's automatic platform choice would open the
    window there instead of on the virtual display.
    """
    if config.remote is not None:
        return config.remote()
    extra = list(config.extra_args)
    env: dict[str, str] = {}
    if config.mode == "xvfb":
        env["DISPLAY"] = display or ""
        extra.append("--ozone-platform=x11")
    return Launch(
        binary=config.binary,
        headless=config.mode == "headless",
        profile=config.profile,
        extra_args=extra,
        env=env,
    )


async def attach(launch: Any, playwright: Playwright) -> Browser:
    """Start the browser and attach with the async API.

    ``Launch.start`` is written against the sync API; with an async
    Playwright its ``connect_over_cdp`` call returns the coroutine, which is
    awaited here. The DevTools readiness poll inside it blocks the loop for
    the few seconds startup takes, which is harmless with one browser at a
    time.
    """
    pending: Any = launch.start(playwright)  # type: ignore[arg-type]
    launch.browser = await pending
    return launch.browser  # type: ignore[return-value]


async def detach(launch: Any) -> None:
    browser: Any = launch.browser
    launch.browser = None
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 (best-effort teardown)
            pass
    launch.stop()


async def visit(site: ModuleType, config: RunConfig, display: str | None, shot: Path) -> tuple[SiteResult, dict[str, Any]]:
    """One attempt at one site: fresh browser, check, full-page screenshot."""
    meta: dict[str, Any] = {}
    launch = make_launch(config, display)
    async with async_playwright() as pw:
        try:
            browser = await attach(launch, pw)
            meta["browser"] = launch.devtools_version.get("Browser")
            meta["user_agent"] = launch.devtools_version.get("User-Agent")
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
            await page.emulate_media(**NO_MEDIA_EMULATION)
            page.set_default_timeout(30_000)
            try:
                result = await asyncio.wait_for(site.check(page), timeout=config.site_timeout)
            except asyncio.TimeoutError:
                result = SiteResult(verdict="error", error=f"site timed out after {config.site_timeout:.0f}s")
            except Exception as exc:  # noqa: BLE001 (a parser or network failure is an error verdict)
                result = SiteResult(verdict="error", error=f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
            try:
                await page.screenshot(path=str(shot), full_page=True, timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                meta["screenshot_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 (launch failure)
            result = SiteResult(verdict="error", error=f"launch: {type(exc).__name__}: {exc}")
            meta["stderr_tail"] = launch.stderr_tail(2000)
        finally:
            await detach(launch)
    return result, meta


def format_score(site: ModuleType, score: float | str | None) -> str | None:
    if score is None:
        return None
    try:
        return getattr(site, "SCORE_FORMAT", "{}").format(score)
    except (ValueError, TypeError):
        return str(score)


def note_for(result: SiteResult) -> str | None:
    if result.verdict == "error":
        return result.error
    if result.verdict == "fail":
        return json.dumps(result.signals, sort_keys=True, default=str)[:600]
    return None


def write_record(directory: Path, record: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['site']}.json"
    path.write_text(json.dumps(record, indent=1, sort_keys=True, default=str) + "\n")
    return path


async def run(config: RunConfig) -> SuiteResult:
    suite = SuiteResult(suite=f"masking-{config.label}-{config.mode}")
    suite.metrics.update({"binary": str(config.binary), "mode": config.mode, "label": config.label})
    directory = config.out / config.label / config.mode
    directory.mkdir(parents=True, exist_ok=True)

    if config.mode == "xvfb" and not Xvfb.available():
        for name in config.sites:
            site = load(name)
            write_record(directory, {
                "site": name, "url": site.URL, "mode": config.mode, "label": config.label,
                "verdict": "skip", "score": None, "score_text": None, "signals": {}, "raw": None,
                "elapsed_s": 0.0, "error": "Xvfb not installed", "attempts": 0,
            })
            suite.record(name, "skip", "Xvfb not installed")
            print(f"SKIP  {name}: Xvfb not installed")
        return suite.finish()

    xvfb = Xvfb() if config.mode == "xvfb" else None
    display = xvfb.start() if xvfb else None
    try:
        for name in config.sites:
            site = load(name)
            shot = directory / f"{name}.png"
            started = time.monotonic()
            attempts: list[dict[str, Any]] = []
            result, meta = await visit(site, config, display, shot)
            attempts.append({"verdict": result.verdict, "error": result.error})
            if result.verdict == "error":
                result, meta = await visit(site, config, display, shot)
                attempts.append({"verdict": result.verdict, "error": result.error})
            elapsed = round(time.monotonic() - started, 1)
            record = {
                "site": name,
                "url": site.URL,
                "mode": config.mode,
                "label": config.label,
                "verdict": result.verdict,
                "score": result.score,
                "score_text": format_score(site, result.score),
                "signals": result.signals,
                "raw": result.raw,
                "elapsed_s": elapsed,
                "error": result.error,
                "attempts": attempts,
                **meta,
            }
            write_record(directory, record)
            note = note_for(result)
            if len(attempts) > 1 and result.verdict != "error":
                note = f"flaky: error on first attempt ({attempts[0]['error']}), {result.verdict} on retry" + (f"; {note}" if note else "")
            suite.record(name, result.verdict, note)
            print(f"{result.verdict.upper():5s} {name} {record['score_text'] or ''} ({elapsed}s){' ' + result.error if result.error else ''}")
    finally:
        if xvfb:
            xvfb.stop()
    return suite.finish()


def run_sync(config: RunConfig) -> SuiteResult:
    return asyncio.run(run(config))


def default_out() -> Path:
    return Path(os.environ.get("PHASMID_MASKING_DIR", Path(__file__).resolve().parents[1] / "out" / "masking"))
