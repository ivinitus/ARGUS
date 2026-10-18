"""Browser integration against the real review service, with no external calls."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from argus.operations.review_service import ReviewError, ReviewService
from argus.operations.review_store import SqliteReviewStore
from argus.reporting.review_data import render_page

playwright = pytest.importorskip("playwright.sync_api")


def test_assignment_review_and_mobile_layout(tmp_path):
    from tests.test_human_review import Data, resolve_reviewers

    data = Data()
    for index, item in enumerate(data.items):
        item["run_name"] = "Membership & podcast purchases"
        item["verdict"] = ["pass", "concerns", "fail"][index % 3]
        data.details[item["key"]]["verdict"] = item["verdict"]
    for detail in data.details.values():
        detail["evidence"] = [{"url": "image.png", "type": "image/png", "name": "Step evidence"}]
        detail["findings"] = [
            {"id": "missing-defect", "rule": "R11", "title": "R11", "step_index": 1,
             "description": "Step 2 is marked Fail but has no linked defect (trace_links). Add trace_links."},
            {"id": "unknown-rule", "rule": "R999", "title": "R999",
             "description": "Evidence is unclear. Check the recording."},
        ]
    service = ReviewService(SqliteReviewStore(":memory:"), data, resolve_reviewers)
    html = render_page("desktop", "HS_Reg", "https://api.beta.argus.retail.example.dev")
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            url = urlsplit(request.request.url)
            if url.path.rsplit("/", 1)[-1] in {"review", "review.html"}:
                request.fulfill(status=200, content_type="text/html", body=html)
                return
            headers = {"Access-Control-Allow-Origin": "https://beta.argus.retail.example.dev",
                       "Access-Control-Allow-Credentials": "true",
                       "Access-Control-Allow-Headers": "Content-Type,X-Argus-Request,Idempotency-Key",
                       "Access-Control-Allow-Methods": "GET,POST,OPTIONS"}
            if request.request.method == "OPTIONS":
                request.fulfill(status=204, headers=headers)
                return
            if "/reviews/HS_Reg/" not in url.path:
                request.fulfill(status=404)
                return
            action = url.path.split("/reviews/HS_Reg/")[1]
            try:
                result = service.handle(
                    "desktop", "HS_Reg", action, request.request.method, "user0",
                    request.request.post_data_json or {},
                    {key: values[0] for key, values in parse_qs(url.query).items()},
                    request.request.headers.get("idempotency-key", ""),
                )
                code = 200
            except ReviewError as exc:
                result, code = {"error": str(exc)}, exc.status
            request.fulfill(status=code, content_type="application/json",
                            headers=headers, body=json.dumps(result))

        page.route("**/*", route)
        page.goto("https://beta.argus.retail.example.dev/reports/teams/desktop/HS_Reg/review.html?source=check#case")
        playwright.expect(page).to_have_url("https://beta.argus.retail.example.dev/reports/teams/desktop/HS_Reg/review?source=check#case")
        playwright.expect(page.locator("#run option[value='QA-C1']")).to_have_text(
            "Membership & podcast purchases (QA-C1)",
        )
        with page.expect_response(lambda response: "run=QA-C1" in response.url) as run_filter:
            page.select_option("#run", label="Membership & podcast purchases (QA-C1)")
        assert run_filter.value.json()["filtered_total"] == len(data.items)
        playwright.expect(page.locator("#run")).to_have_value("QA-C1")
        verdict_filter = page.get_by_role("combobox", name="Verdict", exact=True)
        playwright.expect(verdict_filter.locator("option")).to_have_text(
            ["All verdicts", "Compliant", "Concern", "Deviation"],
        )
        for value, label in [("pass", "Compliant"), ("concerns", "Concern"), ("fail", "Deviation")]:
            with page.expect_response(lambda response, value=value: f"verdict={value}" in response.url) as verdict_response:
                verdict_filter.select_option(label=label)
            result = verdict_response.value.json()
            assert result["filtered_total"] == 2
            assert all(item["verdict"] == value for item in result["items"])
            playwright.expect(page.locator(".case-row")).to_have_count(2)
            playwright.expect(page.locator("#queue-count")).to_contain_text("2 matching cases")
        with page.expect_response(lambda response: "verdict=&" in response.url):
            verdict_filter.select_option(label="All verdicts")
        playwright.expect(page.locator(".case-row")).to_have_count(len(data.items))
        playwright.expect(page.locator(".case-row .review-status").first).to_have_text("Unassigned")
        assert page.get_by_text("Check every execution. Keep your decisions alongside the automated audit.", exact=True).count() == 0
        page.get_by_text("Manage reviewers and assignments", exact=True).click()
        page.get_by_role("button", name="Add reviewer", exact=True).click()
        for i in range(3):
            row = page.locator(".roster-row").nth(i)
            assert row.locator("input").count() == 1
            row.get_by_label("Name", exact=True).fill(f"user{i}")
        page.get_by_role("button", name="Preview allocation", exact=True).click()
        playwright.expect(page.locator("#allocation-preview")).to_contain_text("Reviewer 0 (user0)")
        playwright.expect(page.locator("#allocation-preview")).to_contain_text("2 cases are missing execution records and cannot be assigned yet.")
        page.locator(".roster-row").first.get_by_label("Name", exact=True).fill("USER0")
        playwright.expect(page.locator("#allocation-preview")).to_be_hidden()
        page.get_by_role("button", name="Preview allocation", exact=True).click()
        page.get_by_role("button", name="Save these assignments").click()
        playwright.expect(page.locator("#message")).to_contain_text("Assignments saved.")
        playwright.expect(page.locator("#reviewer-progress th")).to_have_text(
            ["Reviewer", "Assigned", "Not started", "In progress", "Needs investigation", "Reviewed"],
        )
        reviewer_progress = page.locator("#reviewer-progress tr").filter(has_text="Reviewer 0").locator("td")
        playwright.expect(reviewer_progress).to_have_text(["Reviewer 0", "2", "2", "0", "0", "0"])
        page.get_by_role("button", name="Dismiss notification").click()
        page.select_option("#reviewer", "user0")
        page.locator(".case-row").first.click()
        playwright.expect(page.locator(".case-row .review-status").first).to_have_text("Not started")
        playwright.expect(page.locator(".finding strong").first).to_have_text("Step with an issue has no defect reference")
        playwright.expect(page.locator(".finding strong").last).to_have_text("Evidence is unclear.")
        assert "trace_links" not in page.locator(".finding").first.inner_text()
        page.get_by_role("button", name="Start review", exact=True).click()
        playwright.expect(page.get_by_role("button", name="Start review", exact=True)).to_have_count(0)
        playwright.expect(reviewer_progress).to_have_text(["Reviewer 0", "2", "1", "1", "0", "0"])
        page.get_by_role("button", name="Dismiss notification").click()
        completed_key = page.locator("#case > p.muted").first.inner_text().split(" · ")[0]
        case_status = page.locator(".case-row").filter(
            has=page.get_by_text(completed_key, exact=True),
        ).locator(".review-status")
        playwright.expect(case_status).to_have_text("In progress")
        # The case button must flag unfinished work without mandatory notes.
        page.get_by_role("button", name="Needs investigation", exact=True).click()
        playwright.expect(page.locator("#message")).to_have_text(f"Marked for investigation: {completed_key}.")
        playwright.expect(page.locator("#case .badge.investigation")).to_have_text("Needs investigation")
        playwright.expect(page.locator("#stats")).to_contain_text("1Needs investigation")
        playwright.expect(case_status).to_have_text("Needs investigation")
        playwright.expect(reviewer_progress).to_have_text(["Reviewer 0", "2", "1", "0", "1", "0"])
        page.get_by_role("button", name="Dismiss notification").click()
        complete = page.get_by_role("button", name="Complete case review", exact=True)
        playwright.expect(complete).to_be_disabled()
        playwright.expect(page.locator("#completion-hint")).to_have_text("Resolve 2 findings before completing this review.")
        assert page.get_by_role("checkbox").count() == 0
        for decision in page.get_by_label("Finding decision", exact=True).all():
            decision.select_option("confirm")
        playwright.expect(complete).to_be_enabled()
        # A save notice must not intercept review actions after selecting Confirm.
        page.get_by_role("button", name="Save progress", exact=True).last.click()
        playwright.expect(page.locator("#message")).to_contain_text("Marked for investigation:")
        for width, height in [(1440, 1000), (1366, 768), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            actions = page.locator("#case > .actions")
            actions.evaluate("node => node.scrollIntoView({block: 'end'})")
            for button in actions.get_by_role("button").all():
                playwright.expect(button).to_be_enabled()
                assert button.evaluate("""node => {
                    const box = node.getBoundingClientRect();
                    const target = document.elementFromPoint(
                        box.x + box.width / 2, box.y + box.height / 2);
                    return target === node || node.contains(target);
                }"""), f"{button.inner_text()} is covered at {width} × {height}"
        page.get_by_role("button", name="Dismiss notification").click()
        playwright.expect(page.locator("#message")).to_be_hidden()
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_label("Finding decision", exact=True).first.select_option("investigate")
        playwright.expect(complete).to_be_disabled()
        page.get_by_label("Finding decision", exact=True).first.select_option("reject")
        playwright.expect(complete).to_be_disabled()
        playwright.expect(page.locator("#completion-hint")).to_have_text("Add a reason for 1 rejected finding before completing this review.")
        page.get_by_label("Decision reason", exact=True).first.fill("The linked defect is present in the evidence.")
        playwright.expect(complete).to_be_enabled()
        playwright.expect(page.locator("#completion-hint")).to_be_hidden()
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and "/items/" in response.url) as completed:
            complete.click()
        assert completed.value.request.post_data_json["attested"] is True
        assert completed.value.json()["state"] == "reviewed"
        playwright.expect(page.locator("#stats")).to_contain_text("1Reviewed")
        playwright.expect(page.locator("#message")).to_have_text(f"Review completed: {completed_key}.")
        playwright.expect(case_status).to_have_text("Reviewed")
        playwright.expect(reviewer_progress).to_have_text(["Reviewer 0", "2", "1", "0", "0", "1"])
        page.get_by_role("button", name="Dismiss notification").click()
        next_key = page.locator("#case > p.muted").first.inner_text().split(" · ")[0]
        assert next_key != completed_key
        page.locator(".case-row").filter(has=page.get_by_text(completed_key, exact=True)).click()
        playwright.expect(page.locator("#case .badge.completed")).to_have_text("Review completed")
        page.locator(".case-row").filter(has=page.get_by_text(next_key, exact=True)).click()
        # Removing the coordinator must not block saving a replacement allocation.
        page.get_by_role("button", name="Start review", exact=True).click()
        playwright.expect(page.get_by_role("button", name="Start review", exact=True)).to_have_count(0)
        page.get_by_role("button", name="Dismiss notification").click()
        # Saving a finding marked for investigation must update the case status.
        page.get_by_label("Finding decision", exact=True).first.select_option("investigate")
        page.get_by_role("button", name="Save progress", exact=True).last.click()
        playwright.expect(page.locator("#message")).to_have_text(f"Marked for investigation: {next_key}.")
        playwright.expect(page.locator("#case .badge.investigation")).to_have_text("Needs investigation")
        playwright.expect(page.get_by_label("Finding decision", exact=True).first).to_have_value("investigate")
        playwright.expect(page.locator("#stats")).to_contain_text("1Needs investigation")
        page.get_by_role("button", name="Dismiss notification").click()
        page.locator(".roster-row").first.get_by_role("button", name="Remove", exact=True).click()
        page.get_by_role("button", name="Preview allocation", exact=True).click()
        playwright.expect(page.locator("#allocation-preview")).to_contain_text("Unfinished cases being reassigned (1)")
        page.get_by_role("button", name="Save these assignments").click()
        playwright.expect(page.locator("#message")).to_have_text("Assignments saved.")
        playwright.expect(page.locator("#reviewer")).to_have_value("")
        assert [row.input_value() for row in page.get_by_label("Name", exact=True).all()] == ["user1", "user2"]
        playwright.expect(reviewer_progress).to_have_text(["Reviewer 0 (completed reviews)", "1", "0", "0", "0", "1"])
        playwright.expect(page.locator("#stats")).to_contain_text("1Reviewed")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        box = page.locator("#message").bounding_box()
        assert box and 800 < box["y"] < box["y"] + box["height"] <= 980
        page.set_viewport_size({"width": 390, "height": 844})
        box = page.locator("#message").bounding_box()
        assert box and 600 < box["y"] < box["y"] + box["height"] <= 824
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.get_by_role("button", name="Dismiss notification").click()
        playwright.expect(page.locator("#message")).to_be_hidden()
        with page.expect_response(lambda response: "queue?reviewer=user1" in response.url) as changed:
            page.select_option("#reviewer", "user1")
        playwright.expect(page.locator(".case-row")).to_have_count(changed.value.json()["filtered_total"])
        assert all("user1" in row.inner_text() for row in page.locator(".case-row small").all())
        page.reload()
        playwright.expect(page.locator("#identity")).to_have_text("Signed in as user0")
        assert not errors
        browser.close()
