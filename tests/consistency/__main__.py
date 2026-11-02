"""CLI: python -m consistency dump|diff|contexts ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser import find_default_binary
from .capture import capture, validate
from .collect import serve
from .compare import compare, format_compare
from .contexts import cross_context_report, format_cross_context
from .diff import diff_dumps, format_diff
from .dump import run_dump, write_dump


def _env_pairs(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--env expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def cmd_dump(args: argparse.Namespace) -> int:
    binary = Path(args.binary) if args.binary else find_default_binary()
    if binary is None or not binary.exists():
        raise SystemExit("no binary found; pass --binary")
    dump = run_dump(
        binary,
        headless=not args.headful,
        profile=Path(args.profile) if args.profile else None,
        extra_args=args.arg,
        env=_env_pairs(args.env),
        stun=args.stun,
        timeout=args.timeout,
    )
    out = Path(args.out)
    write_dump(dump, out)
    errors = dump["context_errors"]
    print(f"wrote {out} ({dump['harness']['binary_version']}, {dump['harness']['duration_s']}s)")
    if errors:
        print(f"context errors: {json.dumps(errors, indent=1)}", file=sys.stderr)
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    report = cross_context_report(dump, baseline)
    print(format_cross_context(report))
    return 1 if errors else 0


def cmd_diff(args: argparse.Namespace) -> int:
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    d = diff_dumps(a, b, ignore_volatile=not args.all)
    print(format_diff(d, Path(args.a).name, Path(args.b).name))
    return 1 if d else 0


def cmd_contexts(args: argparse.Namespace) -> int:
    dump = json.loads(Path(args.dump).read_text())
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    report = cross_context_report(dump, baseline)
    print(format_cross_context(report))
    return 1 if report["disagreements"] else 0


def cmd_capture(args: argparse.Namespace) -> int:
    dump = json.loads(Path(args.dump).read_text())
    nav = dump["contexts"]["main"].get("navigator") or {}
    brands = [b.get("brand") for b in (nav.get("userAgentData") or {}).get("brands") or []]
    if "Google Chrome" not in brands:
        raise SystemExit(f"capture builds Google Chrome profiles; this dump is from {dump['harness'].get('binary_version') or nav.get('userAgent')}")
    if dump["harness"].get("embedded"):
        raise SystemExit("this dump was taken inside an iframe, where window size, permissions and DRM differ from a top-level page")
    gpu_ids = _env_pairs(args.gpu)
    # A dump from collect.html carries the facts its operator typed in; flags win.
    operator = dump.get("operator") or {}
    os_version = args.os_version or operator.get("os_version")
    if not os_version:
        raise SystemExit("--os-version is required when the dump does not carry it")
    try:
        profile = capture(
            dump,
            profile_id=args.id,
            os_version=os_version,
            gpu_ids=gpu_ids,
            seed=args.seed,
            physical_memory_mb=args.physical_memory_mb or operator.get("physical_memory_mb"),
            disk_total_bytes=args.disk_total_bytes or operator.get("disk_total_bytes"),
            note=args.note or operator.get("note") or "",
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    errors = validate(profile)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}")
    if errors:
        print("profile does not validate:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("profile validates against profiles/schema.json")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    serve(args.port, args.cross_port, args.cross, Path(args.out), args.token)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    rc = 0
    for path in args.profile:
        errors = validate(json.loads(Path(path).read_text()))
        if errors:
            rc = 1
            print(f"{path}: {len(errors)} errors")
            for e in errors:
                print(f"  {e}")
        else:
            print(f"{path}: ok")
    return rc


def cmd_compare(args: argparse.Namespace) -> int:
    dump = json.loads(Path(args.dump).read_text())
    profile = json.loads(Path(args.profile).read_text())
    errors = validate(profile)
    if errors:
        print(f"profile does not validate ({len(errors)} errors); comparing anyway", file=sys.stderr)
    report = compare(dump, profile)
    print(format_compare(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str) + "\n")
    return 1 if report["failures"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="consistency", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="launch a binary and dump every surface from every context")
    d.add_argument("--binary", help="path to chrome (default: ../dist/*/chrome, then system Chrome)")
    d.add_argument("--profile", help="Phasmid profile JSON, passed as --phasmid-profile")
    d.add_argument("--arg", action="append", default=[], help="extra browser switch (repeatable)")
    d.add_argument("--env", action="append", default=[], help="KEY=VALUE for the browser environment (repeatable)")
    d.add_argument("--headful", action="store_true", help="run with a window instead of --headless=new")
    d.add_argument("--stun", help="STUN URL for WebRTC candidate gathering, e.g. stun:stun.l.google.com:19302")
    d.add_argument("--timeout", type=float, default=90.0)
    d.add_argument("--baseline", help="stock Chrome dump; cross-context splits it also has are not reported")
    d.add_argument("-o", "--out", required=True, help="output JSON path")
    d.set_defaults(func=cmd_dump)

    f = sub.add_parser("diff", help="compare two dumps")
    f.add_argument("a")
    f.add_argument("b")
    f.add_argument("--all", action="store_true", help="include volatile keys (timing, usage, candidates)")
    f.set_defaults(func=cmd_diff)

    c = sub.add_parser("contexts", help="report values that differ between contexts within one dump")
    c.add_argument("dump")
    c.add_argument("--baseline", help="stock Chrome dump; cross-context splits it also has are not reported")
    c.set_defaults(func=cmd_contexts)

    cp = sub.add_parser("capture", help="turn a dump from a real machine into a profile")
    cp.add_argument("dump")
    cp.add_argument("--id", required=True, help="profile id, e.g. win11-rtx3060-1080p")
    cp.add_argument("--os-version", help="marketing OS version, e.g. 11, 15.1, arch (default: the dump's operator.os_version)")
    cp.add_argument("--gpu", action="append", default=[], help="KEY=VALUE from chrome://gpu: vendor_id, device_id, vendor_name, device_name, driver_vendor, driver_version, gl_vendor, gl_renderer, gl_version (ids default to what a Windows or Apple Silicon renderer string determines)")
    cp.add_argument("--seed", help="32 hex chars; random when omitted")
    cp.add_argument("--physical-memory-mb", type=int, help="default: the dump's operator value, else derived from deviceMemory and jsHeapSizeLimit")
    cp.add_argument("--disk-total-bytes", type=int, help="total bytes of the volume holding the browser profile (df -B1 --output=size ~; default: the dump's operator value, else derived from the quota)")
    cp.add_argument("--note")
    cp.add_argument("-o", "--out", required=True)
    cp.set_defaults(func=cmd_capture)

    co = sub.add_parser("collect", help="serve collect.html for remote machines and save the dump of every visit")
    co.add_argument("--port", type=int, default=8800, help="page port; expose it over https")
    co.add_argument("--cross-port", type=int, default=8801, help="cross-origin frame port; expose it over https too")
    co.add_argument("--cross", help="public https origin of --cross-port, e.g. https://host.tailnet.ts.net:8443 (default: http://localhost:<cross-port>)")
    co.add_argument("--token", help="secret the page URL must carry as ?k= (default: random, printed)")
    co.add_argument("-o", "--out", default="out/captures", help="directory for the dumps")
    co.set_defaults(func=cmd_collect)

    v = sub.add_parser("validate", help="validate profiles against profiles/schema.json")
    v.add_argument("profile", nargs="+")
    v.set_defaults(func=cmd_validate)

    cm = sub.add_parser("compare", help="compare a dump against a profile, every field in every context")
    cm.add_argument("dump")
    cm.add_argument("profile")
    cm.add_argument("--json", help="write the full report here")
    cm.set_defaults(func=cmd_compare)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
