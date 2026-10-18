"""Self-contained team rule editor for private reports and the localhost demo."""
from html import escape
import json
from pathlib import Path

from argus.operations.teams import TEAMS, resolve_team
from argus.reporting.ui import ReportUiContext, TeamLink

_ASSETS = Path(__file__).parent / "assets"


def render_page(team: str, api_url: str, *, demo: bool = False,
                ui_context: ReportUiContext | None = None,
                settings_only: bool | None = None,
                report_root: str | None = None) -> str:
    from argus.reporting import html_report
    selected = resolve_team(team)
    if selected is None:
        raise ValueError("Unknown team")
    ui_context = ui_context or ReportUiContext(
        dashboard_slug=selected.slug, dashboard_name=selected.name,
        teams=tuple(TeamLink(t.slug, t.name) for t in TEAMS),
        team_base_path="/reports/teams", asset_base_path="/_assets" if demo else "/reports/_assets",
        api_url=api_url, docs_url="" if demo else ReportUiContext().docs_url,
    )
    team_url = report_root or f"{ui_context.team_base_path}/{selected.slug}/"
    builder = None
    if demo:
        from argus.operations.rule_builder_demo import catalog
        builder = catalog()
    config = json.dumps({
        "team": selected.slug, "api": api_url.rstrip("/"), "demo": demo,
        "settingsOnly": demo if settings_only is None else settings_only,
        "builder": builder,
        "teams": [{"slug": t.slug, "name": t.name} for t in TEAMS],
    }).replace("<", "\\u003c")
    template = (_ASSETS / "rules.html").read_text()
    return (template.replace("{{TEAM_NAME}}", escape(selected.name))
            .replace("{{TEAM_URL}}", escape(team_url, quote=True))
            .replace("{{HEAD}}", html_report._cs_head_extra(ui_context))
            .replace("{{SHARED_CSS}}", html_report._cs_page_css(ui_context))
            .replace("{{TOPNAV}}", html_report._cs_topnav("/" if report_root else "/reports/", ui_context))
            .replace("{{SIDENAV}}", html_report._cs_sidenav(
                team_url, "rules", False, ui_context, rules_enabled=True))
            .replace("{{CONFIG}}", config)
            .replace("{{CSS}}", (_ASSETS / "rules.css").read_text())
            .replace("{{MODEL_JS}}", (_ASSETS / "rules_model.js").read_text())
            .replace("{{JS}}", (_ASSETS / "rules.js").read_text()))
