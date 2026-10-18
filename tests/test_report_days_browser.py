"""Tablet date navigation and exports against a local report in Chromium."""
import csv
import io
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from argus import config
from argus.reporting import report, ui
from tests.test_report_run_groups import audit, coverage, roster_row

playwright = pytest.importorskip("playwright.sync_api")


def test_tablet_day_filters_sorting_pagination_and_csv(tmp_path):
    summaries = [
        audit(
            tmp_path, f"QA-E{i + 1}", "QA-C1" if i < 56 else "QA-C2",
            execution_date=None if i == 55 else (
                "2026-09-21T00:30:00+05:30" if i < 52 else "2026-09-20T23:30:00-07:00"
            ),
        )
        for i in range(60)
    ]
    summaries[0] = replace(summaries[0], analysis_status="incomplete", verdict="fail")
    summaries[53] = replace(summaries[53], verdict="fail", findings_count=1)
    summaries[54] = replace(summaries[54], verdict="concerns", findings_count=1)
    coverage(tmp_path, [
        {**roster_row("QA-E61", "QA-C1"), "execution_date": "2026-09-21"},
        {**roster_row("QA-E62", "QA-C1"), "execution_date": "2026-09-20"},
    ])
    bundle = report.generate_html_report_bundle(
        tmp_path, summaries=summaries, ui_context=ui.from_settings(config.load(profile="tablet")),
    )
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            if urlsplit(route.request.url).path.endswith("/_argus.html"):
                route.fulfill(status=200, content_type="text/html", body=bundle.canonical_html)
            else:
                route.fulfill(status=404)

        page.route("**/*", serve)
        page.goto("https://argus-preview.test/tablet/_argus.html")
        rows = page.locator("tr.argus-row:visible")
        headings = page.locator("tr.argus-day-heading:visible")
        day = page.get_by_role("combobox", name="Filter by execution date", exact=True)

        def expect_summary(counts, shares):
            cards = page.locator(".argus-stat[data-stat-action]")
            playwright.expect(cards.locator(".argus-stat-value")).to_have_text(
                [str(count) for count in counts],
            )
            playwright.expect(
                page.locator('.argus-stat[data-stat-action="verdict"] .metric-description'),
            ).to_have_text(shares)

        expect_summary([60, 57, 1, 1], ["95% of audits", "2% of audits", "2% of audits"])
        playwright.expect(rows).to_have_count(50)
        playwright.expect(headings).to_have_count(1)
        playwright.expect(headings.first).to_contain_text("2026-09-21")

        day.select_option("2026-09-20")
        playwright.expect(rows).to_have_count(8)
        playwright.expect(headings).to_have_count(2)
        expect_summary([7, 5, 1, 1], ["71% of audits", "14% of audits", "14% of audits"])
        page.locator("#argus-run-picker-open").click()
        page.locator('[data-run-choice="QA-C1"]').click()
        playwright.expect(rows).to_have_count(4)
        playwright.expect(headings).to_have_count(1)
        expect_summary([3, 1, 1, 1], ["33% of audits", "33% of audits", "33% of audits"])
        assert "day=2026-09-20" in page.url and "run=QA-C1" in page.url
        page.reload()
        playwright.expect(rows).to_have_count(4)
        playwright.expect(day).to_have_value("2026-09-20")
        expect_summary([3, 1, 1, 1], ["33% of audits", "33% of audits", "33% of audits"])

        def exported(selector):
            with page.expect_download() as event:
                page.locator(selector).click()
            download = event.value
            values = Path(download.path()).read_text(encoding="utf-8-sig")
            return list(csv.DictReader(io.StringIO(values))), download.suggested_filename

        exported_rows, filename = exported("#argus-export-csv")
        assert len(exported_rows) == 4
        assert {row["Test run key"] for row in exported_rows} == {"QA-C1"}
        assert all(row["Execution date"].startswith("2026-09-20") for row in exported_rows)
        assert "2026-09-20" in filename
        assert "Not audited" in {row["Verdict"] for row in exported_rows}
        page.select_option("#argus-verdict", "fail")
        playwright.expect(rows).to_have_count(1)
        expect_summary([1, 0, 0, 1], ["0% of audits", "0% of audits", "100% of audits"])
        assert len(exported("#argus-export-csv")[0]) == 1
        assert len(exported("#argus-export-all-csv")[0]) == 62
        page.locator("#argus-runs-open").click()
        assert len(exported('[data-run-export="QA-C1"]')[0]) == 58
        page.locator("#argus-runs-close").click()

        page.locator("#argus-clear").click()
        expect_summary([60, 57, 1, 1], ["95% of audits", "2% of audits", "2% of audits"])
        day.select_option("2026-09-21")
        playwright.expect(rows).to_have_count(50)
        expect_summary([52, 51, 0, 0], ["98% of audits", "0% of audits", "0% of audits"])
        # Export includes the other page as well as the visible rows.
        assert len(exported("#argus-export-csv")[0]) == 53
        page.get_by_role("button", name="Next page", exact=True).click()
        playwright.expect(rows).to_have_count(3)
        playwright.expect(headings).to_have_count(1)
        expect_summary([52, 51, 0, 0], ["98% of audits", "0% of audits", "0% of audits"])
        page.select_option("#argus-verdict", "incomplete")
        playwright.expect(rows).to_have_count(1)
        expect_summary([1, 0, 0, 0], ["0% of audits", "0% of audits", "0% of audits"])
        page.select_option("#argus-verdict", "unaudited")
        playwright.expect(rows).to_have_count(1)
        expect_summary([0, 0, 0, 0], ["—", "—", "—"])
        page.select_option("#argus-verdict", "")
        day.select_option("_unknown")
        playwright.expect(rows).to_have_count(1)
        expect_summary([1, 1, 0, 0], ["100% of audits", "0% of audits", "0% of audits"])
        playwright.expect(headings).to_contain_text("Execution date not recorded")
        playwright.expect(page.get_by_role("button", name="Next page", exact=True)).to_have_count(0)

        page.locator("#argus-clear").click()
        key_sort = page.locator('th[data-sort-key="key"]')
        key_sort.click()
        key_sort.click()
        page.wait_for_function("""() =>
            document.querySelector('th[data-sort-key="key"]').getAttribute('aria-sort') === 'descending'
        """)
        # Sorting must never move executions into a different run/day section.
        assert page.locator("#argus-tbody").evaluate("""body => {
            let run = null, day = null;
            return Array.from(body.children).every(row => {
                if (row.classList.contains('argus-run-heading')) { run = row.dataset.run; day = null; }
                if (row.classList.contains('argus-day-heading')) day = row.dataset.day;
                return !row.classList.contains('argus-row') ||
                    (row.dataset.run === run && row.dataset.day === day);
            });
        }""")
        page.locator('[data-run-toggle="QA-C1"]').click()
        playwright.expect(headings).to_have_count(0)
        expect_summary([60, 57, 1, 1], ["95% of audits", "2% of audits", "2% of audits"])
        page.locator('[data-run-toggle="QA-C1"]').click()
        playwright.expect(headings).to_have_count(1)

        page.goto("https://argus-preview.test/tablet/_argus.html?day=2026-01-01")
        playwright.expect(rows).to_have_count(0)
        playwright.expect(headings).to_have_count(0)
        playwright.expect(page.locator("#argus-no-matches")).to_be_visible()
        expect_summary([0, 0, 0, 0], ["—", "—", "—"])
        page.locator("#argus-clear").click()
        playwright.expect(rows).to_have_count(50)
        expect_summary([60, 57, 1, 1], ["95% of audits", "2% of audits", "2% of audits"])
        page.set_viewport_size({"width": 390, "height": 844})
        day.select_option("2026-09-20")
        playwright.expect(rows).to_have_count(8)
        expect_summary([7, 5, 1, 1], ["71% of audits", "14% of audits", "14% of audits"])
        assert errors == []
        browser.close()
