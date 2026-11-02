"""Fingerprint.com playground: a commercial classifier's Smart Signals.

The block under "Server API Response" is the event the demo fetches from its
own backend (``/api/event/v4/<request id>``). The JSON body of that response
is read directly; the rendered tree is only checked for presence, since its
text is not JSON. The event is the v4 shape (flat ``bot``, ``tampering``,
``virtual_machine`` ...); the older ``products.<name>.data`` shape is
accepted too.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..core import SiteResult, site_error

NAME = "fingerprint_com"
URL = "https://demo.fingerprint.com/playground"
SCORE_FORMAT = "suspect {}"

EVENT_PATH = "/api/event/"  # the demo backend's Server API proxy
SERVER_JSON = "[data-testid=serverResponseJSON]"  # rendered "Server API Response" tree
NOT_DETECTED = {"not_detected", "notDetected"}  # v4 spelling, then the older one


async def check(page: Page) -> SiteResult:
    async with page.expect_response(lambda r: EVENT_PATH in r.url and r.request.method in ("GET", "POST"), timeout=90_000) as info:
        await page.goto(URL, wait_until="networkidle")
    response = await info.value
    if response.status != 200:
        return site_error(f"event request returned HTTP {response.status}")
    try:
        event = await response.json()
    except Exception as exc:  # noqa: BLE001 (any decode failure is a parser miss)
        return site_error(f"event response is not JSON: {exc}")
    try:
        await page.wait_for_selector(SERVER_JSON, timeout=30_000)
    except Exception:  # noqa: BLE001
        return site_error(f"{SERVER_JSON} never rendered", raw=event)
    return judge(event)


def normalize(event: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce either event shape to the judged and recorded signals."""
    if "bot" in event and "tampering" in event:
        return {
            "botd": event.get("bot"),
            "bot_type": event.get("bot_type"),
            "tampering": event.get("tampering"),
            "virtualMachine": event.get("virtual_machine"),
            "incognito": event.get("incognito"),
            "developerTools": event.get("developer_tools"),
            "vpn": event.get("vpn"),
            "vpn_methods": event.get("vpn_methods"),
            "proxy": event.get("proxy"),
            "suspect_score": event.get("suspect_score"),
        }
    products = event.get("products")
    if isinstance(products, dict):
        def data(name: str) -> dict[str, Any]:
            return (products.get(name) or {}).get("data") or {}
        return {
            "botd": (data("botd").get("bot") or {}).get("result"),
            "bot_type": (data("botd").get("bot") or {}).get("type"),
            "tampering": data("tampering").get("result"),
            "virtualMachine": data("virtualMachine").get("result"),
            "incognito": data("incognito").get("result"),
            "developerTools": data("developerTools").get("result"),
            "vpn": data("vpn").get("result"),
            "vpn_methods": data("vpn").get("methods"),
            "proxy": data("proxy").get("result"),
            "suspect_score": data("suspectScore").get("result"),
        }
    return None


def judge(event: dict[str, Any]) -> SiteResult:
    signals = normalize(event)
    if signals is None or signals["botd"] is None or signals["tampering"] is None or signals["virtualMachine"] is None:
        return site_error("event has no bot/tampering/virtual machine signals", raw=event)
    ok = signals["botd"] in NOT_DETECTED and signals["tampering"] is False and signals["virtualMachine"] is False
    return SiteResult(verdict="pass" if ok else "fail", score=signals["suspect_score"], signals=signals, raw=event)
