"""Unit tests for history_index.py + R14 streak-break in workflow_rules.py."""
from __future__ import annotations

import json

from argus.domain import history_index as hi
from argus.domain import workflow_rules as wr


def _entry(date: str, status: str, e_key: str = "QA-E000",
           folder: str = "TestFolder",
           tester: str = "Tester") -> hi.HistoryEntry:
    return hi.HistoryEntry(date=date, status=status, e_key=e_key,
                            folder=folder, tester=tester)


def _meta(*, tc_key: str, mp: str | None, status: str,
          execution_date: str = "2026-05-27T10:00:00Z",
          comment: str = "",
          trace_links: list | None = None,
          tr_id: int | None = None) -> dict:
    """Minimal metadata dict matching build_metadata's output shape with the fields R14 cares about."""
    tr: dict = {
        "test_case_key": tc_key,
        "marketplace": mp,
        "status": status,
        "execution_date": execution_date,
        "comment": comment,
        "trace_links": trace_links or [],
    }
    if tr_id is not None:
        tr["id"] = tr_id
    return {"test_results": [tr]}


class _StubIndex:
    """Tiny stand-in for HistoryIndex so tests don't touch disk."""
    def __init__(self, buckets: dict[str, list[hi.HistoryEntry]]):
        self._buckets = buckets

    def get_history(self, tc_key: str, mp: str | None) -> list[hi.HistoryEntry]:
        bucket_key = f"{tc_key}|{mp or '_'}"
        return list(self._buckets.get(bucket_key, []))


def test_is_streak_true_at_default_threshold():
    history = [_entry(f"2026-05-{20-i:02d}T00:00:00Z", "Blocked",
                       e_key=f"E{i}") for i in range(10)]
    assert hi.HistoryIndex.is_streak(history, "Blocked") is True


def test_is_streak_false_below_threshold():
    history = (
        [_entry(f"2026-05-{20-i:02d}T00:00:00Z", "Blocked", e_key=f"E{i}")
         for i in range(7)]
        + [_entry(f"2026-05-{13-i:02d}T00:00:00Z", "Pass", e_key=f"P{i}")
           for i in range(3)]
    )
    assert hi.HistoryIndex.is_streak(history, "Blocked") is False


def test_is_streak_returns_false_when_history_too_short():
    history = [_entry("2026-05-20T00:00:00Z", "Blocked", e_key="E1")] * 4
    assert hi.HistoryIndex.is_streak(history, "Blocked") is False


def test_is_streak_excludes_current_e_key():
    history = (
        [_entry("2026-05-25T00:00:00Z", "Pass", e_key="CURRENT")]
        + [_entry(f"2026-05-{24-i:02d}T00:00:00Z", "Blocked",
                   e_key=f"E{i}")
           for i in range(8)]
        + [_entry("2026-05-10T00:00:00Z", "Pass", e_key="OLD_PASS")]
    )
    assert hi.HistoryIndex.is_streak(history, "Blocked",
                                      exclude_e_key="CURRENT") is True


def test_is_streak_n_a_and_not_applicable_canonicalised():
    history = (
        [_entry(f"2026-05-{20-i:02d}T00:00:00Z", "N/A", e_key=f"E{i}")
         for i in range(4)]
        + [_entry(f"2026-05-{16-i:02d}T00:00:00Z", "Not Applicable",
                   e_key=f"E{i+4}")
           for i in range(4)]
    )
    assert hi.HistoryIndex.is_streak(history, "Not Applicable",
                                      window=8, threshold=8) is True
    assert hi.HistoryIndex.is_streak(history, "N/A",
                                      window=8, threshold=8) is True


def _blocked_streak_buckets(tc_key: str = "QA-T1",
                             mp: str = "BR") -> dict:
    """10 Blocked entries forming a clear streak."""
    history = [
        _entry(f"2026-05-{25-i:02d}T00:00:00Z", "Blocked", e_key=f"E{i}")
        for i in range(10)
    ]
    return {f"{tc_key}|{mp}": history}


def test_r14a_fires_pass_after_blocked_streak():
    buckets = _blocked_streak_buckets()
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Pass",
                     comment="actually verified")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "R14a"
    assert f["severity"] == "medium"
    assert f["source"] == "rule"
    assert "Blocked" in f["description"]
    assert "Pass" in f["description"]


def test_r14a_escalates_to_high_without_comment_or_traces():
    buckets = _blocked_streak_buckets()
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Pass")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert "No tester comment" in findings[0]["description"]


def test_r14_description_tracks_streak_policy(monkeypatch):
    """Verify R14 descriptions reflect configured streak thresholds."""
    monkeypatch.setattr(hi, "_DEFAULT_THRESHOLD", 6)
    monkeypatch.setattr(hi, "_DEFAULT_WINDOW", 7)
    buckets = _blocked_streak_buckets()
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Pass")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert findings
    assert "6 of the last 7" in findings[0]["description"]
    assert "8 of the last 10" not in findings[0]["description"]


def test_r14a_does_not_fire_on_continuing_blocked():
    buckets = _blocked_streak_buckets()
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Blocked")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert findings == []


def test_r14a_fires_on_n_a_streak_break_too():
    """N/A is treated as Blocked-like for R14a purposes."""
    history = [
        _entry(f"2026-05-{25-i:02d}T00:00:00Z", "Not Applicable",
                e_key=f"E{i}")
        for i in range(10)
    ]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Pass")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R14a"


def _pass_streak_buckets(tc_key: str = "QA-T2",
                          mp: str = "DE") -> dict:
    history = [
        _entry(f"2026-05-{25-i:02d}T00:00:00Z", "Pass", e_key=f"E{i}")
        for i in range(10)
    ]
    return {f"{tc_key}|{mp}": history}


def test_r14b_fires_blocked_after_pass_streak():
    buckets = _pass_streak_buckets()
    metadata = _meta(tc_key="QA-T2", mp="DE", status="Blocked",
                     comment="env down today")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R14b"
    assert findings[0]["severity"] == "medium"


def test_r14b_fires_not_applicable_after_pass_streak():
    buckets = _pass_streak_buckets()
    metadata = _meta(tc_key="QA-T2", mp="DE", status="Not Applicable")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R14b"
    assert findings[0]["severity"] == "high"


def test_r14b_does_not_fire_on_fail():
    buckets = _pass_streak_buckets()
    metadata = _meta(tc_key="QA-T2", mp="DE", status="Fail")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert findings == []


def test_no_fire_when_history_below_minimum():
    """History needs >= 5 entries to even consider a streak."""
    history = [
        _entry(f"2026-05-{20-i:02d}T00:00:00Z", "Blocked", e_key=f"E{i}")
        for i in range(4)
    ]
    buckets = {"QA-T3|BR": history}
    metadata = _meta(tc_key="QA-T3", mp="BR", status="Pass")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert findings == []


def test_no_fire_when_metadata_missing_test_case_key():
    metadata = _meta(tc_key="", mp="BR", status="Pass")
    findings = wr.check_streak_break(metadata, _StubIndex({}))
    assert findings == []


def test_no_fire_when_metadata_missing_status():
    metadata = _meta(tc_key="QA-T1", mp="BR", status="")
    findings = wr.check_streak_break(metadata, _StubIndex({}))
    assert findings == []


def test_marketplace_buckets_are_independent():
    buckets = {
        "QA-T1|BR": [
            _entry(f"2026-05-{25-i:02d}T00:00:00Z", "Blocked", e_key=f"BR{i}")
            for i in range(10)
        ],
        "QA-T1|DE": [
            _entry(f"2026-05-{25-i:02d}T00:00:00Z", "Pass", e_key=f"DE{i}")
            for i in range(10)
        ],
    }
    idx = _StubIndex(buckets)

    findings_br = wr.check_streak_break(
        _meta(tc_key="QA-T1", mp="BR", status="Pass"), idx)
    findings_de = wr.check_streak_break(
        _meta(tc_key="QA-T1", mp="DE", status="Pass"), idx)

    assert len(findings_br) == 1 and findings_br[0]["rule"] == "R14a"
    assert findings_de == []


def test_read_metadata_entry_populates_marketplace_placeholder(tmp_path):
    import json
    folder_dir = tmp_path / "output" / "Some_Folder" / "QA-E1"
    folder_dir.mkdir(parents=True)
    meta_path = folder_dir / "metadata.json"
    meta_path.write_text(json.dumps({
        "test_results": [{
            "test_case_key": "QA-T9",
            "marketplace": None,
            "status": "Pass",
            "execution_date": "2026-05-27T10:00:00Z",
            "executed_by_name": "Tester X",
        }]
    }))
    result = hi.HistoryIndex._read_metadata_entry(meta_path)
    assert result is not None
    tc_key, mp, entry = result
    assert tc_key == "QA-T9"
    assert mp == "_"
    assert entry.status == "Pass"
    assert entry.e_key == "QA-E1"


def test_read_metadata_entry_returns_none_on_missing_test_case_key(tmp_path):
    import json
    folder_dir = tmp_path / "output" / "F" / "QA-E2"
    folder_dir.mkdir(parents=True)
    meta_path = folder_dir / "metadata.json"
    meta_path.write_text(json.dumps({
        "test_results": [{"status": "Pass"}]
    }))
    assert hi.HistoryIndex._read_metadata_entry(meta_path) is None


def test_refresh_skips_stale_backups_and_replay(tmp_path):
    def plant(*parts, e_key):
        d = (tmp_path / "output").joinpath(*parts, e_key)
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps(_meta(
            tc_key="QA-T1", mp="US", status="Pass")))
        return d

    plant("Sprint-47", e_key="QA-E1")
    plant("_stale_backups", e_key="QA-E1_stale_r21_stub_20260724")
    plant("_replay", "v5_tools", e_key="QA-E1")

    idx = hi.HistoryIndex()
    idx._refresh_from_disk(tmp_path / "output")

    history = idx.get_history("QA-T1", "US")
    assert [e.e_key for e in history] == ["QA-E1"]
    assert idx.files_indexed == 1


def test_r17_fires_when_prior_was_pass_and_now_n_a():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R17"
    assert findings[0]["severity"] == "high"
    assert "Pass" in findings[0]["description"]


def test_r17_fires_on_blocked_after_pass():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Blocked")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R17"


def test_r17_fires_when_prior_was_fail():
    history = [_entry("2026-05-20T00:00:00Z", "Fail", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert "Fail" in findings[0]["description"]


def test_r17_fires_when_prior_was_passed_with_issue():
    history = [_entry("2026-05-20T00:00:00Z", "Passed With Issue",
                       e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1


def test_r17_severity_medium_when_comment_provided():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR",
                     status="Not Applicable",
                     comment="Test case retired per spec change SPEC-123")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["severity"] == "medium"


def test_r17_severity_medium_when_trace_link_provided():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR",
                     status="Not Applicable",
                     trace_links=[{"key": "QA-BUG-1"}])
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["severity"] == "medium"


def test_r17_silent_when_current_is_pass():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Pass")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_silent_when_no_history():
    buckets = {}
    metadata = _meta(tc_key="QA-NEW", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_silent_when_only_current_in_history():
    history = [_entry("2026-05-27T10:00:00Z", "Not Applicable",
                       e_key="CURRENT")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR",
                     status="Not Applicable",
                     execution_date="2026-05-27T10:00:00Z")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_silent_when_prior_was_also_n_a():
    history = [_entry("2026-05-20T00:00:00Z", "Not Applicable", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_silent_when_prior_was_blocked():
    history = [_entry("2026-05-20T00:00:00Z", "Blocked", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_excludes_current_execution_from_history_search():
    history = [
        _entry("2026-05-27T10:00:00Z", "Not Applicable", e_key="CURRENT"),
        _entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD"),
    ]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR",
                     status="Not Applicable",
                     execution_date="2026-05-27T10:00:00Z")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R17"


def test_r17_per_marketplace_independence():
    buckets = {
        "QA-T1|BR": [_entry("2026-05-20T00:00:00Z", "Pass",
                                  e_key="BR_OLD")],
    }
    idx = _StubIndex(buckets)
    findings_us = wr.check_immediate_prior_executed(
        _meta(tc_key="QA-T1", mp="US", status="Not Applicable"), idx)
    findings_br = wr.check_immediate_prior_executed(
        _meta(tc_key="QA-T1", mp="BR", status="Not Applicable"), idx)
    assert findings_us == []
    assert len(findings_br) == 1


def test_get_shared_reuses_instance_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(hi, "_SHARED", {})
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    calls = []
    real = hi.HistoryIndex.load_or_build.__func__

    def counting(cls, d):
        calls.append(d)
        return real(cls, d)

    monkeypatch.setattr(hi.HistoryIndex, "load_or_build",
                        classmethod(counting))

    a = hi.HistoryIndex.get_shared(out_dir)
    b = hi.HistoryIndex.get_shared(out_dir)
    assert a is b
    assert len(calls) == 1


def test_get_shared_refreshes_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(hi, "_SHARED", {})
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    a = hi.HistoryIndex.get_shared(out_dir)
    b = hi.HistoryIndex.get_shared(out_dir, max_age_s=0.0)
    assert a is not b


def test_get_shared_distinct_out_dirs_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(hi, "_SHARED", {})
    d1 = tmp_path / "out1"
    d2 = tmp_path / "out2"
    d1.mkdir()
    d2.mkdir()
    assert hi.HistoryIndex.get_shared(d1) is not hi.HistoryIndex.get_shared(d2)


def test_history_entry_verdict_round_trip():
    e = hi.HistoryEntry(date="2026-06-01T00:00:00Z", status="Pass",
                        e_key="E-1", folder="F", tester="A",
                        verdict="fail", highs=3)
    d = e.to_dict()
    assert d["verdict"] == "fail" and d["highs"] == 3
    back = hi.HistoryEntry.from_dict(d)
    assert back.verdict == "fail" and back.highs == 3
    legacy = hi.HistoryEntry.from_dict(
        {"date": "x", "status": "Pass", "e_key": "E", "folder": "F"})
    assert legacy.verdict is None and legacy.highs == 0


def test_read_metadata_entry_picks_up_audit_outcome(tmp_path):
    exec_dir = tmp_path / "output" / "Folder" / "Tester" / "E-9"
    exec_dir.mkdir(parents=True)
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"test_case_key": "T-1", "marketplace": "US",
                          "status": "Pass",
                          "execution_date": "2026-06-01T00:00:00Z",
                          "executed_by_name": "A"}]}))
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "fail",
        "findings": [{"severity": "high"}, {"severity": "medium"},
                     {"severity": "high"}]}))
    got = hi.HistoryIndex._read_metadata_entry(exec_dir / "metadata.json")
    assert got is not None
    _, _, entry = got
    assert entry.verdict == "fail"
    assert entry.highs == 2


def test_read_metadata_entry_no_audit_yet(tmp_path):
    exec_dir = tmp_path / "output" / "Folder" / "Tester" / "E-10"
    exec_dir.mkdir(parents=True)
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"test_case_key": "T-1", "status": "Pass"}]}))
    got = hi.HistoryIndex._read_metadata_entry(exec_dir / "metadata.json")
    _, _, entry = got
    assert entry.verdict is None and entry.highs == 0


def test_configure_sets_shared_refresh_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(hi, "_SHARED", {})
    monkeypatch.setattr(hi, "_refresh_interval_s", hi._SHARED_TTL_S)
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    hi.configure(tmp_path / "state", refresh_interval_s=0.0)
    assert hi.HistoryIndex.get_shared(out_dir) is not (
        hi.HistoryIndex.get_shared(out_dir))

    hi.configure(tmp_path / "state", refresh_interval_s=3600.0)
    assert hi._refresh_interval_s == 3600.0
    assert hi.HistoryIndex.get_shared(out_dir) is (
        hi.HistoryIndex.get_shared(out_dir))
    # An explicit max_age_s still overrides the configured interval.
    assert hi.HistoryIndex.get_shared(out_dir) is not (
        hi.HistoryIndex.get_shared(out_dir, max_age_s=0.0))
