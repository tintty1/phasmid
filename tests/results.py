"""Evidence records for the test pipeline.

Every suite writes one JSON record. The summary job reads them all and
treats a required suite with no record as a failure: a suite that did not
run has not passed. Records carry the run id so a stale file from another
run cannot satisfy a gate.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

RESULTS_DIR = Path(os.environ.get("PHASMID_RESULTS_DIR", Path(__file__).parent / "out" / "results"))

Outcome = str  # "pass" | "fail" | "error" | "skip"


def run_id() -> str:
    if os.environ.get("PHASMID_RUN_ID"):
        return os.environ["PHASMID_RUN_ID"]
    if os.environ.get("GITHUB_RUN_ID"):
        return f"{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return f"local-{socket.gethostname()}-{os.getpid()}"


@dataclass
class SuiteResult:
    suite: str
    status: str = "running"  # pass | fail | error | skip
    started: float = field(default_factory=time.time)
    finished: float | None = None
    tests: dict[str, Outcome] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    run_id: str = field(default_factory=run_id)
    schema: int = 1

    def record(self, test_id: str, outcome: Outcome, note: str | None = None) -> None:
        # Best outcome wins: a retry cannot downgrade a pass, and a pass
        # cannot hide an earlier error on a different test.
        rank = {"pass": 3, "skip": 2, "fail": 1, "error": 0}
        prev = self.tests.get(test_id)
        if prev is None or rank[outcome] > rank[prev]:
            self.tests[test_id] = outcome
        if note:
            self.notes.append(f"{test_id}: {note}")

    def finish(self, status: str | None = None) -> "SuiteResult":
        self.finished = time.time()
        tally = {k: sum(1 for v in self.tests.values() if v == k) for k in ("pass", "fail", "error", "skip")}
        self.metrics.update({f"tally_{k}": v for k, v in tally.items()})
        if status is None:
            if tally["error"]:
                status = "error"
            elif tally["fail"]:
                status = "fail"
            elif tally["pass"]:
                status = "pass"
            else:
                status = "skip"
        self.status = status
        return self

    def save(self, directory: Path = RESULTS_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.suite}.json"
        path.write_text(json.dumps(asdict(self), indent=1, sort_keys=True) + "\n")
        return path


def load_all(directory: Path = RESULTS_DIR) -> dict[str, dict]:
    out = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        out[path.stem] = json.loads(path.read_text())
    return out
