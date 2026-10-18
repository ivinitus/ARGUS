"""Browser checks against the real publication service and isolated sample data."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from argus.operations.rule_demo import DemoRuleReports, finish_sample_preview
from argus.operations.rule_builder_demo import DemoRuleService
from argus.operations.rule_service import RuleError
from argus.operations.rule_store import SqliteRuleStore
from argus.reporting.rules_page import render_page
from argus.reporting.static_assets import resolve

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def editor(request):
    settings_only = getattr(request, "param", False)
    store = SqliteRuleStore(":memory:")
    service = DemoRuleService(store, lambda team, actor: True,
                          lambda team, identifier, actor: finish_sample_preview(store, team, identifier),
                          DemoRuleReports(), settings_only=settings_only)
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        faults = {}

        def route(request):
            url = urlsplit(request.request.url)
            path = url.path
            if path == "/rules":
                request.fulfill(status=200, content_type="text/html", body=render_page("desktop", "", demo=True, settings_only=settings_only))
                return
            asset = resolve(path)
            if asset:
                request.fulfill(status=200, content_type=asset.content_type, body=asset.path.read_bytes())
                return
            if path == "/session":
                request.fulfill(status=200, content_type="application/json", body=json.dumps({"alias": "local-user"}))
                return
            operation = path.partition("/teams/desktop/rules")[2].lstrip("/")
            if faults.get(operation):
                request.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "Service unavailable. Retry."}))
                return
            try:
                result = service.handle(
                    "desktop", operation, request.request.method, "local-user",
                    request.request.post_data_json or {}, request.request.headers.get("idempotency-key", ""),
                    {key: values[0] for key, values in parse_qs(url.query).items()},
                )
                code = 200
            except RuleError as exc:
                result, code = {"error": str(exc)}, exc.status
            request.fulfill(status=code, content_type="application/json", body=json.dumps(result))

        page.route("**/*", route)
        page.goto("http://127.0.0.1:2410/rules")
        playwright.expect(page.locator("#active-version")).to_have_text("Published version 1")
        yield page, service, faults
        assert not errors
        browser.close()


def select_sample_tests(page):
    page.get_by_role("tab", name="Test changes", exact=True).click()
    page.get_by_label("Report", exact=True).select_option("sample-tests")
    playwright.expect(page.locator("#test-rows tr")).to_have_count(3)
    for checkbox in page.locator("#test-rows input").all():
        checkbox.check()


def create_example(page):
    page.get_by_role("button", name="Create rule", exact=True).click()
    page.get_by_label("Evaluation method", exact=True).select_option("evidence")
    page.get_by_role("button", name="Use purchase-email example").click()
    page.get_by_label("Impact on results", exact=True).select_option("enforce")
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")


def assert_readable(page, selector, *, placeholder=False):
    """Measure rendered text against its painted background, including alpha."""
    contrast = page.locator(selector).first.evaluate("""(element, placeholder) => {
      const rgba = value => value.match(/[\\d.]+/g).map(Number);
      const blend = (front, back) => front.slice(0, 3).map(
        (channel, i) => channel * (front[3] ?? 1) + back[i] * (1 - (front[3] ?? 1)));
      const luminance = rgb => rgb.map(channel => {
        const c = channel / 255;
        return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
      }).reduce((total, c, i) => total + c * [.2126, .7152, .0722][i], 0);
      const parents = [];
      for (let node = element; node; node = node.parentElement) parents.unshift(node);
      const background = parents.reduce(
        (paint, node) => blend(rgba(getComputedStyle(node).backgroundColor), paint), [255, 255, 255]);
      const style = getComputedStyle(element, placeholder ? '::placeholder' : null);
      const text = rgba(style.color);
      text[3] = (text[3] ?? 1) * Number(style.opacity);
      const a = luminance(blend(text, background)), b = luminance(background);
      return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    }""", placeholder)
    assert contrast >= 4.5, f"{selector} text contrast is only {contrast:.2f}:1"


@pytest.mark.parametrize("system_theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 390])
def test_theme_contrast_and_form_content_do_not_overlap(editor, system_theme, width):
    page, _, _ = editor
    page.emulate_media(color_scheme=system_theme)
    page.set_viewport_size({"width": width, "height": 900})
    for selected_theme in ["light", "dark"]:
        page.evaluate("(theme) => localStorage.setItem('cs.theme', theme)", selected_theme)
        page.reload()
        playwright.expect(page.locator("#active-version")).to_have_text("Published version 1")
        for selector in ["#published-detail", ".rules-rule-name small", ".rules-severity select",
                         ".rules-badge", "#demo-banner", "#tab-button-policies"]:
            assert_readable(page, selector)
        assert_readable(page, "#rule-search", placeholder=True)
        # A system preference must not mix a dark banner into the selected light UI.
        banner = page.locator("#demo-banner").evaluate(
            "(el) => getComputedStyle(el).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number)"
        )
        brightness = sum(channel * weight for channel, weight in zip(banner, (0.2126, 0.7152, 0.0722), strict=True))
        assert brightness > 220 if selected_theme == "light" else brightness < 65
        page.get_by_role("tab", name="Test changes", exact=True).click()
        assert_readable(page, ".rules-intro")
        assert_readable(page, "#publish-note", placeholder=True)
        page.get_by_role("tab", name="Rules", exact=True).click()
        page.get_by_role("button", name="Create rule", exact=True).click()
        page.get_by_label("Evaluation method", exact=True).select_option("evidence")
        assert_readable(page, "#custom-applies")
        page.get_by_text("Limit to specific cases or runs", exact=True).click()
        assert_readable(page, "#custom-cases", placeholder=True)
        for offset in [0, 500, 900]:
            page.evaluate("(offset) => scrollTo(0, offset)", offset)
            overlapping = page.evaluate("""() => {
              const footer = document.querySelector('#rule-form .rules-form-actions').getBoundingClientRect();
              return [...document.querySelectorAll('#rule-form .cs-container')]
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  return r.top < footer.bottom && r.bottom > footer.top;
                }).map(el => el.querySelector('h2').textContent);
            }""")
            assert not overlapping, f"Action bar covers form sections: {overlapping}"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_edit_test_publish_compare_restore_and_mobile(editor, tmp_path):
    page, _, _ = editor
    playwright.expect(page.get_by_label("Enable R0", exact=True)).to_be_disabled()
    playwright.expect(page.locator(".cs-topnav .cs-mark")).to_be_visible()
    assert page.locator("#argus-fonts").count() == 1
    assert page.locator("#rules-body td").first.bounding_box()["width"] >= 350
    create_example(page)
    page.get_by_role("button", name="View changes", exact=False).click()
    playwright.expect(page.get_by_role("dialog")).to_contain_text("Purchase confirmation email")
    page.get_by_role("button", name="Close", exact=True).click()
    select_sample_tests(page)
    page.get_by_role("button", name="Test changes", exact=True).click()
    playwright.expect(page.locator("#preview-status")).to_have_text("Tests complete")
    playwright.expect(page.locator(".rules-result")).to_have_count(3)
    playwright.expect(page.locator(".rules-result").first).to_contain_text("Concerns")
    page.locator(".rules-result summary").first.click()
    playwright.expect(page.locator(".rules-result").first).to_contain_text("Added")
    page.screenshot(path=str(tmp_path / "test-changes-desktop.png"), full_page=True)
    page.get_by_label("Change summary", exact=True).fill("Require confirmation emails for purchases")
    page.get_by_role("button", name="Publish rules", exact=True).click()
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 2")
    page.get_by_role("tab", name="History", exact=True).click()
    playwright.expect(page.locator("#history tr")).to_have_count(2)
    page.get_by_role("button", name="Version 1", exact=True).click()
    playwright.expect(page.locator("#version-diff")).to_contain_text("Custom rule removed")
    page.get_by_role("button", name="Restore as draft", exact=True).click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 2")
    page.get_by_role("button", name="Test changes", exact=True).click()
    playwright.expect(page.locator("#preview-status")).to_have_text("Tests complete")
    page.get_by_label("Change summary", exact=True).fill("Restore initial defaults")
    page.get_by_role("button", name="Publish rules", exact=True).click()
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 3")
    page.get_by_role("tab", name="Rules", exact=True).click()
    page.screenshot(path=str(tmp_path / "rules-desktop.png"), full_page=True)
    page.get_by_label("Switch visual mode", exact=True).click()
    playwright.expect(page.locator("html")).to_have_attribute("data-cs-theme", "dark")
    page.screenshot(path=str(tmp_path / "rules-dark.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_label("Toggle navigation", exact=True).click()
    playwright.expect(page.locator(".side-navigation")).to_be_visible()
    page.keyboard.press("Escape")
    playwright.expect(page.locator(".side-navigation")).not_to_be_visible()
    page.screenshot(path=str(tmp_path / "rules-mobile.png"), full_page=True)
    text = page.locator("body").inner_text().lower()
    for excluded in ["lead", "your team, your rules", "publish once", "upcoming audits", "team default"]:
        assert excluded not in text


def test_failed_save_retains_form_and_cancel_retains_other_edits(editor):
    page, _, faults = editor
    page.get_by_label("Severity for R1", exact=True).select_option("medium")
    page.get_by_role("button", name="Create rule", exact=True).click()
    page.get_by_label("Evaluation method", exact=True).select_option("evidence")
    page.get_by_role("button", name="Use purchase-email example").click()
    faults["draft"] = True
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#notice")).to_contain_text("Service unavailable")
    playwright.expect(page.get_by_label("Rule name", exact=True)).to_have_value("Purchase confirmation email")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Cancel", exact=True).click()
    playwright.expect(page.get_by_label("Severity for R1", exact=True)).to_have_value("medium")
    playwright.expect(page.locator("#draft-status")).to_have_text("Unsaved changes")
    faults.clear()
    page.locator("#save-draft").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")


def test_test_account_disabled_persists_and_checkbox_stays_in_sync(editor):
    page, service, _ = editor
    page.get_by_role("tab", name="Policies", exact=True).click()
    mode = page.get_by_label("Test-account check mode", exact=True)
    mode.select_option("disabled")
    page.locator("#save-draft").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.reload()
    playwright.expect(mode).to_have_value("disabled")
    policy = service.state("desktop", "local-user")["draft"]["document"]["policy"]
    assert policy["r22_mode"] == "disabled"
    page.get_by_role("tab", name="Rules", exact=True).click()
    toggle = page.get_by_label("Enable R22", exact=True)
    playwright.expect(toggle).not_to_be_checked()
    playwright.expect(page.locator('tr[data-rule-id="R22"]')).to_contain_text("Not evaluated")
    toggle.check()
    page.get_by_role("tab", name="Policies", exact=True).click()
    playwright.expect(mode).to_have_value("advisory")
    mode.select_option("enforced")
    page.locator("#save-draft").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.reload()
    playwright.expect(mode).to_have_value("enforced")
    page.get_by_role("tab", name="Rules", exact=True).click()
    playwright.expect(toggle).to_be_checked()
    toggle.uncheck()
    page.get_by_role("tab", name="Policies", exact=True).click()
    playwright.expect(mode).to_have_value("disabled")
    mode.select_option("advisory")
    page.get_by_role("tab", name="Rules", exact=True).click()
    playwright.expect(toggle).to_be_checked()


@pytest.mark.parametrize("width,theme", [(1440, "light"), (390, "dark")])
def test_compact_policies_save_percentage_switches_and_map_errors(editor, width, theme):
    page, service, _ = editor
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme="dark")
    page.evaluate("(theme) => localStorage.setItem('cs.theme', theme)", theme)
    page.reload()
    page.get_by_role("tab", name="Policies", exact=True).click()
    rate = page.get_by_label("Required completion rate", exact=True)
    playwright.expect(rate).to_have_value("100")
    assert rate.bounding_box()["width"] <= 100
    rate.fill("90")
    toggle = page.get_by_role("switch", name="Allow empty placeholder steps without a status", exact=True)
    toggle.focus()
    page.keyboard.press("Space")
    playwright.expect(toggle).to_be_checked()
    assert_readable(page, "#policy-completion_threshold-help")
    assert_readable(page, "#policy-min_assigned_for_completion-help")
    page.get_by_role("button", name="View changes", exact=False).click()
    row = page.get_by_role("dialog").locator(".rules-diff-row").filter(has_text="Required completion rate")
    playwright.expect(row).to_contain_text("100%")
    playwright.expect(row).to_contain_text("90%")
    page.get_by_role("button", name="Close", exact=True).click()
    page.locator("#save-draft").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    policy = service.state("desktop", "local-user")["draft"]["document"]["policy"]
    assert policy["completion_threshold"] == 0.9
    assert policy["empty_step_status_optional"] is True
    assert policy["min_assigned_for_completion"] == 5
    page.reload()
    playwright.expect(rate).to_have_value("90")
    playwright.expect(toggle).to_be_checked()
    section = page.locator('[data-policy-section="environment"]')
    section.locator("summary").click()
    mapping = section.locator(".rules-setting-map").filter(has=page.get_by_role(
        "heading", name="Allowed hosts by test case", exact=True,
    ))
    mapping.get_by_role("button", name="Add entry", exact=True).click()
    value = mapping.get_by_label("Allowed hosts by test case value", exact=True)
    value.fill("qa.example.test")
    playwright.expect(mapping.locator(".rules-field-error")).to_have_text("Enter a name or key.")
    rate.fill("80")
    page.locator("#save-draft").click()
    playwright.expect(mapping.locator('[aria-invalid="true"]')).to_be_focused()
    key = mapping.get_by_label("Allowed hosts by test case key", exact=True)
    key.fill("QA-T123")
    playwright.expect(key).to_have_attribute("aria-invalid", "false")
    # Validation stays underneath the key rather than displacing the value/remove control.
    assert key.bounding_box()["x"] < mapping.get_by_role("button", name="Remove Allowed hosts by test case entry").bounding_box()["x"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#save-draft").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    assert section.evaluate("(el) => el.open")
    policy = service.state("desktop", "local-user")["draft"]["document"]["policy"]
    assert policy["completion_threshold"] == 0.8
    assert policy["environment_allowed_host_exceptions"]["QA-T123"] == ["qa.example.test"]


def test_inherited_conditional_severity_filters_and_keyboard(editor):
    page, _, _ = editor
    page.get_by_label("Find a rule", exact=True).fill("R9-AMB")
    row = page.locator('tr[data-rule-id="R9-AMB"]')
    playwright.expect(row).to_contain_text("High · From R9")
    playwright.expect(page.get_by_label("Severity for R9-AMB", exact=True)).to_have_value("")
    page.get_by_role("button", name="Clear filters", exact=True).click()
    page.get_by_label("Find a rule", exact=True).fill("R14")
    playwright.expect(page.locator('tr[data-rule-id="R14"]')).to_contain_text("Conditional · Rule default")
    page.get_by_role("tab", name="Rules", exact=True).focus()
    page.keyboard.press("ArrowRight")
    playwright.expect(page.get_by_role("tab", name="Policies", exact=True)).to_have_attribute("aria-selected", "true")


def test_sparse_allowlist_and_mixed_family_match_runtime(editor):
    from argus.config.models import PolicySettings
    from argus.domain import workflow_rules
    page, _, _ = editor
    # Read catalog and policy from the same API used by the page.
    policy, rules = page.evaluate("""async () => {
      const state = await (await fetch('/teams/desktop/rules')).json();
      const p = state.active.document.policy;
      p.enabled_rules = ['R0', 'R-RUNTIME', 'R2B', 'R9-AMB'];
      p.disabled_rules = [];
      const r = state.catalog.find(rule => rule.id === 'R9-DOC');
      window.ArgusRules.toggle(p, state.catalog, r, true);
      return [p, state.catalog];
    }""")
    workflow_rules.configure(PolicySettings(**policy))
    try:
        findings = [{"rule": rule["id"], "severity": "medium"} for rule in rules]
        actual = {finding["rule"] for finding in workflow_rules.filter_enabled(findings)}
        assert {"R0", "R-RUNTIME", "R2B", "R9-AMB", "R9-DOC"} == actual
    finally:
        workflow_rules.reset_configuration()


def test_existing_report_shell_navigation_and_theme(tmp_path):
    from argus.reporting.server import _render_index
    html = _render_index(None, rows=[], banner_html="")
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def route(request):
            asset = resolve(urlsplit(request.request.url).path)
            request.fulfill(status=200, content_type=asset.content_type if asset else "text/html",
                            body=asset.path.read_bytes() if asset else html)

        page.route("**/*", route)
        page.goto("http://localhost:2410/")
        playwright.expect(page.locator(".side-navigation")).to_be_visible()
        page.get_by_label("Collapse navigation", exact=True).click()
        playwright.expect(page.locator(".side-navigation")).not_to_be_visible()
        page.get_by_label("Toggle navigation", exact=True).click()
        playwright.expect(page.locator(".side-navigation")).to_be_visible()
        page.get_by_label("Switch visual mode", exact=True).click()
        playwright.expect(page.locator("html")).to_have_attribute("data-cs-theme", "dark")
        page.set_viewport_size({"width": 1000, "height": 900})
        playwright.expect(page.locator(".side-navigation")).not_to_be_visible()
        page.get_by_label("Toggle navigation", exact=True).click()
        playwright.expect(page.locator(".side-navigation")).to_be_visible()
        browser.close()


def test_read_only_controls_and_theme_persistence(editor):
    page, service, _ = editor
    service.can_edit = lambda team, actor: False
    page.reload()
    playwright.expect(page.locator("#read-only")).to_be_visible()
    playwright.expect(page.get_by_label("Severity for R1", exact=True)).to_be_disabled()
    playwright.expect(page.get_by_role("button", name="Create rule", exact=True)).to_be_disabled()
    playwright.expect(page.get_by_role("tab", name="Test changes", exact=True)).to_have_count(0)
    page.get_by_label("Switch visual mode", exact=True).click()
    page.reload()
    playwright.expect(page.locator("html")).to_have_attribute("data-cs-theme", "dark")


@pytest.mark.parametrize("failure", ["signed_out", "network"])
def test_hosted_signin_is_available_when_initial_connection_fails(failure):
    html = render_page("desktop", "https://operator.example.test")
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()

        def route(request):
            if urlsplit(request.request.url).path == "/rules":
                request.fulfill(status=200, content_type="text/html", body=html)
            elif failure == "network":
                request.abort()
            else:
                request.fulfill(status=401, content_type="application/json",
                                headers={"Access-Control-Allow-Origin": "http://127.0.0.1:2410",
                                         "Access-Control-Allow-Credentials": "true"},
                                body=json.dumps({"error": "Sign in to use ARGUS controls."}))

        page.route("**/*", route)
        page.goto("http://127.0.0.1:2410/rules")
        playwright.expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
        playwright.expect(page.get_by_role("button", name="Reconnect", exact=True)).to_be_visible()
        playwright.expect(page.locator("#save-draft")).to_be_disabled()
        browser.close()


@pytest.mark.parametrize("editor", [False, True], indirect=True)
def test_python_builder_try_save_publish_and_restore(editor, tmp_path):
    page, service, _ = editor
    if service.settings_only:
        page.get_by_role("button", name="Create rule", exact=True).click()
        playwright.expect(page.locator("#rule-method-section")).to_be_visible()
        playwright.expect(page.locator("#custom-requirement")).to_be_disabled()
    else:
        page.goto("http://127.0.0.1:2410/rules#builder")
    playwright.expect(page.get_by_label("Evaluation method", exact=True)).to_have_value("structured")
    page.get_by_role("button", name="Use defect example", exact=True).click()
    playwright.expect(page.locator("#builder-summary")).to_contain_text("Execution status is Failed")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_have_text("4 tests complete · Python · 0 model calls")
    rows = page.locator(".rules-builder-result")
    playwright.expect(rows).to_have_count(4)
    playwright.expect(rows.nth(0).locator(".rules-verdict")).to_have_text("Not met")
    playwright.expect(rows.nth(1).locator(".rules-verdict")).to_have_text("Met")
    playwright.expect(rows.nth(2).locator(".rules-verdict")).to_have_text("Not applicable")
    rows.nth(1).locator("summary").click()
    playwright.expect(rows.nth(1)).to_contain_text("DEMO-42")
    assert service.state("desktop", "local-user")["draft"] is None
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(path=str(tmp_path / "python-builder-tested.png"), full_page=True)
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.get_by_label("Rule type", exact=True).select_option("structured")
    playwright.expect(page.locator("#rules-body tr")).to_have_count(1)
    playwright.expect(page.locator("#rules-body")).to_contain_text("Python check")
    page.get_by_role("button", name="Failed tests need a defect", exact=True).click()
    playwright.expect(page.get_by_label("Evaluation method", exact=True)).to_be_disabled()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("tab", name="Test changes", exact=True).click()
    page.get_by_label("Report", exact=True).select_option("rule-builder")
    playwright.expect(page.locator("#test-rows tr")).to_have_count(4)
    for checkbox in page.locator("#test-rows input").all():
        checkbox.check()
    page.get_by_role("button", name="Test changes", exact=True).click()
    playwright.expect(page.locator("#preview-status")).to_have_text("Tests complete")
    playwright.expect(page.locator(".rules-result").first).to_contain_text("Concerns")
    page.get_by_label("Change summary", exact=True).fill("Require defects for failed executions")
    page.get_by_role("button", name="Publish rules", exact=True).click()
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 2")
    page.get_by_role("tab", name="History", exact=True).click()
    page.get_by_role("button", name="Version 1", exact=True).click()
    playwright.expect(page.locator("#version-diff")).to_contain_text("Python rule removed")
    page.get_by_role("button", name="Restore as draft", exact=True).click()
    assert service.state("desktop", "local-user")["draft"]["document"]["custom_rules"] == []


@pytest.mark.parametrize("editor", [False, True], indirect=True)
@pytest.mark.parametrize("width,theme", [(1440, "light"), (390, "dark")])
def test_python_builder_conditions_validation_and_theme(editor, tmp_path, width, theme):
    page, service, _ = editor
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme="dark")
    page.evaluate("(theme) => localStorage.setItem('cs.theme', theme)", theme)
    page.reload()
    page.wait_for_function("(theme) => (document.documentElement.dataset.csTheme || 'light') === theme", arg=theme)
    page.goto("http://127.0.0.1:2410/rules#builder")
    page.get_by_role("button", name="Use defect example", exact=True).click()
    page.get_by_role("button", name="Add condition", exact=True).click()
    page.get_by_label("Field for condition 2", exact=True).select_option("marketplace")
    page.get_by_label("Value for condition 2", exact=True).select_option("DE")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_contain_text("4 tests complete")
    playwright.expect(page.locator(".rules-builder-result .neutral")).to_have_count(4)
    page.get_by_label("Match", exact=True).select_option("any")
    playwright.expect(page.locator("#builder-results")).to_be_empty()
    page.get_by_label("Requirement", exact=True).select_option("attachment_count")
    minimum = page.get_by_label("Minimum attachments", exact=True)
    minimum.fill("0")
    page.locator("#save-custom").click()
    playwright.expect(minimum).to_be_focused()
    assert service.state("desktop", "local-user")["draft"] is None
    minimum.fill("2")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_contain_text("4 tests complete")
    playwright.expect(page.locator(".rules-builder-result").nth(1).locator(".rules-verdict")).to_have_text("Met")
    for selector in ["#builder-summary", "#rule-method-help", "#builder-check-help",
                     ".rules-builder-result small", ".rules-builder-result .neutral"]:
        if page.locator(selector).first.is_visible():
            assert_readable(page, selector)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(path=str(tmp_path / f"python-builder-{width}-{theme}.png"), full_page=True)
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.reload()
    page.get_by_role("button", name="Failed tests need a defect", exact=True).click()
    playwright.expect(minimum).to_have_value("2")
    playwright.expect(page.get_by_label("Match", exact=True)).to_have_value("any")
    playwright.expect(page.get_by_label("Value for condition 2", exact=True)).to_have_value("DE")


def test_python_builder_preserves_form_on_failure_and_method_switch(editor):
    page, service, faults = editor
    page.get_by_role("button", name="Create rule", exact=True).click()
    page.get_by_role("button", name="Use defect example", exact=True).click()
    page.get_by_label("Search in", exact=True).select_option("steps")
    page.get_by_label("Evaluation method", exact=True).select_option("evidence")
    page.get_by_label("Evaluation method", exact=True).select_option("structured")
    playwright.expect(page.get_by_label("Search in", exact=True)).to_have_value("steps")
    faults["builder/test"] = True
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_have_text("Sample test failed. Try again.")
    playwright.expect(page.get_by_label("Rule name", exact=True)).to_have_value("Failed tests need a defect")
    faults.clear()
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_contain_text("4 tests complete")
    faults["draft"] = True
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#notice")).to_contain_text("Service unavailable")
    playwright.expect(page.get_by_label("Search in", exact=True)).to_have_value("steps")
    assert service.state("desktop", "local-user")["draft"] is None


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_settings_demo_save_test_publish_and_restore(editor):
    page, service, _ = editor
    playwright.expect(page.get_by_role("button", name="Create rule", exact=True)).to_be_visible()
    playwright.expect(page.locator("#tab-rules")).to_be_visible()
    page.get_by_role("button", name="Test account", exact=True).click()
    playwright.expect(page.locator("#rule-settings-panel")).to_be_visible()
    page.get_by_label("Account check mode", exact=True).select_option("enforced")
    page.get_by_label("Finding severity", exact=True).select_option("")
    page.locator("#save-rule-settings").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.get_by_role("button", name="Test draft", exact=True).click()
    page.get_by_label("Report", exact=True).select_option("rule-builder")
    playwright.expect(page.locator("#test-rows tr")).to_have_count(4)
    for checkbox in page.locator("#test-rows input").all():
        checkbox.check()
    page.get_by_role("button", name="Test changes", exact=True).click()
    playwright.expect(page.locator("#preview-status")).to_have_text("Tests complete")
    results = page.locator(".rules-result")
    playwright.expect(results.first).to_contain_text("Concerns")
    results.first.locator("summary").click()
    playwright.expect(results.first).to_contain_text("test-account")
    playwright.expect(results.nth(1)).not_to_contain_text("Concerns")
    page.get_by_label("Change summary", exact=True).fill("Enforce test-account recording")
    page.get_by_role("button", name="Publish rules", exact=True).click()
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 2")
    page.get_by_role("tab", name="History", exact=True).click()
    page.get_by_role("button", name="Version 1", exact=True).click()
    playwright.expect(page.locator("#version-panel")).to_be_visible()
    page.get_by_role("button", name="Restore as draft", exact=True).click()
    assert service.state("desktop", "local-user")["draft"]["base_revision"] == 2
    page.get_by_role("button", name="Test changes", exact=True).click()
    playwright.expect(page.locator("#preview-status")).to_have_text("Tests complete")
    page.get_by_label("Change summary", exact=True).fill("Restore original account settings")
    page.get_by_role("button", name="Publish rules", exact=True).click()
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 3")


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_rule_settings_links_preserve_edits_and_focus_policy(editor):
    page, service, _ = editor
    page.get_by_label("Severity for R1", exact=True).select_option("high")
    page.get_by_role("button", name="Test account", exact=True).click()
    page.get_by_label("Account check mode", exact=True).select_option("enforced")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Cancel", exact=True).click()
    playwright.expect(page.get_by_label("Severity for R1", exact=True)).to_have_value("high")
    assert service.state("desktop", "local-user")["draft"] is None
    page.get_by_label("Find a rule", exact=True).fill("Tester completion")
    page.get_by_role("button", name="Tester completion", exact=True).click()
    page.get_by_role("button", name="Minimum assigned cases", exact=True).click()
    minimum = page.get_by_label("Minimum assigned cases", exact=True)
    playwright.expect(minimum).to_be_focused()
    minimum.fill("7")
    page.locator("#save-draft").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    policy = service.state("desktop", "local-user")["draft"]["document"]["policy"]
    assert policy["min_assigned_for_completion"] == 7
    assert policy["rule_severities"]["R1"] == "high"
    page.get_by_text("Advanced evidence policy", exact=True).click()
    playwright.expect(page.locator("#evidence-policy")).to_have_attribute("readonly", "")


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_rule_settings_protection_mixed_family_and_save_failure(editor):
    page, service, faults = editor
    page.get_by_role("button", name="Rule engine health", exact=True).click()
    playwright.expect(page.get_by_label("Check state", exact=True)).to_be_disabled()
    playwright.expect(page.get_by_label("Finding severity", exact=True)).to_be_disabled()
    playwright.expect(page.locator("#save-rule-settings")).to_be_hidden()
    page.get_by_role("button", name="Back to rules", exact=True).click()
    page.get_by_role("button", name="Environment", exact=True).click()
    page.get_by_label("Finding severity", exact=True).select_option("medium")
    faults["draft"] = True
    page.locator("#save-rule-settings").click()
    playwright.expect(page.locator("#notice")).to_contain_text("Service unavailable")
    playwright.expect(page.get_by_label("Finding severity", exact=True)).to_have_value("medium")
    faults.clear()
    page.locator("#save-rule-settings").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    assert service.state("desktop", "local-user")["draft"]["document"]["policy"]["rule_severities"]["R9"] == "medium"
    page.get_by_label("Find a rule", exact=True).fill("R9-AMB")
    playwright.expect(page.locator('tr[data-rule-id="R9-AMB"]')).to_contain_text("Medium · From R9")
    page.get_by_role("button", name="Clear filters", exact=True).click()
    service.can_edit = lambda *_: False
    page.reload()
    page.get_by_role("button", name="Test account", exact=True).click()
    playwright.expect(page.get_by_label("Account check mode", exact=True)).to_be_disabled()
    playwright.expect(page.locator("#save-rule-settings")).to_be_hidden()


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_existing_free_text_definition_can_be_edited_in_guided_demo(editor):
    import uuid
    page, service, _ = editor
    state = service.state("desktop", "local-user")
    definition = page.evaluate("window.ArgusRules.example()")
    document = state["active"]["document"]
    document["custom_rules"].append(definition)
    saved = service.handle("desktop", "draft", "POST", "local-user",
                           {"version": state["version"], "document": document}, uuid.uuid4().hex)
    original = saved["draft"]["document"]["custom_rules"][0]
    page.reload()
    page.get_by_role("button", name="Purchase confirmation email", exact=True).click()
    playwright.expect(page.locator("#custom-requirement")).to_be_editable()
    page.locator("#custom-requirement").fill("A purchase confirmation must show the order identifier.")
    page.get_by_label("Severity", exact=True).select_option("high")
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    current = service.state("desktop", "local-user")["draft"]["document"]["custom_rules"][0]
    assert current == {**original, "severity": "high", "requirement": "A purchase confirmation must show the order identifier."}


@pytest.mark.parametrize("editor", [True], indirect=True)
@pytest.mark.parametrize("width,theme", [(1440, "light"), (390, "dark")])
def test_settings_demo_theme_and_policy_mapping(editor, tmp_path, width, theme):
    page, service, _ = editor
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme="dark")
    page.evaluate("(theme) => localStorage.setItem('cs.theme', theme)", theme)
    page.reload()
    page.get_by_role("button", name="Test account", exact=True).click()
    for selector in ["#settings-rule-description", "#settings-account-help",
                     "#settings-severity-help", ".rules-detail-intro", ".rules-setting-link .rules-muted"]:
        assert_readable(page, selector)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(path=str(tmp_path / f"rule-settings-{width}-{theme}.png"), full_page=True)
    catalog = service.state("desktop", "local-user")["catalog"]
    policy = service.state("desktop", "local-user")["active"]["document"]["policy"]
    mapping = page.evaluate("(rules) => Object.fromEntries(rules.map(rule => [rule.id, window.ArgusRules.relatedSettings(rule)]))", catalog)
    assert all(key in policy for keys in mapping.values() for key in keys)
    expected = set(policy) - {"enabled_rules", "disabled_rules", "rule_severities"}
    assert expected == {key for keys in mapping.values() for key in keys}


def review_text_example(page, kind="Attachment"):
    page.get_by_role("button", name="Create rule", exact=True).click()
    page.get_by_role("button", name=kind + " example", exact=True).click()
    page.get_by_role("button", name="Review description", exact=True).click()
    playwright.expect(page.locator("#authoring-recommendation")).to_be_visible()


@pytest.mark.parametrize("editor", [True], indirect=True)
@pytest.mark.parametrize("width,theme", [(1440, "light"), (390, "dark")])
def test_text_interpretation_requires_review_and_persists_as_editable_checks(editor, tmp_path, width, theme):
    page, service, _ = editor
    page.set_viewport_size({"width": width, "height": 950})
    page.evaluate("(theme) => localStorage.setItem('cs.theme', theme)", theme)
    page.reload()
    review_text_example(page)
    playwright.expect(page.locator("#authoring-recommendation")).to_contain_text("Recommended: deterministic checks")
    playwright.expect(page.locator("#custom-name")).to_have_value("")
    playwright.expect(page.locator("#save-custom")).to_be_disabled()
    playwright.expect(page.locator("#view-changes")).to_be_disabled()
    assert service.state("desktop", "local-user")["draft"] is None
    page.get_by_role("button", name="Use this interpretation", exact=True).click()
    playwright.expect(page.get_by_label("Minimum attachments", exact=True)).to_have_value("2")
    playwright.expect(page.get_by_label("Value for condition 2", exact=True)).to_have_value("purchase")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_have_text("4 tests complete · Python · 0 model calls")
    playwright.expect(page.locator("#builder-impact")).to_contain_text("have not been benchmarked")
    for selector in ["#authoring-help", "#rule-method-help", "#builder-impact", "#rule-matches .rules-help"]:
        assert_readable(page, selector)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(path=str(tmp_path / f"text-rule-{width}-{theme}.png"), full_page=True)
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    name = "Failed purchase tests need at least two attachments"
    page.reload()
    page.get_by_role("button", name=name, exact=True).click()
    playwright.expect(page.get_by_label("Rule description", exact=True)).to_have_value(name + ".")
    page.get_by_label("Minimum attachments", exact=True).fill("3")
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    assert service.state("desktop", "local-user")["draft"]["document"]["custom_rules"][0]["check"]["minimum"] == 3


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_duplicate_text_opens_existing_rule_and_does_not_create_another(editor):
    page, service, _ = editor
    review_text_example(page)
    page.get_by_role("button", name="Use this interpretation", exact=True).click()
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    review_text_example(page)
    root = page.locator("#authoring-recommendation")
    playwright.expect(root).to_contain_text("Exact duplicate")
    playwright.expect(root.get_by_role("button", name="Use this interpretation", exact=True)).to_have_count(0)
    page.once("dialog", lambda dialog: dialog.accept())
    root.get_by_role("button", name="Failed purchase tests need at least two attachments", exact=True).click()
    playwright.expect(page.locator("#page-title")).to_have_text("Edit rule")
    assert len(service.state("desktop", "local-user")["draft"]["document"]["custom_rules"]) == 1


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_text_evidence_rule_simulates_tests_publishes_and_remains_editable(editor):
    page, service, _ = editor
    review_text_example(page, "Screenshot")
    playwright.expect(page.locator("#authoring-recommendation")).to_contain_text("Recommended: AI evidence assessment")
    page.get_by_role("button", name="Use this interpretation", exact=True).click()
    playwright.expect(page.get_by_label("Evaluation method", exact=True)).to_have_value("evidence")
    playwright.expect(page.get_by_label("Impact on results", exact=True)).to_have_value("advisory")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_contain_text("4 simulated outcomes")
    playwright.expect(page.locator(".rules-builder-result .rules-verdict").nth(1)).to_have_text("Met")
    playwright.expect(page.locator(".rules-builder-result .rules-verdict").nth(3)).to_have_text("Needs review")
    playwright.expect(page.locator("#builder-impact")).to_contain_text("have not been measured")
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.get_by_role("tab", name="Test changes", exact=True).click()
    page.get_by_label("Report", exact=True).select_option("rule-builder")
    playwright.expect(page.locator("#test-rows tr")).to_have_count(4)
    for checkbox in page.locator("#test-rows input").all():
        checkbox.check()
    page.get_by_role("button", name="Test changes", exact=True).click()
    playwright.expect(page.locator("#preview-status")).to_have_text("Tests complete")
    page.get_by_label("Change summary", exact=True).fill("Try purchase confirmation evidence")
    page.get_by_role("button", name="Publish rules", exact=True).click()
    playwright.expect(page.locator("#active-version")).to_have_text("Published version 2")
    page.get_by_role("tab", name="Rules", exact=True).click()
    page.get_by_role("button", name="Purchase confirmation evidence", exact=True).click()
    page.get_by_label("What should be checked?", exact=True).fill("The confirmation must not show an order identifier.")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_contain_text("4 simulated outcomes")
    assert page.locator(".rules-builder-result .rules-verdict").all_text_contents() == ["Needs review"] * 4
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    assert "must not" in service.state("desktop", "local-user")["draft"]["document"]["custom_rules"][0]["requirement"]


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_unrecognised_text_requires_explicit_method_choice_and_preserves_failed_requests(editor):
    page, service, faults = editor
    page.get_by_role("button", name="Create rule", exact=True).click()
    description = "Failed tests need two attachments unless they are purchase tests."
    page.get_by_label("Rule description", exact=True).fill(description)
    faults["builder/interpret"] = True
    page.get_by_role("button", name="Review description", exact=True).click()
    playwright.expect(page.locator("#notice")).to_contain_text("Service unavailable")
    playwright.expect(page.get_by_label("Rule description", exact=True)).to_have_value(description)
    faults.clear()
    page.get_by_role("button", name="Review description", exact=True).click()
    playwright.expect(page.locator("#authoring-recommendation")).to_contain_text("Clarification needed")
    playwright.expect(page.get_by_label("Evaluation method", exact=True)).to_have_value("structured")
    playwright.expect(page.locator("#save-custom")).to_be_disabled()
    assert service.state("desktop", "local-user")["draft"] is None
    page.get_by_role("button", name="Use AI evidence assessment", exact=True).click()
    playwright.expect(page.get_by_label("What should be checked?", exact=True)).to_have_value(description)
    playwright.expect(page.get_by_label("When does this apply?", exact=True)).to_have_value("")
    page.get_by_label("When does this apply?", exact=True).fill("Failed tests excluding purchase tests")
    page.get_by_label("Expected evidence", exact=True).fill("Two attachments")
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    assert service.state("desktop", "local-user")["draft"]["document"]["custom_rules"][0]["source_text"] == description


@pytest.mark.parametrize("editor", [True], indirect=True)
def test_generic_field_comparisons_support_all_executions_and_persist_values(editor):
    page, service, _ = editor
    page.get_by_role("button", name="Create rule", exact=True).click()
    page.get_by_label("Rule name", exact=True).fill("Allowed marketplaces")
    page.get_by_role("button", name="Remove condition 1", exact=True).click()
    playwright.expect(page.locator("#builder-conditions")).to_contain_text("Applies to all executions")
    page.get_by_label("Requirement", exact=True).select_option("field_value")
    page.get_by_label("Recorded field", exact=True).select_option("marketplace")
    page.get_by_label("Comparison", exact=True).select_option("in")
    page.get_by_label("Allowed values", exact=True).fill("US, UK")
    page.get_by_role("button", name="Try sample tests", exact=True).click()
    playwright.expect(page.locator("#builder-test-status")).to_contain_text("4 tests complete")
    assert page.locator(".rules-builder-result .rules-verdict").all_text_contents() == ["Met", "Met", "Met", "Not met"]
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.reload()
    page.get_by_role("button", name="Allowed marketplaces", exact=True).click()
    playwright.expect(page.get_by_label("Allowed values", exact=True)).to_have_value("US, UK")
    page.get_by_label("Recorded field", exact=True).select_option("attachment_count")
    page.get_by_label("Comparison", exact=True).select_option("gte")
    page.get_by_label("Comparison value", exact=True).fill("2")
    page.locator("#save-custom").click()
    playwright.expect(page.locator("#draft-status")).to_have_text("Draft saved")
    page.get_by_role("button", name="Allowed marketplaces", exact=True).click()
    playwright.expect(page.get_by_label("Comparison value", exact=True)).to_have_value("2")
    assert service.state("desktop", "local-user")["draft"]["document"]["custom_rules"][0]["conditions"] == []
