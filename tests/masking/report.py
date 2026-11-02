"""Aggregate per-site records into one markdown table.

Rows are sites, columns are every (label, mode) found under the output
directory. Below the table: sites whose modes disagree within one label
(headful, headless and Xvfb must be indistinguishable) and sites where a
label fails while the stock baseline passes in the same mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import MODES
from .sites import SITES, informational

Records = dict[tuple[str, str], dict[str, dict[str, Any]]]


def load_records(root: Path) -> Records:
    """Read ``<root>/<label>/<mode>/<site>.json`` into {(label, mode): {site: record}}."""
    out: Records = {}
    for path in sorted(root.glob("*/*/*.json")):
        label, mode = path.parent.parent.name, path.parent.name
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        out.setdefault((label, mode), {})[record.get("site", path.stem)] = record
    return out


def cell(record: dict[str, Any] | None) -> str:
    if record is None:
        return "–"
    verdict = record.get("verdict", "error")
    score = record.get("score_text")
    if verdict in ("pass", "fail") and score:
        return f"{verdict} {score}"
    return verdict


def columns(records: Records, baseline: str) -> list[tuple[str, str]]:
    def key(col: tuple[str, str]) -> tuple[int, str, int]:
        label, mode = col
        return (0 if label == baseline else 1, label, MODES.index(mode) if mode in MODES else len(MODES))
    return sorted(records, key=key)


def site_order(records: Records) -> list[str]:
    seen = {site for per in records.values() for site in per}
    return [s for s in SITES if s in seen] + sorted(seen - set(SITES))


def row_name(site: str) -> str:
    return f"(informational) {site}" if informational(site) else site


def mode_splits(records: Records) -> list[str]:
    """Sites whose pass/fail verdict differs between modes of the same label.

    ``error`` and ``skip`` carry no verdict about the browser, so they are
    not counted as a disagreement.
    """
    out = []
    labels = sorted({label for label, _ in records})
    for site in site_order(records):
        for label in labels:
            verdicts = {
                mode: per[site]["verdict"]
                for (lab, mode), per in records.items()
                if lab == label and site in per and per[site].get("verdict") in ("pass", "fail")
            }
            if len(set(verdicts.values())) > 1:
                detail = ", ".join(f"{m}: {cell(records[(label, m)][site])}" for m in sorted(verdicts, key=lambda m: MODES.index(m) if m in MODES else 99))
                out.append(f"{row_name(site)} [{label}]: {detail}")
    return out


def regressions(records: Records, baseline: str) -> list[str]:
    """Sites where a label fails in a mode while the baseline passes in that mode."""
    out = []
    for site in site_order(records):
        for (label, mode), per in sorted(records.items()):
            if label == baseline or site not in per or per[site].get("verdict") != "fail":
                continue
            base = records.get((baseline, mode), {}).get(site)
            if base is not None and base.get("verdict") == "pass":
                out.append(f"{row_name(site)} [{label} {mode}]: {cell(per[site])} vs {baseline} {cell(base)}")
    return out


def render(records: Records, baseline: str = "stock") -> str:
    if not records:
        return "No masking records found.\n"
    cols = columns(records, baseline)
    head = ["Site"] + [f"{label} {mode}" + (" (baseline)" if label == baseline else "") for label, mode in cols]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for site in site_order(records):
        lines.append("| " + " | ".join([row_name(site)] + [cell(records[c].get(site)) for c in cols]) + " |")
    splits = mode_splits(records)
    regs = regressions(records, baseline)
    lines += ["", "### Mode split", ""] + ([f"- {s}" for s in splits] or ["None."])
    lines += ["", f"### Regression vs {baseline}", ""] + ([f"- {r}" for r in regs] or ["None."])
    return "\n".join(lines) + "\n"
