"""Structural diff of two dumps, with volatile keys ignored by default."""

from __future__ import annotations

import re
from typing import Any

# Keys whose values legitimately change between runs of the same binary.
VOLATILE = [
    re.compile(r"^harness\."),
    re.compile(r"^stderr_tail$"),
    re.compile(r"\.meta\.origin$"),
    re.compile(r"\.storage\.(usage|quota)$"),  # quota is derived from free disk at the time
    re.compile(r"\.storage\.usageDetails$"),
    re.compile(r"\.intl\.timezoneOffsetNow$"),
    re.compile(r"\.navigator\.connection\.(rtt|downlink)$"),
    re.compile(r"\.navigator\.battery\."),
    re.compile(r"\.webrtc\.(candidates|addresses)"),
    re.compile(r"\.webrtc\.sdpLines$"),
    re.compile(r"\.errors\.stackDepth$"),
    re.compile(r"\.audio\.state$"),
    re.compile(r"\.mediaDevices\.devices\.\d+\.(deviceIdLength|groupIdLength)$"),
    re.compile(r"\.screen\.(innerWidth|innerHeight|outerWidth|outerHeight|screenX|screenY|screenLeft|screenTop|visualViewport|documentClient)"),
    re.compile(r"\.mediaQueries\.widthMatchesInner$"),
    re.compile(r"\.rects\."),  # position depends on viewport; compared per-context instead
    re.compile(r"^headers\.[^.]+\.(rtt|downlink|ect|priority|sec-ch-viewport-width|viewport-width|sec-ch-viewport-height)$"),
]

LIST_LEN = "__len"


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten to dotted keys. Lists of scalars stay whole; lists of
    containers expand by index and also record their length under
    ``<key>.__len`` so a length change can be reported once."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix] = obj
        else:
            out[f"{prefix}.{LIST_LEN}"] = len(obj)
            for i, v in enumerate(obj):
                out.update(flatten(v, f"{prefix}.{i}"))
    else:
        out[prefix] = obj
    return out


def is_volatile(key: str) -> bool:
    return any(p.search(key) for p in VOLATILE)


def diff_dumps(a: dict, b: dict, ignore_volatile: bool = True) -> dict[str, tuple[Any, Any]]:
    fa, fb = flatten(a), flatten(b)
    out: dict[str, tuple[Any, Any]] = {}
    # Lists whose length changed are reported once, not per element.
    collapsed: list[str] = []
    for key in sorted(set(fa) | set(fb)):
        if key.endswith(f".{LIST_LEN}") and fa.get(key) != fb.get(key):
            base = key[: -len(LIST_LEN) - 1]
            collapsed.append(base + ".")
            out[base] = (f"<list of {fa.get(key, 0)}>", f"<list of {fb.get(key, 0)}>")
    for key in sorted(set(fa) | set(fb)):
        if key.endswith(f".{LIST_LEN}"):
            continue
        if ignore_volatile and is_volatile(key):
            continue
        if any(key.startswith(c) for c in collapsed):
            continue
        va, vb = fa.get(key, "<absent>"), fb.get(key, "<absent>")
        if va != vb:
            out[key] = (va, vb)
    return dict(sorted(out.items()))


def _short(v: Any, limit: int = 120) -> str:
    s = repr(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def format_diff(d: dict[str, tuple[Any, Any]], name_a: str, name_b: str) -> str:
    if not d:
        return "no differences"
    lines = [f"{len(d)} differences ({name_a} -> {name_b})"]
    for key, (va, vb) in d.items():
        lines.append(f"  {key}")
        lines.append(f"    - {_short(va)}")
        lines.append(f"    + {_short(vb)}")
    return "\n".join(lines)
