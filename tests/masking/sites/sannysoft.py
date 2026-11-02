"""SannySoft: headless and automation leaks (webdriver, chrome object, plugins, WebGL, permissions)."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, scroll_to_bottom, site_error, wait_stable

NAME = "sannysoft"
URL = "https://bot.sannysoft.com/"
SCORE_FORMAT = "{} failed"

# Result cells in both tables: "passed"/"failed" class on the value <td>
# (the fingerprint-scanner table uses "result passed"/"failed result").
PASSED = ".passed"
FAILED = ".failed"
# The row name is the first cell of the row holding a result cell; its first
# line drops the "(Old)"/"(New)" suffix the intoli table adds.
EXTRACT = """() => {
  const name = (cell) => {
    const row = cell.closest('tr');
    const first = row && row.querySelector('td, th');
    return first ? first.innerText.split('\\n')[0].trim() : cell.innerText.trim();
  };
  return {
    passed: document.querySelectorAll('%s').length,
    failed: [...document.querySelectorAll('%s')].map(name),
  };
}""" % (PASSED, FAILED)
COUNT = f"() => document.querySelectorAll('{PASSED}, {FAILED}').length"


async def check(page: Page) -> SiteResult:
    await page.goto(URL, wait_until="load")
    await page.wait_for_selector(f"{PASSED}, {FAILED}", timeout=30_000)
    await scroll_to_bottom(page)
    await wait_stable(page, COUNT, quiet=2.0, timeout=20.0)
    return judge(await page.evaluate(EXTRACT))


def judge(data: dict[str, Any]) -> SiteResult:
    passed = int(data.get("passed", 0))
    failed = sorted(set(data.get("failed", [])))
    if passed == 0 and not failed:
        return site_error("no result cells found", raw=data)
    verdict = "pass" if passed >= 1 and not failed else "fail"
    return SiteResult(verdict=verdict, score=len(failed), signals={"failed": failed, "passed_count": passed}, raw=data)
