"""Web controls embedded only in dashboards with a configured operator API."""
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from argus.operations.test_run_targets import MAX_MANUAL_REFERENCES, MAX_MANUAL_TARGET_CHARS
from argus.reporting.ui import ReportUiContext

_ASSETS = Path(__file__).parent / "assets"
_JS = (_ASSETS / "operator.js").read_text(encoding="utf-8")
_CSS = (_ASSETS / "operator.css").read_text(encoding="utf-8")


def render(ui: ReportUiContext) -> str:
    if not ui.api_url or not ui.dashboard_slug:
        return ""
    origin = urlsplit(ui.api_url)
    if origin.scheme != "https" or not origin.hostname:
        return ""
    from argus.operations.rule_runtime import enabled
    rules_link = (
        f'<a class="argus-team-rules-link" href="{escape(ui.team_base_path, quote=True)}'
        f'/{escape(ui.dashboard_slug, quote=True)}/rules">Team rules</a>'
        if enabled(ui.dashboard_slug) else ""
    )
    return (
        '<section class="cs-container argus-operator" data-argus-operator '
        f'data-api-url="{escape(ui.api_url, quote=True)}" '
        f'data-team="{escape(ui.dashboard_slug, quote=True)}">'
        '<div class="cs-container-header"><h2>Run an audit</h2>'
        f'{rules_link}'
        '<span data-operator-identity></span></div>'
        '<div class="cs-card-body">'
        '<p data-operator-signin>Sign in to start audits and manage this team’s runs. '
        f'<a href="{escape(ui.api_url, quote=True)}/login?team={escape(ui.dashboard_slug, quote=True)}">'
        'Sign in to controls</a></p>'
        '<form data-operator-form hidden>'
        '<div class="argus-operator-label">'
        '<label for="operator-target">Folders or Jira execution, test-case, run, or test-plan keys</label>'
        '<details class="argus-operator-help" data-operator-help>'
        '<summary aria-label="Help with audit targets" aria-controls="operator-target-help">'
        '<span aria-hidden="true">i</span></summary>'
        '<div id="operator-target-help" class="argus-operator-help-content" role="note">'
        '<strong>Audit targets</strong>'
        f'<p>Enter up to {MAX_MANUAL_REFERENCES} targets for this team, separated by commas.'
        ' Jira Test Player links are also accepted.</p>'
        '<p>A test-case key (T) audits its completed executions in this team.'
        ' An execution key (E) audits that one execution.</p>'
        '<p>A test-plan key creates an isolated report named from the plan title,'
        ' containing only its linked runs. Leave blank to use this team’s scheduled'
        ' folder where supported.</p>'
        '<p>Refresh reports republishes existing results; use one report folder'
        ' or leave blank.</p></div></details></div>'
        '<div class="argus-operator-actions">'
        '<input class="argus-input" id="operator-target" name="target" '
        f'maxlength="{MAX_MANUAL_TARGET_CHARS}" autocomplete="off" '
        'placeholder="QA-C123, QA-C456">'
        '<button class="argus-operator-primary" type="submit">Run audit</button>'
        '<button type="button" data-operator-rerender>Refresh reports</button>'
        '</div></form>'
        '<p data-operator-message role="status" aria-live="polite"></p>'
        '<div data-operator-run hidden>'
        '<div class="argus-operator-run-header"><strong data-operator-phase></strong>'
        '<button type="button" data-operator-stop hidden>Stop run</button></div>'
        '<p data-operator-progress></p>'
        '<a href="./" data-operator-reports hidden>View updated reports</a>'
        '<details><summary>Recent activity</summary>'
        '<pre data-operator-logs aria-label="Recent audit activity"></pre></details>'
        '</div></div></section>'
        f'<style>{_CSS}</style><script>{_JS}</script>'
    )
