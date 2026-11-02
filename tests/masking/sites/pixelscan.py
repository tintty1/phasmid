"""Pixelscan fingerprint check: per-card verdicts from an Angular app with drifting markup.

Judged cards are browser integrity, fingerprint masking, bot detection and
location masking. The proxy card depends on the network, so it is recorded
and not judged. A card that is missing or still loading is an ``error``.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error

NAME = "pixelscan"
URL = "https://pixelscan.net/fingerprint-check"
SCORE_FORMAT = "{} failed"

# One custom element per checker; inside it, pxlscn-checker-card renders
# div.checker-card (with --failed on a failed check), a value paragraph and a
# label. While collecting, the icon carries checker-card__icon--loading.
JUDGED = {
    "pxlscn-browser-integrity": "browser integrity",
    "pxlscn-fingerprint-masking": "fingerprint masking",
    "pxlscn-bot-detection": "bot detection",
    "pxlscn-location-masking": "location masking",
}
RECORDED = {"pxlscn-proxy": "proxy"}
CARD = ".checker-card"
FAILED_CLASS = "checker-card--failed"
LOADING = ".checker-card__icon--loading"
VALUE = ".checker-card__value-wrapper"
TAGS = list(JUDGED) + list(RECORDED)
READY = """(tags) => tags.every(t => {
  const el = document.querySelector(t);
  return el && el.querySelector('%s') && !el.querySelector('%s');
})""" % (CARD, LOADING)
EXTRACT = """(tags) => Object.fromEntries(tags.map(t => {
  const el = document.querySelector(t);
  const card = el && el.querySelector('%s');
  if (!card) return [t, null];
  return [t, {
    failed: card.classList.contains('%s'),
    loading: !!el.querySelector('%s'),
    value: (el.querySelector('%s')?.innerText || '').trim(),
  }];
}))""" % (CARD, FAILED_CLASS, LOADING, VALUE)


async def check(page: Page) -> SiteResult:
    await page.goto(URL, wait_until="load")
    try:
        await page.wait_for_function(READY, arg=TAGS, timeout=60_000, polling=500)
    except Exception:  # noqa: BLE001 (report what was there)
        return judge(await page.evaluate(EXTRACT, TAGS))
    await page.wait_for_timeout(1000)
    return judge(await page.evaluate(EXTRACT, TAGS))


def judge(data: dict[str, Any]) -> SiteResult:
    missing = [JUDGED[t] for t in JUDGED if not data.get(t)]
    loading = [JUDGED[t] for t in JUDGED if data.get(t) and data[t].get("loading")]
    if missing or loading:
        return site_error(f"cards missing: {missing}, still loading: {loading}", raw=data)
    cards = {JUDGED[t]: "failed" if data[t]["failed"] else "passed" for t in JUDGED}
    values = {JUDGED[t]: data[t].get("value", "") for t in JUDGED}
    recorded = {
        RECORDED[t]: {"result": "failed" if data[t]["failed"] else "passed", "value": data[t].get("value", "")}
        for t in RECORDED
        if data.get(t)
    }
    failed = sum(1 for v in cards.values() if v == "failed")
    signals = {"cards": cards, "values": values, "recorded": recorded}
    return SiteResult(verdict="pass" if failed == 0 else "fail", score=failed, signals=signals, raw=data)
