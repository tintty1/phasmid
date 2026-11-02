"""Fingerprint inspector: which Web APIs a page reads, with arguments and results.

    uv run python -m inspector run --binary <chrome> [--profile <p.json>] --url <url> [--url ...] -o out/site
    uv run python -m inspector report out/site.json            # re-render the Markdown summary
    uv run python -m inspector diff out/stock.json out/phasmid.json

`run` launches the binary (with --phasmid-profile when given), attaches to
the browser target only, starts CDP Tracing with the category
disabled-by-default-phasmid.fingerprint before the first page opens, opens
each URL in a new tab, and writes <out>.json (the raw trace and the parsed
calls) and <out>.md (per thread: API, arguments, result, caller URL:line).
A page that sets its title to the --until-title value ends its wait early.
Exit 1 if the browser reported data loss or the trace holds no event (a
binary without devtools/fingerprint-trace.patch).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .trace import collect, diff, load, markdown, parse


def cmd_run(args: argparse.Namespace) -> int:
    trace = collect(Path(args.binary), args.url, profile=Path(args.profile) if args.profile else None,
                    headless=not args.headful, until_title=args.until_title, wait=args.wait, extra_args=args.arg)
    parsed = parse(trace)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps({**trace, "parsed": parsed}, indent=1))
    out.with_suffix(".md").write_text(markdown(trace, parsed))
    print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.md')}: {parsed['event_count']} events, "
          f"{len(parsed['calls'])} calls, data loss {parsed['data_loss']}")
    if parsed["data_loss"]:
        print("data loss: raise the buffer or trace fewer pages per run", file=sys.stderr)
        return 1
    return 0 if parsed["event_count"] else 1


def cmd_report(args: argparse.Namespace) -> int:
    trace, parsed = load(Path(args.trace))
    print(markdown(trace, parsed))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    _, a = load(Path(args.a))
    _, b = load(Path(args.b))
    lines = diff(a, b)
    print("\n".join(lines) if lines else "no differences")
    return 1 if lines else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m inspector", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="trace one or more pages")
    r.add_argument("--binary", required=True)
    r.add_argument("--profile")
    r.add_argument("--url", action="append", required=True)
    r.add_argument("--until-title", help="stop waiting once the page title equals this")
    r.add_argument("--wait", type=float, default=20, help="seconds per page (default 20)")
    r.add_argument("--headful", action="store_true")
    r.add_argument("--arg", action="append", default=[], help="extra browser switch")
    r.add_argument("-o", "--out", required=True, help="output path without extension")
    r.set_defaults(fn=cmd_run)
    rep = sub.add_parser("report", help="print the Markdown summary of a saved trace")
    rep.add_argument("trace")
    rep.set_defaults(fn=cmd_report)
    d = sub.add_parser("diff", help="what differs between two traces (same pages)")
    d.add_argument("a")
    d.add_argument("b")
    d.set_defaults(fn=cmd_diff)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
