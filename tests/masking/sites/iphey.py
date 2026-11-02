"""Iphey: IP against browser coherence (location, timezone, language, hardware, software).

Slow and driven by the network the run uses, so its verdict is informational.
The signals block is a card of label/value items, each with a severity dot;
a clean run is either a single "Not detected" item or items that are all
marked good.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error

NAME = "iphey"
URL = "https://iphey.com/"
SCORE_FORMAT = "MX {:g}"
INFORMATIONAL = True

# <article id="signals" class="detail-card detail-card--bad"> holding
# .detail-item blocks: .detail-label, .detail-value, and a
# .detail-dot--{good,warning,bad} severity marker.
SIGNALS = "#signals"
ITEM = "#signals .detail-item"
EXTRACT = """() => ({
  card: document.querySelector('%s')?.className || null,
  items: [...document.querySelectorAll('%s')].map(i => ({
    label: (i.querySelector('.detail-label')?.innerText || '').trim(),
    value: (i.querySelector('.detail-value')?.innerText || '').trim(),
    level: ((i.querySelector('[class*="detail-dot--"]')?.className || '').match(/detail-dot--(\\w+)/) || [])[1] || null,
  })),
  mx: (/(\\d+)\\s*MX SCORE/i.exec(document.body.innerText) || [])[1] || null,
})""" % (SIGNALS, ITEM)
BAD_LEVELS = {"bad", "warning"}


async def check(page: Page) -> SiteResult:
    await page.goto(URL, wait_until="load")
    await page.wait_for_selector(ITEM, timeout=90_000)
    await page.wait_for_timeout(2000)
    return judge(await page.evaluate(EXTRACT))


def judge(data: dict[str, Any]) -> SiteResult:
    items = data.get("items") or []
    if not items:
        return site_error(f"no {ITEM} entries", raw=data)
    entries = [f"{i.get('label')}: {i.get('value')}" for i in items]
    only_not_detected = len(items) == 1 and re.search(r"not detected", items[0].get("value", ""), re.I) is not None
    all_good = all(i.get("level") not in BAD_LEVELS for i in items)
    mx = data.get("mx")
    score = float(mx) if mx is not None else None
    signals = {"entries": entries, "levels": {i.get("label"): i.get("level") for i in items}, "card": data.get("card")}
    return SiteResult(verdict="pass" if only_not_detected or all_good else "fail", score=score, signals=signals, raw=data)
