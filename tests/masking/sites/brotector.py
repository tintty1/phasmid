"""Brotector: CDP and driver detection, with the driven input the test is about.

The page scores every detection it sees (``runtime.enabled`` for a CDP
client with the Runtime domain on, input provenance for synthetic events)
and shows the average. Playwright itself enables Runtime, so stock Chrome
driven the same way is the control for what the driver causes.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error

NAME = "brotector"
URL = "https://ttlns.github.io/brotector/"
SCORE_FORMAT = "score {:g}"

CLICK_TARGET = "#clickHere"  # the page's "Click here" button
AVG_SCORE = "td#avg-score"  # average score cell; "0" until a detection lands
# Detection rows: every row of #detections except the header (#table-keys)
# and the average row; cells are detection, ms since load, type, score, data.
EXTRACT = """() => ({
  avg: document.querySelector('%s')?.innerText.trim() ?? null,
  rows: [...document.querySelectorAll('#detections tr')]
    .filter(r => r.id !== 'table-keys' && !r.querySelector('%s'))
    .map(r => [...r.querySelectorAll('td')].map(td => td.innerText.trim()))
    .filter(cells => cells.length >= 4),
})""" % (AVG_SCORE, AVG_SCORE)
TEXT_INPUT = "input[type=text], input:not([type]), textarea"


async def check(page: Page) -> SiteResult:
    await page.goto(URL, wait_until="load")
    await page.wait_for_selector(AVG_SCORE, timeout=30_000)
    await page.wait_for_timeout(1000)
    await page.click(CLICK_TARGET)
    await page.mouse.move(100, 100, steps=10)
    await page.mouse.click(100, 100)
    await page.evaluate("async () => { await new Promise(r => setTimeout(r, 100)); return document.title; }")
    field = await page.query_selector(TEXT_INPUT)
    if field is not None:
        await field.click()
    await page.keyboard.type("World", delay=100)
    await page.wait_for_timeout(2000)
    data = await page.evaluate(EXTRACT)
    data["typed_into"] = "input" if field is not None else "document (the page has no text input)"
    return judge(data)


def parse_number(text: str) -> float | None:
    try:
        return float(text.replace("'", "").replace(",", "").strip())
    except ValueError:
        return None


def judge(data: dict[str, Any]) -> SiteResult:
    avg = data.get("avg")
    if avg is None:
        return site_error(f"{AVG_SCORE} not found", raw=data)
    score = parse_number(avg)
    if score is None:
        return site_error(f"average score {avg!r} is not a number", raw=data)
    detections: dict[str, dict[str, Any]] = {}
    for cells in data.get("rows", []):
        name, _ms, kind, row_score = cells[0], cells[1], cells[2], parse_number(cells[3])
        entry = detections.setdefault(name, {"type": kind, "count": 0, "max_score": 0.0})
        entry["count"] += 1
        if row_score is not None:
            entry["max_score"] = max(entry["max_score"], row_score)
    signals = {"detections": detections, "typed_into": data.get("typed_into")}
    return SiteResult(verdict="pass" if score == 0 else "fail", score=score, signals=signals, raw=data)
