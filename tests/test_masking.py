"""Masking report: verdict rules per site and report aggregation, no network."""

from __future__ import annotations

import json
from pathlib import Path

from masking.report import load_records, mode_splits, regressions, render
from masking.sites import SITES, brotector, browserscan, creepjs, creepjs_machine, fingerprint_com, iphey, load, pixelscan, sannysoft


def test_every_site_module_has_the_contract():
    for name in SITES:
        mod = load(name)
        assert mod.NAME == name
        assert mod.URL.startswith("https://")
        assert callable(mod.check) and callable(mod.judge)


def test_sannysoft_fails_on_any_failed_row_and_errors_on_empty_page():
    assert sannysoft.judge({"passed": 31, "failed": []}).verdict == "pass"
    r = sannysoft.judge({"passed": 27, "failed": ["HEADCHR_UA", "User Agent", "HEADCHR_UA"]})
    assert r.verdict == "fail" and r.score == 2 and r.signals["failed"] == ["HEADCHR_UA", "User Agent"]
    assert sannysoft.judge({"passed": 0, "failed": []}).verdict == "error"


def test_creepjs_machine_needs_a_pass_and_no_fail():
    assert creepjs_machine.judge({"passed": ["core: Linux"], "failed": []}).verdict == "pass"
    assert creepjs_machine.judge({"passed": ["core: Linux"], "failed": ["gpu: x"]}).verdict == "fail"
    assert creepjs_machine.judge({"passed": [], "failed": []}).verdict == "error"


def test_creepjs_judges_lies_and_records_trash_and_headless():
    data = {
        "totalLies": 2,
        "lies": {"Navigator.hardwareConcurrency": ["failed toString", "failed descriptor"]},
        "trash": [{"name": "WebGLRenderingContext.getParameter", "value": "suspicious gpu"}],
        "headlessRating": 67, "likeHeadlessRating": 50, "stealthRating": 0,
        "headless": ["hasHeadlessUA"], "likeHeadless": [], "stealth": [], "lieSections": ["Navigator"],
    }
    r = creepjs.judge(data)
    assert r.verdict == "fail" and r.score == 2
    assert r.signals["lie_items"] == ["Navigator.hardwareConcurrency: failed toString, failed descriptor"]
    assert r.signals["trash"] == 1 and r.signals["bot"].startswith("67% headless")
    assert creepjs.judge({**data, "totalLies": 0, "lies": {}}).verdict == "pass"
    assert creepjs.judge({"lies": {}}).verdict == "error"


def test_brotector_passes_only_on_zero_and_aggregates_rows():
    assert brotector.judge({"avg": "0", "rows": []}).verdict == "pass"
    r = brotector.judge({"avg": "1.00", "rows": [["runtime.enabled", "627.00", "webdriver", "1", "{}"]] * 3})
    assert r.verdict == "fail" and r.score == 1.0
    assert r.signals["detections"]["runtime.enabled"] == {"type": "webdriver", "count": 3, "max_score": 1.0}
    assert brotector.parse_number("2'797.78") == 2797.78
    assert brotector.judge({"avg": None, "rows": []}).verdict == "error"
    assert brotector.judge({"avg": "n/a", "rows": []}).verdict == "error"


def test_fingerprint_com_reads_v4_and_legacy_events():
    v4 = {"bot": "not_detected", "tampering": False, "virtual_machine": False, "vpn": True, "proxy": False, "suspect_score": 12}
    r = fingerprint_com.judge(v4)
    assert r.verdict == "pass" and r.signals["vpn"] is True and r.score == 12
    assert fingerprint_com.judge({**v4, "bot": "bad"}).verdict == "fail"
    assert fingerprint_com.judge({**v4, "tampering": True}).verdict == "fail"
    assert fingerprint_com.judge({**v4, "virtual_machine": True}).verdict == "fail"
    legacy = {"products": {
        "botd": {"data": {"bot": {"result": "notDetected"}}},
        "tampering": {"data": {"result": False}},
        "virtualMachine": {"data": {"result": False}},
        "proxy": {"data": {"result": True}},
    }}
    assert fingerprint_com.judge(legacy).verdict == "pass"
    assert fingerprint_com.judge({"event_id": "x"}).verdict == "error"


def test_browserscan_threshold_and_deductions():
    one = [{"head": "Different time zones -10%", "detail": "Your IP timezone does not match"}]
    r = browserscan.judge({"authenticity": "90%", "deductions": one, "bot_detection": "No"})
    assert r.verdict == "fail" and r.score == 90.0
    assert r.signals["deductions"][0] == {"item": "Different time zones", "percent": 10.0, "detail": "Your IP timezone does not match"}
    assert browserscan.judge({"authenticity": "98%", "deductions": one, "bot_detection": "No"}).verdict == "pass"
    assert browserscan.judge({"authenticity": "98%", "deductions": one * 2, "bot_detection": "No"}).verdict == "fail"
    assert browserscan.judge({"authenticity": None, "deductions": [], "bot_detection": "No"}).verdict == "error"
    assert browserscan.judge({"authenticity": "100%", "deductions": [], "bot_detection": None}).verdict == "error"


def _card(failed: bool, loading: bool = False) -> dict:
    return {"failed": failed, "loading": loading, "value": "x"}


def test_pixelscan_judges_four_cards_records_proxy_and_errors_on_misses():
    data = {t: _card(False) for t in pixelscan.JUDGED} | {"pxlscn-proxy": _card(True)}
    r = pixelscan.judge(data)
    assert r.verdict == "pass" and r.signals["recorded"]["proxy"]["result"] == "failed"
    assert pixelscan.judge(data | {"pxlscn-bot-detection": _card(True)}).verdict == "fail"
    assert pixelscan.judge(data | {"pxlscn-bot-detection": None}).verdict == "error"
    assert pixelscan.judge(data | {"pxlscn-bot-detection": _card(False, loading=True)}).verdict == "error"


def test_iphey_passes_on_not_detected_or_all_good():
    assert iphey.judge({"items": [{"label": "Status", "value": "Not detected", "level": None}]}).verdict == "pass"
    assert iphey.judge({"items": [{"label": "Browser", "value": "Fine", "level": "good"}], "mx": "100"}).verdict == "pass"
    r = iphey.judge({"items": [{"label": "Software", "value": "automated (jungle)", "level": "bad"}], "mx": "60"})
    assert r.verdict == "fail" and r.score == 60.0
    assert iphey.judge({"items": []}).verdict == "error"


def _write(root: Path, label: str, mode: str, site: str, verdict: str, score_text: str | None = None) -> None:
    d = root / label / mode
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{site}.json").write_text(json.dumps({"site": site, "mode": mode, "verdict": verdict, "score_text": score_text}))


def test_report_table_mode_split_and_regression(tmp_path: Path):
    _write(tmp_path, "stock", "headless", "sannysoft", "fail", "4 failed")
    _write(tmp_path, "stock", "headful", "sannysoft", "pass", "0 failed")
    _write(tmp_path, "phasmid", "headless", "sannysoft", "fail", "4 failed")
    _write(tmp_path, "phasmid", "headful", "sannysoft", "fail", "1 failed")
    _write(tmp_path, "phasmid", "xvfb", "sannysoft", "skip")
    _write(tmp_path, "phasmid", "headful", "iphey", "error")
    _write(tmp_path, "stock", "headful", "iphey", "fail", "MX 60")
    records = load_records(tmp_path)
    md = render(records)
    head = md.splitlines()[0]
    assert head.index("stock headful (baseline)") < head.index("stock headless") < head.index("phasmid headful")
    assert "| sannysoft | pass 0 failed | fail 4 failed | fail 1 failed | fail 4 failed | skip |" in md
    assert "| (informational) iphey | fail MX 60 | – | error | – | – |" in md
    assert mode_splits(records) == ["sannysoft [stock]: headful: pass 0 failed, headless: fail 4 failed"]
    assert regressions(records, "stock") == ["sannysoft [phasmid headful]: fail 1 failed vs stock pass 0 failed"]


def test_report_ignores_error_and_skip_for_mode_splits(tmp_path: Path):
    _write(tmp_path, "phasmid", "headful", "creepjs", "pass", "0 lies")
    _write(tmp_path, "phasmid", "headless", "creepjs", "error")
    _write(tmp_path, "phasmid", "xvfb", "creepjs", "skip")
    records = load_records(tmp_path)
    assert mode_splits(records) == [] and regressions(records, "stock") == []
    assert render({}) == "No masking records found.\n"
