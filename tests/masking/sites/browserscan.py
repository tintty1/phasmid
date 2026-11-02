"""BrowserScan: "Browser fingerprint authenticity" percentage with itemised deductions.

Deductions name surfaces (timezone, IP vs WebRTC, fonts, UA vs platform), so
they map onto Phasmid patches directly.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error, wait_stable

NAME = "browserscan"
URL = "https://www.browserscan.net/"
SCORE_FORMAT = "{:g}%"

# Google Funding Choices consent dialog, shown in some regions.
CONSENT_CLOSE = "button.fc-close, button.fc-cta-consent"
# "<span>Browser fingerprint authenticity: </span><span>80%</span>": the
# label is a leaf span, the percentage its next sibling.
LABEL = "Browser fingerprint authenticity"
# Deductions are <li> items whose title row ends in " -10%" and whose <p>
# explains the deduction.
EXTRACT = """() => {
  const label = [...document.querySelectorAll('span, div')].find(
    e => e.childElementCount === 0 && e.textContent.trim().startsWith('%s'));
  const value = label && label.nextElementSibling ? label.nextElementSibling.textContent.trim() : null;
  const deductions = [...document.querySelectorAll('li')]
    .filter(li => /-\\d+%%/.test(li.textContent))
    .map(li => {
      const p = li.querySelector('p');
      const head = li.firstElementChild ? li.firstElementChild.innerText : li.innerText;
      return {head: head.trim(), detail: p ? p.innerText.trim() : ''};
    });
  const bot = /Bot Detection:\\s*(Yes|No)/.exec(document.body.innerText);
  return {authenticity: value, deductions, bot_detection: bot ? bot[1] : null};
}""" % LABEL
# The percentage reads 100% as a placeholder until the IP lookup returns;
# the summary table's "Bot Detection" value is filled by the same lookup, so
# both must be present before the page is read.
READY = """() => {
  const label = [...document.querySelectorAll('span, div')].find(
    e => e.childElementCount === 0 && e.textContent.trim().startsWith('%s'));
  const scored = !!(label && label.nextElementSibling && /\\d+%%/.test(label.nextElementSibling.textContent));
  return scored && /Bot Detection:\\s*(Yes|No)/.test(document.body.innerText);
}""" % LABEL
DEDUCTION = re.compile(r"^(?P<what>.*?)\s*-(?P<pct>\d+(?:\.\d+)?)%\s*$", re.S)


async def check(page: Page) -> SiteResult:
    # The labels above are English; the site redirects to the browser
    # language's page (e.g. /vi for a vi-VN locale) unless Nuxt i18n's cookie
    # already names one.
    await page.context.add_cookies([{"name": "i18n_redirected", "value": "en", "domain": ".browserscan.net", "path": "/", "secure": True, "sameSite": "None"}])
    await page.goto(URL, wait_until="load")
    consent = await page.query_selector(CONSENT_CLOSE)
    if consent is not None and await consent.is_visible():
        await consent.click()
    await page.wait_for_function(READY, timeout=90_000, polling=500)
    await wait_stable(page, f"({EXTRACT})()", quiet=3.0, timeout=30.0)
    return judge(await page.evaluate(EXTRACT))


def judge(data: dict[str, Any]) -> SiteResult:
    text = data.get("authenticity")
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    if not match:
        return site_error(f"authenticity percentage not found ({text!r})", raw=data)
    if data.get("bot_detection") is None:
        return site_error("summary table never filled (IP lookup did not finish)", raw=data)
    score = float(match.group(1))
    deductions = []
    for item in data.get("deductions", []):
        m = DEDUCTION.match(item.get("head", ""))
        what, pct = (m.group("what").strip(), float(m.group("pct"))) if m else (item.get("head", ""), None)
        deductions.append({"item": what, "percent": pct, "detail": item.get("detail", "")})
    ok = score >= 97 and len(deductions) <= 1
    signals = {"deductions": deductions, "bot_detection": data.get("bot_detection")}
    return SiteResult(verdict="pass" if ok else "fail", score=score, signals=signals, raw=data)
