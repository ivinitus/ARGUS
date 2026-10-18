from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

from argus.reporting import server as argus_serve
from argus.reporting import static_assets


class _HandlerProbe:
    local_api = None

    def _valid_local_host(self):
        return True

    def _local_page(self, path, *, head_only):
        return False

    def __init__(
        self,
        path: str,
        request_headers: dict[str, str] | None = None,
    ):
        self.path = path
        self.wfile = io.BytesIO()
        self.status = None
        self.headers = request_headers or {}
        self.response_headers: list[tuple[str, str]] = []
        self.ended = False
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status: int):
        self.status = status

    def send_header(self, key: str, value: str):
        self.response_headers.append((key, value))

    def end_headers(self):
        self.ended = True

    def date_time_string(self, timestamp: int) -> str:
        del timestamp
        return "Sat, 05 Sep 2026 00:00:00 GMT"

    def _handle_request(self, *, head_only: bool) -> None:
        argus_serve._ArgusHandler._handle_request(
            self,
            head_only=head_only,
        )


def _response_header(probe: _HandlerProbe, name: str) -> str | None:
    return dict(probe.response_headers).get(name)


def test_healthz_returns_json():
    probe = _HandlerProbe("/healthz")

    argus_serve._ArgusHandler.do_GET(probe)

    assert probe.status == 200
    assert (
        "Content-Type",
        "application/json; charset=utf-8",
    ) in probe.response_headers
    assert probe.ended is True
    assert json.loads(probe.wfile.getvalue().decode("utf-8")) == {
        "ok": True,
        "service": "argus",
    }


def test_index_route_still_renders(tmp_path):
    probe = _HandlerProbe("/")
    probe.out_dir_path = tmp_path

    argus_serve._ArgusHandler.do_GET(probe)

    assert probe.status == 200
    assert (
        "Content-Type",
        "text/html; charset=utf-8",
    ) in probe.response_headers
    assert b"ARGUS" in probe.wfile.getvalue()


def test_index_supports_gzip_etag_304_and_head(tmp_path):
    gzip_probe = _HandlerProbe("/", {"Accept-Encoding": "gzip"})
    gzip_probe.out_dir_path = tmp_path

    argus_serve._ArgusHandler.do_GET(gzip_probe)

    assert gzip_probe.status == 200
    assert _response_header(gzip_probe, "Content-Encoding") == "gzip"
    assert _response_header(gzip_probe, "Cache-Control") == "no-cache"
    assert gzip.decompress(gzip_probe.wfile.getvalue()).startswith(
        b"<!DOCTYPE html>"
    )
    gzip_etag = _response_header(gzip_probe, "ETag")
    assert gzip_etag is not None and gzip_etag.endswith('-gzip"')

    revisit = _HandlerProbe(
        "/",
        {
            "Accept-Encoding": "gzip",
            "If-None-Match": gzip_etag,
        },
    )
    revisit.out_dir_path = tmp_path

    argus_serve._ArgusHandler.do_GET(revisit)

    assert revisit.status == 304
    assert revisit.wfile.getvalue() == b""
    assert _response_header(revisit, "ETag") == gzip_etag

    head_probe = _HandlerProbe("/", {"Accept-Encoding": "gzip"})
    head_probe.out_dir_path = tmp_path

    argus_serve._ArgusHandler.do_HEAD(head_probe)

    assert head_probe.status == 200
    assert head_probe.wfile.getvalue() == b""
    assert _response_header(head_probe, "Content-Encoding") == "gzip"
    assert int(_response_header(head_probe, "Content-Length") or 0) > 0


def test_static_file_validators_are_representation_specific(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"value": "x" * 4096}))

    identity = _HandlerProbe("/report.json")
    argus_serve._ArgusHandler._send_file(
        identity,
        str(report),
        identity.headers,
    )
    compressed = _HandlerProbe(
        "/report.json",
        {"Accept-Encoding": "br, gzip"},
    )
    argus_serve._ArgusHandler._send_file(
        compressed,
        str(report),
        compressed.headers,
    )

    identity_etag = _response_header(identity, "ETag")
    compressed_etag = _response_header(compressed, "ETag")
    assert identity_etag is not None
    assert compressed_etag is not None
    assert identity_etag != compressed_etag
    assert identity_etag.endswith('-identity"')
    assert compressed_etag.endswith('-gzip"')
    assert gzip.decompress(compressed.wfile.getvalue()) == report.read_bytes()

    revisit = _HandlerProbe(
        "/report.json",
        {
            "Accept-Encoding": "gzip",
            "If-None-Match": compressed_etag,
        },
    )
    argus_serve._ArgusHandler._send_file(
        revisit,
        str(report),
        revisit.headers,
    )
    assert revisit.status == 304
    assert revisit.wfile.getvalue() == b""


def test_gzip_negotiation_respects_quality_values():
    assert argus_serve._accepts_gzip({"Accept-Encoding": "br, gzip"})
    assert argus_serve._accepts_gzip({"Accept-Encoding": "*;q=0.5"})
    assert not argus_serve._accepts_gzip(
        {"Accept-Encoding": "gzip;q=0, *;q=1"}
    )


def test_static_image_is_not_compressed_and_has_one_day_cache(tmp_path: Path):
    image = tmp_path / "evidence.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 4096)
    probe = _HandlerProbe(
        "/evidence.png",
        {"Accept-Encoding": "gzip"},
    )

    argus_serve._ArgusHandler._send_file(
        probe,
        str(image),
        probe.headers,
    )

    assert probe.status == 200
    assert _response_header(probe, "Content-Encoding") is None
    assert (
        _response_header(probe, "Cache-Control")
        == "public, max-age=86400"
    )
    assert probe.wfile.getvalue() == image.read_bytes()


def test_index_has_no_live_pulse_indicator(tmp_path):
    html = argus_serve._render_index(
        tmp_path,
        rows=[],
        banner_html="",
    )

    assert "argus-logo::before" not in html
    assert "argus-watch" not in html


def test_index_relative_times_update_in_browser():
    html = argus_serve._render_index(
        None,
        rows=[{
            "name": "Targeted_regression_-_21_Aug_2026_WEB",
            "audits": 1,
            "mtime": 1_700_000_000.125,
            "rel_url": "folder/_argus.html",
        }],
        banner_html="",
    )

    assert html.count('data-timestamp-ms="1700000000125"') == 2
    assert html.count("data-argus-relative-time") >= 3
    assert "window.setInterval(updateRelativeTimes, 30000)" in html
    assert "visibilitychange" in html
    assert "data-argus-row-label=" in html


def test_index_report_search_has_stable_metadata_and_local_prefetch(monkeypatch):
    # Report CLI tests can configure the process-wide dashboard sections.
    monkeypatch.setattr(
        argus_serve, "_FOLDER_SECTIONS", list(argus_serve._DEFAULT_FOLDER_SECTIONS)
    )
    monkeypatch.setattr(
        argus_serve, "_SECTION_ORDER", list(argus_serve._DEFAULT_SECTION_ORDER)
    )
    html = argus_serve._render_index(
        None,
        rows=[
            {
                "name": "Targeted_regression_WEB",
                "title": "Web targeted regression",
                "audits": 10,
                "mtime": 1_700_000_000.125,
                "rel_url": "Targeted_regression_WEB/_argus.html",
                "prefetch": True,
            },
            {
                "name": "Sanity_testing_WEB",
                "audits": 5,
                "mtime": 1_600_000_000.0,
                "rel_url": "/reports/web/Sanity_testing_WEB/",
            },
        ],
        banner_html="",
    )

    assert 'type="search" id="argus-report-search"' in html
    assert (
        'data-argus-report-search="Web targeted regression '
        'Targeted_regression_WEB"'
    ) in html
    assert 'data-argus-prefetch="true"' in html
    assert html.count(
        '<section class="cs-container argus-report-section" '
        'data-argus-report-section>'
    ) == 2
    assert 'id="argus-report-no-results"' in html
    assert "url.searchParams.set('q', query)" in html
    assert "row.dataset.argusPrefetch === 'true'" in html
    assert "argus-report-search-count" in html


def test_index_uses_branded_actions_and_pinned_navigation():
    html = argus_serve._render_index(
        None,
        rows=[{
            "name": "Folder",
            "audits": 1,
            "mtime": 1_700_000_000.0,
            "rel_url": "Folder/",
        }],
        banner_html="",
    )

    assert ".argus-folder-row a.cs-btn" in html
    assert "--argus-action:var(--ui-color-background-button-primary-default)" in html
    assert 'aria-label="Toggle navigation"' in html
    assert (argus_serve._ASSETS_DIR / "shell.js").read_text() in html
    assert '>Open</a>' in html


def test_relative_phrase_never_says_now_ago():
    assert argus_serve._relative_phrase(0) == "now"
    assert argus_serve._relative_phrase(59) == "now"
    assert argus_serve._relative_phrase(60) == "1m ago"
    assert argus_serve._relative_phrase(3600) == "1h ago"


def test_log_request_suppresses_success(capsys):
    probe = _HandlerProbe("/ok")

    argus_serve._ArgusHandler.log_request(probe, 200, "-")

    assert capsys.readouterr().err == ""


def test_log_request_keeps_error_visibility(capsys):
    probe = _HandlerProbe("/missing")

    argus_serve._ArgusHandler.log_request(probe, 404, "-")

    err = capsys.readouterr().err
    assert "status=404" in err
    assert "path='/missing'" in err
    assert "client=127.0.0.1" in err


def _probe_with_send_error(path: str) -> _HandlerProbe:
    probe = _HandlerProbe(path)
    probe.errors = []
    probe.send_error = lambda code, msg=None: probe.errors.append(code)
    return probe


def test_private_data_stores_are_not_served():
    for path in ("/_index.sqlite", "/_fp_corpus.jsonl",
                 "/_index.sqlite-wal", "/%5Findex.sqlite",
                 "/_index.sqlite?x=1"):
        probe = _probe_with_send_error(path)
        argus_serve._ArgusHandler.do_GET(probe)
        assert probe.errors == [404], path


def test_folder_row_href_escapes_hostile_names(tmp_path):
    row = {"name": 'evil"><script>x</script>',
           "audits": 1, "mtime": 0.0,
           "rel_url": 'evil"><script>x</script>/_argus.html'}
    html = argus_serve._render_folder_row(row, is_active=False)
    assert '"><script>' not in html
    assert "<script>" not in html


def test_folder_row_progress_uses_coverage_not_relative_audit_volume():
    row = {
        "name": "Targeted_regression_-_21_Aug_2026_WEB",
        "audits": 168,
        "mtime": 0.0,
        "rel_url": "folder/_argus.html",
        "coverage": {"items_executed": 161, "items_total": 235},
    }

    html = argus_serve._render_folder_row(row, is_active=False)

    assert "161/235 completed" in html
    assert ">69%</span>" in html
    assert 'aria-valuenow="69"' in html
    assert 'style="width:69%"' in html
    assert '<td class="argus-num">168</td>' in html


def test_versioned_static_asset_requires_exact_content_hash(tmp_path, monkeypatch):
    path = tmp_path / "example.css"
    path.write_text("body { color: black; }")
    asset = static_assets.StaticAsset("example.css", path, static_assets._version(path), "text/css")
    monkeypatch.setattr(static_assets, "_ASSETS", {asset.name: asset})
    good_path = f"/_assets/{asset.version}/{asset.name}"
    bad_path = f"/_assets/000000000000/{asset.name}"

    assert static_assets.resolve(good_path) == asset
    assert static_assets.resolve(bad_path) is None


def test_system_fonts_do_not_require_bundled_font_downloads():
    assert static_assets.FONT_FACES == ()
    assert static_assets.font_face_css() == ""
    assert static_assets.preload_links() == ""


def test_folder_row_progress_clamps_invalid_coverage():
    row = {
        "name": "folder",
        "audits": 1,
        "mtime": 0.0,
        "rel_url": "folder/_argus.html",
        "coverage": {"items_executed": 12, "items_total": 10},
    }

    html = argus_serve._render_folder_row(row, is_active=False)

    assert "10/10 completed" in html
    assert 'aria-valuenow="100"' in html
    assert 'style="width:100%"' in html


def test_in_progress_banner_shows_phase_and_requester(monkeypatch):
    monkeypatch.setattr(argus_serve.argus_control, "current_audit", lambda: {
        "folder": "Sprint <47>",
        "phase": "preparing",
        "requested_by": "alice<script>",
        "n_keys": 0,
        "age_seconds": 75,
        "stale": False,
    })

    html = argus_serve._render_in_progress_banner()

    assert "Preparing audit" in html
    assert "Sprint &lt;47&gt;" in html
    assert "requested by <code>alice&lt;script&gt;</code>" in html
    assert "keys" not in html
    assert "started 1m ago" in html

    monkeypatch.setattr(argus_serve.argus_control, "current_audit", lambda: {
        "folder": "Sprint 47",
        "n_keys": 12,
        "age_seconds": 5,
        "stale": False,
    })
    legacy = argus_serve._render_in_progress_banner()
    assert "Auditing now" in legacy
    assert "12 keys" in legacy
    assert "requested by" not in legacy


def test_index_live_status_banner_is_opt_in_and_never_meta_refreshes():
    html = argus_serve._render_index(
        None,
        rows=[],
        banner_html="",
        live_status_url="./index.json?<x>",
    )

    assert 'data-status-url="./index.json?&lt;x&gt;"' in html
    assert 'hidden data-argus-live-status' in html
    assert ".argus-live-status[hidden]{display:none}" in html
    assert 'http-equiv="refresh"' not in html

    plain = argus_serve._render_index(None, rows=[], banner_html="")
    assert "data-argus-live-status" not in plain
