"""Independent execution-date and audit-date range filters in Chromium.

Exercises the two custom date-range pickers added to the main report filter
bar (surface A: html_report.py rows + filter bar, driven by argus.js
applyFilters()). Uses the non-Tablet ``web`` profile so the ranges are proven
universal (no Tablet day grouping / #argus-day dropdown involved) and so execution
date and audit date are genuinely distinct dimensions: every row's execution
date and audit date are deliberately misaligned, so a filter on one dimension
that also matched on the other would be caught.

Data model (60 audited rows, run QA-C1, one row per key):
  execution_date -> calendar day E = 2026-09-01 + (i % 3)   (Sep 01 / 02 / 03)
  audited_at     -> UTC day        A = 2026-09-10 + (i % 4)  (Sep 10..13)
so the two dimensions partition the rows into unrelated buckets.
"""
import csv
import io
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from argus import config
from argus.reporting import report, ui
from tests.test_report_run_groups import audit

playwright = pytest.importorskip("playwright.sync_api")

_EXEC_DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]
_AUDIT_DAYS = ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"]
_N = 120


def _exec_day(i: int) -> str:
    return _EXEC_DAYS[i % len(_EXEC_DAYS)]


def _audit_day(i: int) -> str:
    return _AUDIT_DAYS[i % len(_AUDIT_DAYS)]


def _build_summaries(tmp_path):
    summaries = []
    for i in range(_N):
        summaries.append(audit(
            tmp_path, f"QA-E{i + 1:03d}", "QA-C1",
            # Noon local so a UTC audit boundary can't accidentally shift the
            # execution calendar date the way it would near midnight.
            execution_date=f"{_exec_day(i)}T12:00:00+00:00",
            # Audit timestamps are canonical UTC 'Z' strings, late in the day so
            # the audit-date dimension is unambiguously the UTC calendar day.
            audited_at=f"{_audit_day(i)}T23:30:00Z",
        ))
    # A couple of deviations/concerns so the summary-card counts are non-trivial
    # and we can assert summary counts follow the active date filters. Row 0 is
    # exec Sep-01 / audit Sep-10; row 4 is exec Sep-02 / audit Sep-11.
    summaries[0] = replace(summaries[0], verdict="fail", findings_count=2)
    summaries[4] = replace(summaries[4], verdict="concerns", findings_count=1)
    return summaries


def _expected(pred):
    return sum(1 for i in range(_N) if pred(i))


def test_execution_and_audit_date_ranges_are_independent(tmp_path):
    summaries = _build_summaries(tmp_path)
    bundle = report.generate_html_report_bundle(
        tmp_path, summaries=summaries,
        ui_context=ui.from_settings(config.load(profile="web")),
    )
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            if urlsplit(route.request.url).path.endswith("/_argus.html"):
                route.fulfill(status=200, content_type="text/html",
                              body=bundle.canonical_html)
            else:
                route.fulfill(status=404)

        page.route("**/*", serve)
        page.goto("https://argus-preview.test/web/_argus.html")

        rows = page.locator("tr.argus-row:visible")
        count_el = page.locator("#argus-count")

        # The Tablet-only execution-date dropdown must NOT be present here, but the
        # two universal range pickers must be.
        assert page.locator("#argus-day").count() == 0
        for sel in ("#argus-exec-from", "#argus-exec-to",
                    "#argus-audit-from", "#argus-audit-to"):
            assert page.locator(sel).count() == 1

        def total_matches():
            # "<lo>–<hi> of <N>" or "<lo>–<hi> of <N> (…)" — parse the of-count.
            text = count_el.inner_text()
            after_of = text.split(" of ", 1)[1]
            return int(after_of.split(" ")[0])

        # Baseline: all 60 rows, first page shows 50.
        playwright.expect(rows).to_have_count(50)
        assert total_matches() == _N

        page.locator("#argus-daterange summary").click()
        exec_from = page.get_by_label("Execution date from", exact=True)
        exec_to = page.get_by_label("Execution date to", exact=True)
        audit_from = page.get_by_label("Audit date from", exact=True)
        audit_to = page.get_by_label("Audit date to", exact=True)

        # --- Execution-date range ALONE (Sep-01 only) -----------------------
        exec_from.fill("2026-09-01")
        exec_to.fill("2026-09-01")
        exec_only = _expected(lambda i: _exec_day(i) == "2026-09-01")
        assert total_matches() == exec_only
        assert "execFrom=2026-09-01" in page.url and "execTo=2026-09-01" in page.url
        # Audit dates within the exec=Sep-01 set still span several audit days,
        # proving the exec filter did not also constrain the audit dimension.
        audit_days_seen = rows.evaluate_all(
            "els => Array.from(new Set(els.map(e => e.dataset.audited))).sort()")
        assert len(audit_days_seen) > 1

        # --- Add an audit-date range (Sep-10 only) on top -------------------
        audit_from.fill("2026-09-10")
        audit_to.fill("2026-09-10")
        both = _expected(lambda i: _exec_day(i) == "2026-09-01"
                         and _audit_day(i) == "2026-09-10")
        assert total_matches() == both
        assert 0 < both < exec_only  # intersection is strictly smaller

        # --- Audit-date range ALONE (clear exec, keep audit) ----------------
        exec_from.fill("")
        exec_to.fill("")
        audit_only = _expected(lambda i: _audit_day(i) == "2026-09-10")
        assert total_matches() == audit_only
        assert "execFrom" not in page.url and "audFrom=2026-09-10" in page.url
        exec_days_seen = rows.evaluate_all(
            "els => Array.from(new Set(els.map(e => e.dataset.day))).sort()")
        assert len(exec_days_seen) > 1  # audit filter didn't touch exec dim

        # --- Inclusive boundaries: Sep-10..Sep-11 includes both endpoints ---
        audit_to.fill("2026-09-11")
        incl = _expected(lambda i: _audit_day(i) in ("2026-09-10", "2026-09-11"))
        assert total_matches() == incl
        assert incl > audit_only

        # --- Combine with verdict + run + tester ----------------------------
        # verdict=fail is only row 0 (exec Sep-01, audit Sep-10) — inside range.
        page.select_option("#argus-verdict", "fail")
        assert total_matches() == 1
        page.select_option("#argus-verdict", "")

        # The native #argus-run select is hidden in favour of the custom picker;
        # drive the run filter through the picker exactly as an operator would.
        page.locator("#argus-run-picker-open").click()
        page.locator('[data-run-choice="QA-C1"]').click()   # the only run
        assert total_matches() == incl
        page.select_option("#argus-tester", "Tester")    # the only tester
        assert total_matches() == incl
        page.locator("#argus-run-picker-open").click()
        page.locator('[data-run-choice=""]').click()      # back to all runs
        page.select_option("#argus-tester", "")

        # --- Empty result: an audit range with no rows ----------------------
        audit_from.fill("2026-01-01")
        audit_to.fill("2026-01-02")
        assert total_matches() == 0
        playwright.expect(page.locator("#argus-no-matches")).to_be_visible()

        # --- Clear restores totals ------------------------------------------
        # Collapse the floating range panel first (it overlays the bar while
        # open) — the realistic operator flow before reaching for Clear.
        page.locator("#argus-daterange summary").click()
        page.locator("#argus-clear").click()
        playwright.expect(rows).to_have_count(50)
        assert total_matches() == _N
        for sel in ("#argus-exec-from", "#argus-exec-to",
                    "#argus-audit-from", "#argus-audit-to"):
            playwright.expect(page.locator(sel)).to_have_value("")

        assert errors == []
        browser.close()


def test_both_ranges_restore_from_url_with_pagination_and_csv(tmp_path):
    summaries = _build_summaries(tmp_path)
    bundle = report.generate_html_report_bundle(
        tmp_path, summaries=summaries,
        ui_context=ui.from_settings(config.load(profile="web")),
    )
    exec_lo, exec_hi = "2026-09-01", "2026-09-02"
    aud_lo, aud_hi = "2026-09-10", "2026-09-13"
    expected = _expected(
        lambda i: exec_lo <= _exec_day(i) <= exec_hi
        and aud_lo <= _audit_day(i) <= aud_hi)

    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            if urlsplit(route.request.url).path.endswith("/_argus.html"):
                route.fulfill(status=200, content_type="text/html",
                              body=bundle.canonical_html)
            else:
                route.fulfill(status=404)

        page.route("**/*", serve)
        query = (f"?execFrom={exec_lo}&execTo={exec_hi}"
                 f"&audFrom={aud_lo}&audTo={aud_hi}")
        page.goto("https://argus-preview.test/web/_argus.html" + query)

        rows = page.locator("tr.argus-row:visible")
        count_el = page.locator("#argus-count")

        # Restored ranges populate the inputs and expand the panel so they're
        # visible to the reviewer.
        playwright.expect(page.locator("#argus-daterange")).to_have_attribute(
            "open", "")
        playwright.expect(
            page.get_by_label("Execution date from", exact=True)).to_have_value(exec_lo)
        playwright.expect(
            page.get_by_label("Execution date to", exact=True)).to_have_value(exec_hi)
        playwright.expect(
            page.get_by_label("Audit date from", exact=True)).to_have_value(aud_lo)
        playwright.expect(
            page.get_by_label("Audit date to", exact=True)).to_have_value(aud_hi)

        def total_matches():
            text = count_el.inner_text()
            return int(text.split(" of ", 1)[1].split(" ")[0])

        assert expected > 50  # ensure pagination is genuinely exercised
        assert total_matches() == expected
        # First page shows exactly 50 of the matched rows.
        playwright.expect(rows).to_have_count(50)

        # Page 2 shows the remainder and keeps the same match total + ranges.
        page.get_by_role("button", name="Next page", exact=True).click()
        playwright.expect(rows).to_have_count(expected - 50)
        assert total_matches() == expected

        # CSV export covers the whole matched set across both pages and every
        # exported row falls inside both ranges (independent dimensions AND-ed).
        with page.expect_download() as event:
            page.locator("#argus-export-csv").click()
        download = event.value
        values = Path(download.path()).read_text(encoding="utf-8-sig")
        exported = list(csv.DictReader(io.StringIO(values)))
        assert len(exported) == expected
        for row in exported:
            assert exec_lo <= row["Execution date"][:10] <= exec_hi

        assert errors == []
        browser.close()
