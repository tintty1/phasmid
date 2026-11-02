"""Run the patch guards: one standalone script per shipped patch.

    uv run python -m guards --binary <chrome> [--profile <profile.json>] [--patch <area>/<surface>] [--area <area>]

Contract for a guard, ``tests/guards/<area>/<surface>.py``:
  - it receives ``--binary``, ``--profile`` (may be empty) and ``--work-dir``;
  - it launches the browser itself (``consistency.browser.Launch`` is the
    helper) and asserts the patch's observable effect;
  - it prints ``PASS: <what>`` or ``FAIL: <what>`` lines and exits 0 or 1;
  - it first proves its precondition took effect and fails with
    "vacuous" otherwise, so a guard cannot pass by testing nothing;
  - when the host cannot run it at all (a GPU guard without the profile's GPU
    class), it prints ``SKIP: <why>`` and exits 3, recorded as a skip.

``--headful`` is passed on to every guard; without it guards run headless.

Every patch in patches/phasmid/series names its guard in its header, and
tests/static/test_patches.py checks the file exists. Guards run in parallel;
each gets its own work directory and picks its own ports.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from results import SuiteResult  # noqa: E402

GUARD_DIR = Path(__file__).resolve().parent


def discover(area: str | None, patch: str | None) -> list[Path]:
    guards = sorted(p for p in GUARD_DIR.rglob("*.py") if p.name not in ("__main__.py", "__init__.py", "helpers.py") and not p.name.startswith("_"))
    if area:
        guards = [g for g in guards if g.parent.name == area]
    if patch:
        guards = [g for g in guards if str(g.relative_to(GUARD_DIR).with_suffix("")) == patch]
    return guards


def run_one(guard: Path, binary: Path, profile: Path | None, timeout: float, headful: bool = False) -> tuple[str, str, str]:
    test_id = str(guard.relative_to(GUARD_DIR).with_suffix(""))
    work = Path(tempfile.mkdtemp(prefix=f"phasmid-guard-{guard.stem}-"))
    cmd = [sys.executable, str(guard), "--binary", str(binary), "--profile", str(profile or ""), "--work-dir", str(work)]
    if headful:
        cmd.append("--headful")
    env = dict(os.environ, PYTHONPATH=f"{GUARD_DIR}{os.pathsep}{GUARD_DIR.parent}")
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(GUARD_DIR.parent))
        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode == 0:
            outcome = "pass"
        elif proc.returncode == 1:
            outcome = "fail"
        elif proc.returncode == 3:
            outcome = "skip"
        else:
            outcome = "error"
    except subprocess.TimeoutExpired as exc:
        output = f"timed out after {timeout}s\n{(exc.stdout or '')[-2000:] if isinstance(exc.stdout, str) else ''}"
        outcome = "error"
    elapsed = time.monotonic() - started
    tail = "\n".join(output.splitlines()[-6:])
    return test_id, outcome, f"{elapsed:.1f}s\n{tail}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--binary", required=True)
    p.add_argument("--profile")
    p.add_argument("--area")
    p.add_argument("--patch", help="<area>/<surface>")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--suite", default="guards", help="result record name")
    p.add_argument("--headful", action="store_true", help="pass --headful to every guard")
    args = p.parse_args(argv)

    binary = Path(args.binary)
    result = SuiteResult(suite=args.suite)
    if not binary.is_file():
        result.notes.append(f"binary not found: {binary}")
        result.finish("error").save()
        print(f"ERROR: binary not found: {binary}", file=sys.stderr)
        return 2
    guards = discover(args.area, args.patch)
    if not guards:
        result.notes.append("no guards matched the selection")
        result.finish("error").save()
        print("ERROR: no guards matched; an empty selection is not a pass", file=sys.stderr)
        return 2

    profile = Path(args.profile) if args.profile else None
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for test_id, outcome, note in pool.map(lambda g: run_one(g, binary, profile, args.timeout, args.headful), guards):
            result.record(test_id, outcome, note if outcome != "pass" else None)
            print(f"{outcome.upper():5s} {test_id}")
    result.finish().save()
    tally = {k: v for k, v in result.metrics.items() if k.startswith("tally_")}
    print(f"guards: {result.status} {tally}")
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
