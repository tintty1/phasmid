"""results.py and summarize.py: a missing or stale suite never passes."""

from __future__ import annotations

from pathlib import Path

from results import SuiteResult, load_all
from summarize import judge


def test_record_keeps_best_outcome_and_tallies(tmp_path: Path):
    r = SuiteResult(suite="demo", run_id="run-1")
    r.record("a", "fail", "first try")
    r.record("a", "pass")
    r.record("b", "error", "boom")
    r.finish().save(tmp_path)
    loaded = load_all(tmp_path)["demo"]
    assert loaded["tests"] == {"a": "pass", "b": "error"}
    assert loaded["status"] == "error"
    assert loaded["metrics"]["tally_pass"] == 1 and loaded["metrics"]["tally_error"] == 1


def test_missing_required_suite_fails(tmp_path: Path):
    SuiteResult(suite="static", run_id="run-1").finish("pass").save(tmp_path)
    ok, problems, _rows = judge(load_all(tmp_path), ["static", "guards"], [], "run-1")
    assert not ok
    assert any("guards: no result file" in p for p in problems)


def test_stale_record_fails(tmp_path: Path):
    SuiteResult(suite="static", run_id="old-run").finish("pass").save(tmp_path)
    ok, problems, _ = judge(load_all(tmp_path), ["static"], [], "run-1")
    assert not ok and "old-run" in problems[0]


def test_skip_only_passes_when_allowed(tmp_path: Path):
    SuiteResult(suite="stealth", run_id="r").finish("skip").save(tmp_path)
    assert not judge(load_all(tmp_path), ["stealth"], [], "r")[0]
    assert judge(load_all(tmp_path), ["stealth"], ["stealth"], "r")[0]


def test_all_pass(tmp_path: Path):
    for s in ("static", "harness"):
        r = SuiteResult(suite=s, run_id="r")
        r.record("x", "pass")
        r.finish().save(tmp_path)
    ok, problems, rows = judge(load_all(tmp_path), ["static", "harness"], [], "r")
    assert ok and not problems and {r["suite"] for r in rows} == {"static", "harness"}
