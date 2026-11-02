"""CreepJS machine test: per-check agreement of OS, platform, GPU and hardware claims."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error, wait_stable

NAME = "creepjs_machine"
URL = "https://abrahamjuliot.github.io/creepjs/tests/machine.html"
SCORE_FORMAT = "{} failed"

# Each check is a <div> whose first child is <span class="pass">✔</span> or
# <span class="fail">; the div's text is "<check>: <value>".
PASS = "span.pass"
FAIL = "span.fail"
EXTRACT = """() => ({
  passed: [...document.querySelectorAll('%s')].map(s => s.parentElement.innerText.trim()),
  failed: [...document.querySelectorAll('%s')].map(s => s.parentElement.innerText.trim()),
})""" % (PASS, FAIL)
COUNT = f"() => document.querySelectorAll('{PASS}, {FAIL}').length"


async def check(page: Page) -> SiteResult:
    await page.goto(URL, wait_until="load")
    await page.wait_for_selector(f"{PASS}, {FAIL}", timeout=60_000)
    await wait_stable(page, COUNT, quiet=2.0, timeout=30.0)
    return judge(await page.evaluate(EXTRACT))


def judge(data: dict[str, Any]) -> SiteResult:
    passed = list(data.get("passed", []))
    failed = list(data.get("failed", []))
    if not passed and not failed:
        return site_error("no pass/fail spans found", raw=data)
    verdict = "pass" if passed and not failed else "fail"
    return SiteResult(verdict=verdict, score=len(failed), signals={"failed": failed, "passed_count": len(passed)}, raw=data)
