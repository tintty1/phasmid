"""Browsers that run inside a vendor's container, for comparison columns.

A ``Remote`` stands in for ``consistency.browser.Launch``: each site visit
creates a fresh profile through the vendor's local API, starts it, attaches
over the CDP endpoint the vendor exposes, and deletes the profile afterwards,
so no site sees another's storage. The vendor decides every launch switch;
the harness only attaches.

    kameleo       Kameleo's API (default http://localhost:5050); the profile
                  is built from a desktop Chrome fingerprint of --os.
    cloakbrowser  CloakBrowser Manager (default http://localhost:8080); token
                  from PHASMID_CLOAK_TOKEN. The manager picks the claimed OS
                  itself (Windows on a Linux host).
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any

PROVIDERS = ("kameleo", "cloakbrowser")
DEFAULT_URLS = {"kameleo": "http://localhost:5050", "cloakbrowser": "http://localhost:8080"}


def _request(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 120.0) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode()
    return json.loads(text) if text else None


class Remote:
    """One vendor profile for one site visit; the ``Launch`` surface the runner uses."""

    def __init__(self, provider: str, base_url: str | None = None, fingerprint: str | None = None) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
        self.provider = provider
        self.base = (base_url or DEFAULT_URLS[provider]).rstrip("/")
        self.fingerprint = fingerprint
        self.profile_id: str | None = None
        self.devtools_version: dict[str, Any] = {}
        self.browser: Any = None
        self.headers: dict[str, str] = {}
        if provider == "cloakbrowser" and os.environ.get("PHASMID_CLOAK_TOKEN"):
            self.headers["Authorization"] = f"Bearer {os.environ['PHASMID_CLOAK_TOKEN']}"

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        return _request(method, self.base + path, body, self.headers)

    async def start(self, playwright: Any) -> Any:
        if self.provider == "kameleo":
            profile = self._call("POST", "/profiles/new", {"fingerprintId": self.fingerprint, "name": "phasmid-masking"})
            self.profile_id = profile["id"]
            self._call("POST", f"/profiles/{self.profile_id}/start", {})
            endpoint = f"ws://{self.base.split('://', 1)[1]}/playwright/{self.profile_id}"
        else:
            profile = self._call("POST", "/api/profiles", {"name": "phasmid-masking"})
            self.profile_id = profile["id"]
            self._launch_cloak()
            endpoint = f"{self.base}/api/profiles/{self.profile_id}/cdp"
            deadline = time.monotonic() + 60
            while True:
                try:
                    self.devtools_version = self._call("GET", f"/api/profiles/{self.profile_id}/cdp/json/version")
                    break
                except urllib.error.HTTPError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.5)
        self.browser = await playwright.chromium.connect_over_cdp(endpoint, headers=self.headers or None)
        if not self.devtools_version:
            page = self.browser.contexts[0].pages[0] if self.browser.contexts[0].pages else None
            agent = await page.evaluate("navigator.userAgent") if page else None
            self.devtools_version = {"Browser": self.browser.version, "User-Agent": agent}
        return self.browser

    def _launch_cloak(self, timeout: float = 420.0) -> None:
        """Launch, waiting out 402: the license server holds a stopped session for minutes on the free plan."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._call("POST", f"/api/profiles/{self.profile_id}/launch")
                return
            except urllib.error.HTTPError as exc:
                if exc.code != 402 or time.monotonic() > deadline:
                    raise
                time.sleep(5)

    def stderr_tail(self, limit: int = 4000) -> str:
        return ""

    def stop(self) -> None:
        if self.profile_id is None:
            return
        prefix = "/profiles" if self.provider == "kameleo" else "/api/profiles"
        for method, path in (("POST", f"{prefix}/{self.profile_id}/stop"), ("DELETE", f"{prefix}/{self.profile_id}")):
            try:
                self._call(method, path)
            except (urllib.error.URLError, TimeoutError):
                pass
            if self.provider == "kameleo" and method == "POST":
                self._wait_kameleo_stopped()
        self.profile_id = None

    def _wait_kameleo_stopped(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self._call("GET", f"/profiles/{self.profile_id}/status")
            except (urllib.error.URLError, TimeoutError):
                return
            if status.get("lifetimeState") not in ("running", "terminating", "starting"):
                return
            time.sleep(0.5)


def pick_fingerprint(base_url: str | None, os_family: str) -> dict[str, Any]:
    """A random desktop Chrome fingerprint of ``os_family`` and the newest major from Kameleo's catalogue."""
    base = (base_url or DEFAULT_URLS["kameleo"]).rstrip("/")
    found = _request("GET", f"{base}/fingerprints?deviceType=desktop&osFamily={os_family}&browserProduct=chrome")
    if not found:
        raise SystemExit(f"Kameleo has no desktop Chrome fingerprint for {os_family}")
    newest = max(f["browser"]["major"] for f in found)
    return random.choice([f for f in found if f["browser"]["major"] == newest])
