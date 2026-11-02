"""Shared types and page helpers for the site modules.

A site module exposes ``NAME``, ``URL``, an async ``check(page)`` that
navigates and extracts, and a pure ``judge(data)`` that turns the extracted
data into a ``SiteResult``. Keeping the verdict rule pure lets the unit
tests cover it without a network.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from playwright.async_api import Page

Verdict = Literal["pass", "fail", "error"]


@dataclass
class SiteResult:
    """One site's verdict.

    ``error`` means the page did not render what the parser expects
    (selector miss, timeout, network); ``fail`` means the page rendered a
    verdict against the browser. The two are never merged, so site drift is
    not read as a leak.
    """

    verdict: Verdict
    score: float | str | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    raw: str | dict | list | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def site_error(reason: str, raw: str | dict | list | None = None) -> SiteResult:
    return SiteResult(verdict="error", error=reason, raw=raw)


async def scroll_to_bottom(page: Page, step: int = 300, interval: float = 0.2, quiet: float = 1.0, cap: float = 30.0) -> None:
    """Scroll in steps until the page stops moving for ``quiet`` seconds.

    Lazy-loaded result rows only render once scrolled into view; a single
    jump to the end skips them.
    """
    deadline = time.monotonic() + cap
    last = -1.0
    still_since = time.monotonic()
    while time.monotonic() < deadline:
        y = await page.evaluate(f"() => {{ window.scrollBy(0, {step}); return window.scrollY; }}")
        if y != last:
            last = y
            still_since = time.monotonic()
        elif time.monotonic() - still_since >= quiet:
            return
        await asyncio.sleep(interval)


async def wait_stable(page: Page, expression: str, quiet: float = 1.5, timeout: float = 30.0, interval: float = 0.25) -> Any:
    """Poll a JS expression until its value stops changing for ``quiet`` seconds; return it.

    Detection pages fill their tables asynchronously and give no completion
    event, so a value that holds still is the only signal that they are done.
    """
    deadline = time.monotonic() + timeout
    last: Any = object()
    since = time.monotonic()
    while True:
        value = await page.evaluate(expression)
        now = time.monotonic()
        if value != last:
            last, since = value, now
        elif now - since >= quiet:
            return value
        if now > deadline:
            return value
        await asyncio.sleep(interval)
