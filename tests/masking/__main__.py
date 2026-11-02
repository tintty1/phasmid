"""CLI: python -m masking run|report ...

    uv run python -m masking run --binary <chrome> [--profile <json>] --mode {headful,headless,xvfb}
        [--site <name> ...] [--label <name>] [-o out/masking]
    uv run python -m masking remote --provider {kameleo,cloakbrowser} [--os linux] [--label <name>] [--site <name> ...]
    uv run python -m masking report out/masking [--markdown out/masking/report.md] [--baseline stock]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .report import load_records, render
from .remote import PROVIDERS, Remote, pick_fingerprint
from .runner import MODES, REMOTE_MODE, RunConfig, default_out, run_sync
from .sites import SITES


def cmd_run(args: argparse.Namespace) -> int:
    binary = Path(args.binary)
    if not binary.is_file():
        raise SystemExit(f"binary not found: {binary}")
    sites = args.site or list(SITES)
    unknown = [s for s in sites if s not in SITES]
    if unknown:
        raise SystemExit(f"unknown site(s) {unknown}; known: {', '.join(SITES)}")
    config = RunConfig(
        binary=binary,
        mode=args.mode,
        label=args.label,
        out=Path(args.out),
        sites=sites,
        profile=Path(args.profile) if args.profile else None,
        extra_args=args.arg,
        site_timeout=args.timeout,
    )
    suite = run_sync(config)
    path = suite.save()
    tally = {k: v for k, v in suite.metrics.items() if k.startswith("tally_")}
    print(f"{suite.suite}: {suite.status} {tally} -> {path}")
    return 0 if suite.status in ("pass", "skip") else 1


def cmd_remote(args: argparse.Namespace) -> int:
    sites = args.site or list(SITES)
    fingerprint = None
    if args.provider == "kameleo":
        chosen = pick_fingerprint(args.url, args.os)
        fingerprint = chosen["id"]
        print(f"fingerprint {fingerprint}: {chosen['os']['platform']} Chrome {chosen['browser']['version']}, {chosen['webglMeta']['renderer']}")
    config = RunConfig(
        binary=Path(f"{args.provider}:{args.url or 'default'}"),
        mode=REMOTE_MODE,
        label=args.label or f"{args.provider}-docker",
        out=Path(args.out),
        sites=sites,
        site_timeout=args.timeout,
        remote=lambda: Remote(args.provider, args.url, fingerprint),
    )
    suite = run_sync(config)
    if fingerprint:
        suite.metrics["fingerprint"] = fingerprint
    path = suite.save()
    tally = {k: v for k, v in suite.metrics.items() if k.startswith("tally_")}
    print(f"{suite.suite}: {suite.status} {tally} -> {path}")
    return 0 if suite.status in ("pass", "skip") else 1


def cmd_report(args: argparse.Namespace) -> int:
    md = render(load_records(Path(args.dir)), baseline=args.baseline)
    print(md, end="")
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="masking", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="visit every detection site with one binary in one mode")
    r.add_argument("--binary", required=True, help="path to chrome")
    r.add_argument("--profile", help="Phasmid profile JSON, passed as --phasmid-profile")
    r.add_argument("--mode", required=True, choices=MODES, help="headful: real $DISPLAY; headless: --headless=new; xvfb: a private Xvfb display")
    r.add_argument("--site", action="append", choices=SITES, help="run only this site (repeatable)")
    r.add_argument("--label", default="phasmid", help="column label, e.g. phasmid or stock")
    r.add_argument("--arg", action="append", default=[], help="extra browser switch (repeatable)")
    r.add_argument("--timeout", type=float, default=120.0, help="per-site timeout in seconds")
    r.add_argument("-o", "--out", default=str(default_out()), help="output root; records go to <out>/<label>/<mode>/")
    r.set_defaults(func=cmd_run)

    rm = sub.add_parser("remote", help="visit every detection site with a browser running in a vendor's container")
    rm.add_argument("--provider", required=True, choices=PROVIDERS)
    rm.add_argument("--url", help="vendor API base URL (default: the vendor's local port)")
    rm.add_argument("--os", default="linux", help="Kameleo fingerprint OS family (CloakBrowser picks its own)")
    rm.add_argument("--site", action="append", choices=SITES, help="run only this site (repeatable)")
    rm.add_argument("--label", help="column label (default: <provider>-docker)")
    rm.add_argument("--timeout", type=float, default=120.0, help="per-site timeout in seconds")
    rm.add_argument("-o", "--out", default=str(default_out()), help="output root; records go to <out>/<label>/docker/")
    rm.set_defaults(func=cmd_remote)

    rep = sub.add_parser("report", help="render the table from saved records")
    rep.add_argument("dir", help="output root of earlier runs")
    rep.add_argument("--markdown", help="also write the table here")
    rep.add_argument("--baseline", default="stock", help="label of the stock Chrome baseline")
    rep.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
