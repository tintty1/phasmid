"""Merge suite records into one verdict and one Markdown table.

    uv run python summarize.py --require static harness guards consistency --markdown out/summary.md

Exit 0 only when every required suite has a record from this run with
status pass. Missing, stale, failed or errored suites fail the verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from results import RESULTS_DIR, load_all, run_id


def judge(records: dict[str, dict], required: list[str], allow_skip: list[str], expected_run: str | None) -> tuple[bool, list[str], list[dict]]:
    problems: list[str] = []
    rows: list[dict] = []
    for suite in required:
        rec = records.get(suite)
        if rec is None:
            problems.append(f"{suite}: no result file (a suite that did not run has not passed)")
            rows.append({"suite": suite, "status": "missing", "detail": "no record"})
            continue
        if expected_run and rec.get("run_id") != expected_run:
            problems.append(f"{suite}: record is from run {rec.get('run_id')}, expected {expected_run}")
        status = rec.get("status")
        tally = {k: rec.get("metrics", {}).get(f"tally_{k}", 0) for k in ("pass", "fail", "error", "skip")}
        detail = f"{tally['pass']} passed, {tally['fail']} failed, {tally['error']} errors, {tally['skip']} skipped"
        rows.append({"suite": suite, "status": status, "detail": detail, "notes": rec.get("notes", [])})
        if status == "pass":
            continue
        if status == "skip" and suite in allow_skip:
            continue
        problems.append(f"{suite}: {status} ({detail})")
    for suite, rec in records.items():
        if suite not in required:
            rows.append({"suite": suite, "status": rec.get("status"), "detail": "not required", "notes": rec.get("notes", [])})
    return not problems, problems, rows


ICON = {"pass": "✅", "fail": "❌", "error": "💥", "skip": "⏭️", "missing": "🚫", None: "❓"}


def render(ok: bool, problems: list[str], rows: list[dict]) -> str:
    lines = ["## " + ("✅ All tests passed" if ok else "❌ Tests failed"), "", "| Suite | Result | Detail |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['suite']}` | {ICON.get(r['status'], '❓')} {r['status']} | {r['detail']} |")
    if problems:
        lines += ["", "### What failed", ""] + [f"- {p}" for p in problems]
    failing_notes = [n for r in rows if r.get("status") in ("fail", "error") for n in r.get("notes", [])[:10]]
    if failing_notes:
        lines += ["", "<details><summary>Notes from failing suites</summary>", ""] + [f"- {n}" for n in failing_notes] + ["", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", default=str(RESULTS_DIR))
    p.add_argument("--require", nargs="+", required=True, help="suites that must have passed")
    p.add_argument("--allow-skip", nargs="*", default=[], help="required suites that may report skip")
    p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID") and run_id(), help="reject records from other runs")
    p.add_argument("--markdown", help="write the table here")
    p.add_argument("--json", help="write the verdict here")
    args = p.parse_args(argv)

    records = load_all(Path(args.results))
    ok, problems, rows = judge(records, args.require, args.allow_skip, args.run_id)
    md = render(ok, problems, rows)
    print(md)
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a").write(md)
    if args.json:
        Path(args.json).write_text(json.dumps({"ok": ok, "problems": problems, "rows": rows}, indent=1) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
