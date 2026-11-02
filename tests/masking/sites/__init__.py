"""Site modules, in report order.

Each module exposes ``NAME``, ``URL``, ``SCORE_FORMAT`` (how its score reads
in the table), an async ``check(page) -> SiteResult`` that navigates and
extracts, and a pure ``judge(data) -> SiteResult``. ``INFORMATIONAL = True``
marks a site whose verdict depends on the network rather than the browser.
"""

from __future__ import annotations

import importlib
from types import ModuleType

SITES = [
    "sannysoft",
    "creepjs_machine",
    "creepjs",
    "brotector",
    "fingerprint_com",
    "browserscan",
    "pixelscan",
    "iphey",
]


def load(name: str) -> ModuleType:
    if name not in SITES:
        raise KeyError(f"unknown site {name!r}; known: {', '.join(SITES)}")
    return importlib.import_module(f"{__name__}.{name}")


def informational(name: str) -> bool:
    return bool(getattr(load(name), "INFORMATIONAL", False)) if name in SITES else False
