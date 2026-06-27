"""Unit tests for history_index.py + R14 streak-break in workflow_rules.py.

Covered:
  - HistoryIndex bucketing by (test_case_key, marketplace)
  - Status alias canonicalisation (N/A vs Not Applicable)
  - is_streak threshold + window arithmetic
  - exclude_e_key behaviour (current execution removed before streak check)
  - R14a: Pass after persistent Blocked-or-N/A
  - R14b: Blocked / N/A after persistent Pass
  - Severity escalation (no-comment + no-trace-links → high)
  - Skip cases (history < 5, current status equals streak status)
  - Per-marketplace independence
"""
from __future__ import annotations

import datetime as _dt

import history_index as hi
import workflow_rules as wr


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
    """Minimal metadata dict matching build_metadata's output shape with
    the fields R14 cares about."""
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


# ---------------------------------------------------------------------------
# is_streak
# ---------------------------------------------------------------------------
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
    # 7/10 Blocked, threshold is 8 → not a streak.
    assert hi.HistoryIndex.is_streak(history, "Blocked") is False


def test_is_streak_returns_false_when_history_too_short():
    history = [_entry("2026-05-20T00:00:00Z", "Blocked", e_key="E1")] * 4
    # Only 4 entries; default threshold is 8, can't possibly meet it.
    assert hi.HistoryIndex.is_streak(history, "Blocked") is False


def test_is_streak_excludes_current_e_key():
    # 8 Blocked + 2 Pass entries. One of the Pass entries is the
    # "current" execution we want excluded. After exclusion: 9 entries
    # in the window — 8 Blocked + 1 OLD_PASS. Streak check on Blocked
    # at window=10 threshold=8 should return True.
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
    # 8 entries split between two short forms — should match either.
    assert hi.HistoryIndex.is_streak(history, "Not Applicable",
                                      window=8, threshold=8) is True
    assert hi.HistoryIndex.is_streak(history, "N/A",
                                      window=8, threshold=8) is True


# ---------------------------------------------------------------------------
# R14a: Pass after persistent Blocked-or-N/A
# ---------------------------------------------------------------------------
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
                     comment="actually verified")  # has a comment
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "R14a"
    assert f["severity"] == "medium"  # comment present → no escalation
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


def test_r14a_does_not_fire_on_continuing_blocked():
    buckets = _blocked_streak_buckets()
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Blocked")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    # Current Blocked extends the streak; no break.
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


# ---------------------------------------------------------------------------
# R14b: Blocked / N/A after persistent Pass
# ---------------------------------------------------------------------------
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
    assert findings[0]["severity"] == "medium"  # comment present


def test_r14b_fires_not_applicable_after_pass_streak():
    buckets = _pass_streak_buckets()
    metadata = _meta(tc_key="QA-T2", mp="DE", status="Not Applicable")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R14b"
    assert findings[0]["severity"] == "high"  # no comment, no trace_links


def test_r14b_does_not_fire_on_fail():
    """R14b cares about Blocked-or-N/A, not Fail. Fail is a real failure
    — escalation logic is in other rules (R2/R7/R11)."""
    buckets = _pass_streak_buckets()
    metadata = _meta(tc_key="QA-T2", mp="DE", status="Fail")
    findings = wr.check_streak_break(metadata, _StubIndex(buckets))
    assert findings == []


# ---------------------------------------------------------------------------
# Skip / safety conditions
# ---------------------------------------------------------------------------
def test_no_fire_when_history_below_minimum():
    """History needs >= 5 entries to even consider a streak."""
    history = [
        _entry(f"2026-05-{20-i:02d}T00:00:00Z", "Blocked", e_key=f"E{i}")
        for i in range(4)  # only 4 entries
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


# ---------------------------------------------------------------------------
# Per-marketplace independence
# ---------------------------------------------------------------------------
def test_marketplace_buckets_are_independent():
    """T1 has a Blocked streak in BR but a Pass streak in DE.
    A Pass in BR fires R14a; a Pass in DE does not."""
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
    assert findings_de == []  # Pass in DE extends DE's Pass streak — no break


# ---------------------------------------------------------------------------
# HistoryIndex._read_metadata_entry
# ---------------------------------------------------------------------------
def test_read_metadata_entry_populates_marketplace_placeholder(tmp_path):
    """A metadata.json with marketplace=None should bucket under '_'
    so it doesn't pollute real per-MP histories."""
    # Build a tiny output/<folder>/<E-key>/metadata.json shape.
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
        "test_results": [{"status": "Pass"}]  # no test_case_key
    }))
    assert hi.HistoryIndex._read_metadata_entry(meta_path) is None


# ---------------------------------------------------------------------------
# R17: immediate-prior-executed → current N/A
# ---------------------------------------------------------------------------
# Companion to R14b: catches the same "tester regressed an executed
# test to Not Applicable" anti-pattern at the FIRST sprint instead
# of waiting for an 8/10 streak.
def test_r17_fires_when_prior_was_pass_and_now_n_a():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R17"
    assert findings[0]["severity"] == "high"  # no comment, no trace_links
    assert "Pass" in findings[0]["description"]


def test_r17_fires_on_blocked_after_pass():
    history = [_entry("2026-05-20T00:00:00Z", "Pass", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Blocked")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R17"


def test_r17_fires_when_prior_was_fail():
    """A Fail in the prior sprint also qualifies — the test was executed,
    it just didn't pass. Marking the same test N/A this sprint is still
    a coverage regression worth surfacing."""
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
    """A test with no PRIOR — only the current entry — has nothing to
    regress against. Don't fire."""
    history = [_entry("2026-05-27T10:00:00Z", "Not Applicable",
                       e_key="CURRENT")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR",
                     status="Not Applicable",
                     execution_date="2026-05-27T10:00:00Z")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_silent_when_prior_was_also_n_a():
    """Two N/A in a row isn't a regression — the test wasn't executed
    in either sprint. R17 only fires on prior=executed → current=N/A."""
    history = [_entry("2026-05-20T00:00:00Z", "Not Applicable", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_silent_when_prior_was_blocked():
    """Same logic as the N/A case: prior also wasn't executed, so the
    current N/A isn't a coverage regression."""
    history = [_entry("2026-05-20T00:00:00Z", "Blocked", e_key="OLD")]
    buckets = {"QA-T1|BR": history}
    metadata = _meta(tc_key="QA-T1", mp="BR", status="Not Applicable")
    findings = wr.check_immediate_prior_executed(metadata, _StubIndex(buckets))
    assert findings == []


def test_r17_excludes_current_execution_from_history_search():
    """If the history index has already picked up this run, R17 must
    skip the current entry and look at the one before it."""
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
    """T1 was Pass on BR, never run on US. Now N/A on US should NOT
    fire R17 — there's no prior US execution to regress against. But
    N/A on BR should fire."""
    buckets = {
        "QA-T1|BR": [_entry("2026-05-20T00:00:00Z", "Pass",
                                  e_key="BR_OLD")],
        # No US history.
    }
    idx = _StubIndex(buckets)
    findings_us = wr.check_immediate_prior_executed(
        _meta(tc_key="QA-T1", mp="US", status="Not Applicable"), idx)
    findings_br = wr.check_immediate_prior_executed(
        _meta(tc_key="QA-T1", mp="BR", status="Not Applicable"), idx)
    assert findings_us == []
    assert len(findings_br) == 1
