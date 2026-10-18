from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from argus.operations import rerender
from argus.operations.teams import AuditTeam


@pytest.fixture(autouse=True)
def local_dashboard_refresh(monkeypatch):
    monkeypatch.setattr(
        rerender.report_publisher, "refresh_dashboard", lambda: False,
    )


def _team(slug: str, output_dir: Path) -> AuditTeam:
    return AuditTeam(
        slug=slug,
        profile=slug,
        name=slug.title(),
        allow_auto_folder=False,
        state_dir=str(output_dir.parent / f"{slug}-state"),
        output_dir=str(output_dir),
    )


def test_report_folders_only_returns_existing_reports(tmp_path):
    report = tmp_path / "report-a"
    report.mkdir()
    (report / "_argus.html").write_text("old report")
    unpublished = tmp_path / "report-b"
    unpublished.mkdir()
    (tmp_path / "file").write_text("not a folder")

    assert rerender._report_folders(tmp_path) == [report]


def test_report_folders_can_select_display_folder_name(tmp_path):
    selected = tmp_path / "Targeted_regression_-_03_sep_2026_WEB"
    other = tmp_path / "Targeted_regression_-_2_sep_2026"
    for report in (selected, other):
        report.mkdir()
        (report / "_argus.html").write_text("old report")

    assert rerender._report_folders(
        tmp_path,
        folder_name="Targeted regression - 03 sep 2026(WEB)",
    ) == [selected]


def test_invalid_environment_team_names_the_rejected_slug(monkeypatch):
    monkeypatch.setenv("ARGUS_TEAM_SLUG", "not-a-team")

    with pytest.raises(ValueError, match="unknown ARGUS team: not-a-team"):
        rerender._selected_teams(all_teams=False, team_slug=None)


@pytest.mark.parametrize("extra", [
    [],
    ["--folder", "PORTAL regression", "--all-teams"],
    ["--dashboards-only"],
])
def test_run_refresh_requires_one_existing_folder(extra):
    with pytest.raises(SystemExit) as exc:
        rerender.main(["--run", "PORTAL-C656", *extra])
    assert exc.value.code == 2


def test_run_refresh_uses_report_only_selection(monkeypatch, tmp_path):
    team = _team("portal", tmp_path / "portal")
    folder = Path(team.output_dir) / "PORTAL_regression"
    folder.mkdir(parents=True)
    (folder / "_argus.html").write_text("existing report")
    settings = SimpleNamespace(
        extractor=SimpleNamespace(out_dir=Path(team.output_dir)),
    )
    monkeypatch.setattr(rerender, "resolve_team", lambda value: team)
    monkeypatch.setattr(rerender.argus_config, "load", lambda *a, **kw: settings)
    monkeypatch.setattr(rerender.runtime, "configure", lambda *a: None)
    refresh = mock.MagicMock(return_value=0)
    monkeypatch.setattr(rerender.orchestrator, "_run_folder_list", refresh)
    monkeypatch.setattr(
        rerender.orchestrator, "_publish_folder_reports",
        lambda *a, **kw: pytest.fail("must apply the requested run boundary"),
    )

    assert rerender.main([
        "--team", "portal", "--folder", "PORTAL regression", "--run", "portal-c656",
    ]) == 0
    refresh.assert_called_once_with(
        "PORTAL regression", settings,
        selected_run_keys={"PORTAL-C656"}, reports_only=True,
    )


@pytest.mark.parametrize("dashboards_only", [False, True])
def test_all_teams_rerenders_existing_reports(
    monkeypatch, tmp_path, dashboards_only,
):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_report = first_root / "folder-a"
    second_report = second_root / "folder-b"
    first_report.mkdir(parents=True)
    second_report.mkdir(parents=True)
    (first_report / "_argus.html").write_text("old first")
    (second_report / "_argus.html").write_text("old second")
    teams = (
        _team("first", first_root),
        _team("second", second_root),
        _team("empty", tmp_path / "empty"),
    )
    configured = []
    published = []
    refreshed = []

    def load(_path, *, profile):
        team = next(item for item in teams if item.profile == profile)
        assert rerender.os.environ["ARGUS_TEAM_SLUG"] == team.slug
        assert rerender.os.environ["ARGUS_OUTPUT_DIR"] == team.output_dir
        return SimpleNamespace(
            extractor=SimpleNamespace(out_dir=Path(team.output_dir)),
        )

    monkeypatch.setattr(rerender, "TEAMS", teams)
    monkeypatch.setattr(rerender.argus_config, "load", load)
    monkeypatch.setattr(
        rerender.runtime,
        "configure",
        lambda settings: configured.append(settings),
    )
    monkeypatch.setattr(
        rerender.orchestrator,
        "_publish_folder_reports",
        lambda folder, settings: published.append((folder, settings)) or True,
    )
    monkeypatch.setattr(
        rerender.report_publisher,
        "refresh_dashboard",
        lambda: refreshed.append(rerender.os.environ["ARGUS_TEAM_SLUG"]) or True,
    )
    monkeypatch.delenv("ARGUS_TEAM_SLUG", raising=False)
    monkeypatch.delenv("ARGUS_OUTPUT_DIR", raising=False)

    args = ["--all-teams"]
    if dashboards_only:
        args.append("--dashboards-only")
    assert rerender.main(args) == 0
    assert [folder for folder, _settings in published] == (
        [] if dashboards_only else [first_report, second_report]
    )
    assert refreshed == ["first", "second", "empty"]
    assert len(configured) == 3
    assert "ARGUS_TEAM_SLUG" not in rerender.os.environ
    assert "ARGUS_OUTPUT_DIR" not in rerender.os.environ


@pytest.mark.parametrize("dashboards_only", [False, True])
def test_dashboard_refresh_failure_is_reported(
    monkeypatch, tmp_path, capsys, dashboards_only,
):
    team = _team("web", tmp_path / "web")
    folder = Path(team.output_dir) / "existing-report"
    folder.mkdir(parents=True)
    (folder / "_argus.html").write_text("old report")
    published = []
    monkeypatch.setattr(rerender, "resolve_team", lambda _value: team)
    monkeypatch.setattr(
        rerender.argus_config,
        "load",
        lambda _path, *, profile: SimpleNamespace(
            extractor=SimpleNamespace(out_dir=Path(team.output_dir)),
        ),
    )
    monkeypatch.setattr(rerender.runtime, "configure", lambda _settings: None)

    def fail_refresh():
        raise RuntimeError("Report storage unavailable")

    monkeypatch.setattr(
        rerender.report_publisher, "refresh_dashboard", fail_refresh,
    )
    monkeypatch.setattr(
        rerender.orchestrator,
        "_publish_folder_reports",
        lambda report, _settings: published.append(report) or True,
    )
    args = ["--team", "web"]
    if dashboards_only:
        args.append("--dashboards-only")
    assert rerender.main(args) == 1
    assert published == ([] if dashboards_only else [folder])
    assert "dashboard refresh failed" in capsys.readouterr().err


def test_rerender_returns_failure_when_publication_fails(
    monkeypatch,
    tmp_path,
):
    output_root = tmp_path / "web"
    report = output_root / "folder"
    report.mkdir(parents=True)
    (report / "_argus.html").write_text("old report")
    team = _team("web", output_root)

    monkeypatch.setattr(rerender, "TEAMS", (team,))
    monkeypatch.setattr(
        rerender,
        "resolve_team",
        lambda _value: team,
    )
    monkeypatch.setattr(
        rerender.argus_config,
        "load",
        lambda _path, *, profile: SimpleNamespace(
            extractor=SimpleNamespace(out_dir=output_root),
        ),
    )
    monkeypatch.setattr(rerender.runtime, "configure", lambda _settings: None)
    monkeypatch.setattr(
        rerender.orchestrator,
        "_publish_folder_reports",
        lambda _folder, _settings: False,
    )

    assert rerender.main(["--team", "web"]) == 1


def test_rerender_returns_failure_when_selected_folder_is_missing(
    monkeypatch,
    tmp_path,
):
    team = _team("web", tmp_path / "web")
    team_root = Path(team.output_dir)
    team_root.mkdir()

    monkeypatch.setattr(rerender, "TEAMS", (team,))
    monkeypatch.setattr(rerender, "resolve_team", lambda _value: team)
    monkeypatch.setattr(
        rerender.argus_config,
        "load",
        lambda _path, *, profile: SimpleNamespace(
            extractor=SimpleNamespace(out_dir=team_root),
        ),
    )
    monkeypatch.setattr(rerender.runtime, "configure", lambda _settings: None)

    assert rerender.main([
        "--team",
        "web",
        "--folder",
        "missing report",
    ]) == 1


def test_all_teams_folder_filter_succeeds_when_one_team_owns_report(
    monkeypatch,
    tmp_path,
):
    web_root = tmp_path / "web"
    native_root = tmp_path / "native"
    report = web_root / "Targeted_regression"
    report.mkdir(parents=True)
    native_root.mkdir()
    (report / "_argus.html").write_text("old report")
    teams = (
        _team("web", web_root),
        _team("native", native_root),
    )
    published = []

    def load(_path, *, profile):
        team = next(item for item in teams if item.profile == profile)
        return SimpleNamespace(
            extractor=SimpleNamespace(out_dir=Path(team.output_dir)),
        )

    monkeypatch.setattr(rerender, "TEAMS", teams)
    monkeypatch.setattr(rerender.argus_config, "load", load)
    monkeypatch.setattr(rerender.runtime, "configure", lambda _settings: None)
    monkeypatch.setattr(
        rerender.orchestrator,
        "_publish_folder_reports",
        lambda folder, _settings: published.append(folder) or True,
    )

    assert rerender.main([
        "--all-teams",
        "--folder",
        "Targeted regression",
    ]) == 0
    assert published == [report]
