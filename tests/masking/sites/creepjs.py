"""CreepJS main page: lies (tampered prototypes and impossible values), trash, headless ratings.

The page renders no trust score or lies total. It publishes its own result
object as ``window.Fingerprint`` once rendering finishes, with
``lies.totalLies`` and ``lies.data`` (API -> lie types), ``trash.trashBin``
and the headless ratings. That object is the page's verdict, read as is; the
per-section ``.lies`` hash markers in the DOM are read as a cross-check.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error

NAME = "creepjs"
URL = "https://abrahamjuliot.github.io/creepjs/"
SCORE_FORMAT = "{} lies"

# Set by creep.js after the fingerprint is computed and rendered.
READY = "() => !!(window.Fingerprint && window.Fingerprint.lies && window.Fingerprint.headless)"
# Section hashes rendered with class "lies" mark sections that lied; their
# heading is the preceding <strong>.
EXTRACT = """() => {
  const fp = window.Fingerprint;
  const h = fp.headless || {};
  const truthy = (o) => Object.keys(o || {}).filter(k => o[k] === true);
  return {
    totalLies: fp.lies.totalLies,
    lies: fp.lies.data,
    trash: (fp.trash && fp.trash.trashBin) || [],
    headlessRating: h.headlessRating,
    likeHeadlessRating: h.likeHeadlessRating,
    stealthRating: h.stealthRating,
    headless: truthy(h.headless),
    likeHeadless: truthy(h.likeHeadless),
    stealth: truthy(h.stealth),
    lieSections: [...document.querySelectorAll('span.lies.hash')].map(s => {
      const strong = s.previousElementSibling;
      return strong ? strong.innerText.trim() : '';
    }),
  };
}"""


async def check(page: Page) -> SiteResult:
    await page.goto(URL, wait_until="load")
    await page.wait_for_function(READY, timeout=90_000, polling=500)
    await page.wait_for_timeout(1500)
    return judge(await page.evaluate(EXTRACT))


def judge(data: dict[str, Any]) -> SiteResult:
    total = data.get("totalLies")
    lies = data.get("lies")
    if not isinstance(total, int) or not isinstance(lies, dict):
        return site_error("window.Fingerprint has no lies.totalLies/lies.data", raw=data)
    lie_items = [f"{api}: {', '.join(map(str, kinds)) if isinstance(kinds, list) else kinds}" for api, kinds in sorted(lies.items())]
    trash = [f"{t.get('name')}: {t.get('value')}" for t in data.get("trash", []) if isinstance(t, dict)]
    bot = (
        f"{data.get('headlessRating')}% headless, {data.get('likeHeadlessRating')}% like headless, "
        f"{data.get('stealthRating')}% stealth"
    )
    signals = {
        "lies": total,
        "lie_items": lie_items,
        "lie_sections": data.get("lieSections", []),
        "trash": len(trash),
        "trash_items": trash,
        "bot": bot,
        "headless_flags": data.get("headless", []),
        "like_headless_flags": data.get("likeHeadless", []),
        "stealth_flags": data.get("stealth", []),
    }
    return SiteResult(verdict="pass" if total == 0 else "fail", score=total, signals=signals, raw=data)
