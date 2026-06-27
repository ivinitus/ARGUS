"""Unit tests for the pure helpers in extractor.py (no network)."""
from pathlib import Path

import pytest

import extractor


def test_slugify_basics():
    assert extractor.slugify("Sprint 47 Regression") == "Sprint_47_Regression"
    assert extractor.slugify("Sprint 47 / Regression") == "Sprint_47_Regression"
    assert extractor.slugify("  foo  ") == "foo"
    assert extractor.slugify("hello.world-1") == "hello.world-1"
    assert extractor.slugify("") == "unnamed"
    assert extractor.slugify("///") == "unnamed"
    assert extractor.slugify("a!!b??c") == "a_b_c"


def test_derive_workdir_strips_cloned_suffix():
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/out"),
        execution_key="QA-E1",
    )
    # "(cloned)" suffix forms we've seen from test-management.
    for name in [
        "E2E Flow (cloned)",
        "E2E Flow cloned",
        "E2E Flow  (CLONED)",
        "E2E Flow",  # no suffix — should be unchanged
    ]:
        tr = [{"testRun": {"name": name}}]
        assert (
            extractor._derive_workdir(cfg, tr, tester_name="alice")
            == Path("/tmp/out/E2E_Flow/alice/QA-E1")
        ), f"failed for input: {name!r}"


def test_derive_workdir_uses_testrun_and_tester():
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/out"),
        execution_key="QA-E1",
    )
    tr = [{"testRun": {"name": "Sprint 47 Regression"}}]
    assert (
        extractor._derive_workdir(cfg, tr, tester_name="prathap bathula")
        == Path("/tmp/out/Sprint_47_Regression/prathap_bathula/QA-E1")
    )


def test_derive_workdir_unknown_tester():
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/out"),
        execution_key="QA-E1",
    )
    tr = [{"testRun": {"name": "Sprint 47 Regression"}}]
    # No tester name resolved -> 'unknown' bucket.
    assert (
        extractor._derive_workdir(cfg, tr, tester_name=None)
        == Path("/tmp/out/Sprint_47_Regression/unknown/QA-E1")
    )


def test_derive_workdir_falls_back_when_name_missing():
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/out"),
        execution_key="QA-E1",
    )
    # No testRun field (old testrun+item path doesn't return it).
    assert (
        extractor._derive_workdir(cfg, [{}], tester_name="alice")
        == Path("/tmp/out/alice/QA-E1")
    )


def test_derive_workdir_testrun_item_fallback():
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/out"),
        testrun_id=23507,
        item_id=1403823,
    )
    assert (
        extractor._derive_workdir(cfg, [], tester_name=None)
        == Path("/tmp/out/unknown/23507_1403823")
    )


def test_collect_pdf_attachments_top_level_only():
    test_results = [{
        "attachments": [
            {"id": 1, "name": "Session_Export.pdf"},
            {"id": 2, "name": "screenshot.png"},  # non-PDF ignored
        ],
        "testScriptResults": [],
    }]
    assert extractor.collect_pdf_attachments(test_results) == [
        (1, "Session_Export.pdf"),
    ]


def test_collect_pdf_attachments_on_step_only():
    # Real-world case from QA-E171442: PDF lives on step 0, not top-level.
    test_results = [{
        "attachments": [],
        "testScriptResults": [
            {"index": 0, "attachments": [
                {"id": 919630, "name": "Session_20260424_Export.pdf"},
            ]},
            {"index": 1, "attachments": []},
        ],
    }]
    assert extractor.collect_pdf_attachments(test_results) == [
        (919630, "Session_20260424_Export.pdf"),
    ]


def test_collect_pdf_attachments_dedupes_across_locations():
    # Same PDF attached both to the test result AND to a step — dedupe by id.
    test_results = [{
        "attachments": [
            {"id": 42, "name": "dup.pdf"},
        ],
        "testScriptResults": [
            {"attachments": [{"id": 42, "name": "dup.pdf"}]},
        ],
    }]
    assert extractor.collect_pdf_attachments(test_results) == [
        (42, "dup.pdf"),
    ]


def test_collect_pdf_attachments_no_pdfs():
    test_results = [{
        "attachments": [{"id": 1, "name": "log.txt"}],
        "testScriptResults": [
            {"attachments": [{"id": 2, "name": "img.jpg"}]},
        ],
    }]
    assert extractor.collect_pdf_attachments(test_results) == []


def test_collect_pdf_attachments_empty_input():
    assert extractor.collect_pdf_attachments([]) == []


def test_collect_pdf_attachments_tolerates_missing_fields():
    # Missing attachments, missing testScriptResults, None values.
    test_results = [
        {},
        {"attachments": None, "testScriptResults": None},
        {"attachments": [{"id": None, "name": "bad.pdf"}]},  # None id -> skip
    ]
    assert extractor.collect_pdf_attachments(test_results) == []


def test_collect_pdf_attachments_is_case_insensitive_extension():
    test_results = [{
        "attachments": [
            {"id": 1, "name": "Report.PDF"},
            {"id": 2, "name": "other.Pdf"},
        ],
    }]
    assert extractor.collect_pdf_attachments(test_results) == [
        (1, "Report.PDF"),
        (2, "other.Pdf"),
    ]


# ---------------------------------------------------------------------------
# Phase 1: tester ground-truth enrichment (status name, comment, traceLinks)
# ---------------------------------------------------------------------------


def test_strip_html_basic():
    # Pulled from real QA comments. Tags stripped, entities decoded.
    html = "it wont be reflected immediately since its a downgrade&nbsp;"
    assert extractor._strip_html(html) == "it wont be reflected immediately since its a downgrade"


def test_strip_html_images_only_returns_empty():
    # Comment with only inline images => nothing for the model to read.
    html = (
        '<img src="../rest/tests/1.0/attachment/image/920428" '
        'style="width: 300px;" class="fr-fic fr-fil fr-dib">'
    )
    assert extractor._strip_html(html) == ""


def test_strip_html_br_becomes_newline_then_collapses():
    assert extractor._strip_html("line1<br>line2<br/>line3") == "line1 line2 line3"


def test_strip_html_entities():
    assert extractor._strip_html("a &amp; b &lt;tag&gt;") == "a & b <tag>"


def test_strip_html_none_and_empty():
    assert extractor._strip_html(None) == ""
    assert extractor._strip_html("") == ""
    assert extractor._strip_html("   ") == ""


def test_normalize_trace_links_empty_inputs():
    assert extractor._normalize_trace_links(None) == []
    assert extractor._normalize_trace_links([]) == []
    assert extractor._normalize_trace_links("not a list") == []


def test_normalize_trace_links_dict_with_link():
    raw = [{"link": "QA-BUG-123", "type": "issue"}]
    assert extractor._normalize_trace_links(raw) == ["QA-BUG-123"]


def test_normalize_trace_links_dict_with_key_fallback():
    # No 'link' field — fall back to 'key', then 'url', then 'id'.
    raw = [{"key": "AUD-42"}, {"url": "https://x/y"}, {"id": 7}]
    assert extractor._normalize_trace_links(raw) == ["AUD-42", "https://x/y", "7"]


def test_normalize_trace_links_mixed_and_skips_nonstring_items():
    raw = ["AUD-9", {"link": "AUD-10"}, 42, None, {"type": "issue"}]  # 42 & None & blank dict dropped
    assert extractor._normalize_trace_links(raw) == ["AUD-9", "AUD-10"]


def test_build_metadata_resolves_status_names_at_top_level_and_steps():
    test_results = [{
        "id": 1,
        "testResultStatusId": 39,
        "testScriptResults": [
            {"index": 1, "testResultStatusId": 39,
             "description": "open page", "expectedResult": "loads"},
            {"index": 2, "testResultStatusId": 40,
             "description": "click button", "expectedResult": "modal opens"},
        ],
    }]
    status_map = {39: "Pass", 40: "Fail"}
    meta = extractor.build_metadata(test_results, status_map=status_map)
    tr = meta["test_results"][0]
    assert tr["status_id"] == 39
    assert tr["status"] == "Pass"
    # Raw id preserved so downstream code can still use it.
    assert tr["steps"][0]["status"] == "Pass"
    assert tr["steps"][1]["status"] == "Fail"


def test_build_metadata_status_none_when_id_not_in_map():
    test_results = [{
        "id": 1,
        "testResultStatusId": 9999,  # unknown project-custom status
        "testScriptResults": [],
    }]
    meta = extractor.build_metadata(test_results, status_map={39: "Pass"})
    assert meta["test_results"][0]["status_id"] == 9999
    assert meta["test_results"][0]["status"] is None


def test_build_metadata_no_status_map_degrades_gracefully():
    # status_map omitted — status field should still be present (None) so
    # downstream code has a consistent shape to check.
    test_results = [{
        "id": 1,
        "testResultStatusId": 39,
        "testScriptResults": [
            {"index": 1, "testResultStatusId": 39},
        ],
    }]
    meta = extractor.build_metadata(test_results)
    tr = meta["test_results"][0]
    assert tr["status_id"] == 39
    assert tr["status"] is None
    assert tr["steps"][0]["status"] is None


def test_build_metadata_strips_html_from_comments():
    test_results = [{
        "id": 1,
        "testResultStatusId": 39,
        "comment": "Overall went well&nbsp;but step 3 flaked.",
        "testScriptResults": [
            {"index": 3,
             "comment": "<br>had to retry twice<br>"},
        ],
    }]
    meta = extractor.build_metadata(test_results, status_map={39: "Pass"})
    tr = meta["test_results"][0]
    assert tr["comment"] == "Overall went well but step 3 flaked."
    assert tr["steps"][0]["comment"] == "had to retry twice"


def test_build_metadata_normalizes_trace_links():
    test_results = [{
        "id": 1,
        "traceLinks": [{"link": "QA-BUG-1"}],
        "testScriptResults": [
            {"index": 1, "traceLinks": [{"key": "QA-BUG-2"}, {"link": "QA-BUG-3"}]},
        ],
    }]
    meta = extractor.build_metadata(test_results)
    tr = meta["test_results"][0]
    assert tr["trace_links"] == ["QA-BUG-1"]
    assert tr["steps"][0]["trace_links"] == ["QA-BUG-2", "QA-BUG-3"]


def test_build_metadata_defaults_when_new_fields_missing():
    # Old-shape response with none of the new fields present — enrichment
    # must produce stable defaults so downstream tooling isn't surprised.
    test_results = [{
        "id": 1,
        "testResultStatusId": 39,
        "testScriptResults": [
            {"index": 1, "testResultStatusId": 39},
        ],
    }]
    meta = extractor.build_metadata(test_results, status_map={39: "Pass"})
    tr = meta["test_results"][0]
    assert tr["comment"] == ""
    assert tr["trace_links"] == []
    assert tr["steps"][0]["comment"] == ""
    assert tr["steps"][0]["trace_links"] == []


# ---------------------------------------------------------------------------
# Image attachment gathering (per-step + inline-comment + top-level images)
# ---------------------------------------------------------------------------


def test_extract_inline_image_ids_single():
    html = (
        'Before <img src="../rest/tests/1.0/attachment/image/920428" '
        'style="width:300px;" class="fr-fic"> After'
    )
    assert extractor._extract_inline_image_ids(html) == [920428]


def test_extract_inline_image_ids_multiple():
    html = (
        '<img src="/api/attachment/image/100" alt="a">'
        '<img src="../rest/tests/1.0/attachment/image/200">'
        'some text'
        '<img class="x" src="/other/attachment/image/300">'
    )
    assert extractor._extract_inline_image_ids(html) == [100, 200, 300]


def test_extract_inline_image_ids_ignores_non_attachment_imgs():
    # <img> pointing at an external resource, not the test-management attachment API.
    html = '<img src="https://example.com/logo.png">'
    assert extractor._extract_inline_image_ids(html) == []


def test_extract_inline_image_ids_handles_none_and_empty():
    assert extractor._extract_inline_image_ids(None) == []
    assert extractor._extract_inline_image_ids("") == []
    assert extractor._extract_inline_image_ids("no imgs here") == []


def test_is_image_name_common_extensions():
    assert extractor._is_image_name("shot.png")
    assert extractor._is_image_name("photo.JPG")
    assert extractor._is_image_name("screenshot.jpeg")
    assert extractor._is_image_name("anim.GIF")
    assert extractor._is_image_name("tight.webp")
    assert not extractor._is_image_name("session.pdf")
    assert not extractor._is_image_name("log.txt")
    assert not extractor._is_image_name(None)
    assert not extractor._is_image_name("")


def test_collect_image_attachments_top_level_image():
    test_results = [{
        "attachments": [
            {"id": 1, "name": "Session.pdf"},            # PDF ignored
            {"id": 2, "name": "cool.png"},               # kept
        ],
    }]
    out = extractor.collect_image_attachments(test_results)
    assert out == [
        {"id": 2, "name": "cool.png", "step_index": None,
         "source": "top_attachment"},
    ]


def test_collect_image_attachments_top_comment_inline():
    # Tester pasted 2 screenshots into the overall remarks comment.
    test_results = [{
        "comment": (
            '<img src="../rest/tests/1.0/attachment/image/111">'
            '<img src="../rest/tests/1.0/attachment/image/222">'
        ),
        "testScriptResults": [],
    }]
    out = extractor.collect_image_attachments(test_results)
    assert [(r["id"], r["source"], r["step_index"]) for r in out] == [
        (111, "top_comment_inline", None),
        (222, "top_comment_inline", None),
    ]


def test_collect_image_attachments_step_attachment_and_inline():
    test_results = [{
        "testScriptResults": [
            {"index": 3,
             "attachments": [{"id": 500, "name": "step3.png"}],
             "comment": '<img src="../rest/tests/1.0/attachment/image/501">'},
            {"index": 4,
             "attachments": [{"id": 600, "name": "note.pdf"}],  # PDF skipped
             "comment": None},
        ],
    }]
    out = extractor.collect_image_attachments(test_results)
    records = [(r["id"], r["step_index"], r["source"]) for r in out]
    assert records == [
        (500, 3, "step_attachment"),
        (501, 3, "step_comment_inline"),
    ]


def test_collect_image_attachments_dedupes_same_id_same_step():
    # Same image id referenced twice on the same step — record it once.
    test_results = [{
        "testScriptResults": [
            {"index": 1,
             "attachments": [{"id": 42, "name": "x.png"}],
             "comment": '<img src="../rest/tests/1.0/attachment/image/42">'},
        ],
    }]
    out = extractor.collect_image_attachments(test_results)
    assert len(out) == 1
    assert out[0]["id"] == 42


def test_collect_image_attachments_keeps_same_id_across_different_steps():
    # If the same attachment id appears on different steps (or top + step),
    # we keep separate records because the step attribution differs.
    test_results = [{
        "attachments": [{"id": 42, "name": "x.png"}],
        "testScriptResults": [
            {"index": 3,
             "attachments": [{"id": 42, "name": "x.png"}]},
        ],
    }]
    out = extractor.collect_image_attachments(test_results)
    step_indices = sorted((r["step_index"] or -1 for r in out))
    assert step_indices == [-1, 3]  # top + step 3


def test_collect_image_attachments_empty_input():
    assert extractor.collect_image_attachments([]) == []
    assert extractor.collect_image_attachments([{}]) == []


def test_step_attachment_path_per_step():
    rec = {"id": 42, "name": "screenshot.png", "step_index": 5}
    p = extractor._step_attachment_path(Path("/tmp/step"), rec)
    assert p == Path("/tmp/step/step_005_42_screenshot.png")


def test_step_attachment_path_top_level():
    rec = {"id": 99, "name": "top.png", "step_index": None}
    p = extractor._step_attachment_path(Path("/tmp/step"), rec)
    assert p == Path("/tmp/step/top_99_top.png")


def test_step_attachment_path_sanitizes_spaces_and_specials():
    rec = {"id": 1, "name": "My Screenshot 2025-02-21 at 2.49.09 PM.png",
           "step_index": 2}
    p = extractor._step_attachment_path(Path("/tmp/step"), rec)
    # Spaces and colons collapsed into underscores.
    assert " " not in p.name
    assert p.name.startswith("step_002_1_")
    assert p.name.endswith(".png")


# ---------------------------------------------------------------------------
# Phase 2: operator alerts (silent-failure visibility)
# ---------------------------------------------------------------------------


def test_alert_operator_writes_to_stderr(capsys):
    """Alerts must reach stderr regardless of the argus logger level so
    operators see silent-degradation warnings (status-map fetch failure,
    missing projectId) live — not post-hoc in audit.json or only under -v."""
    extractor._alert_operator("status-map fetch returned empty for TEST-E1")
    captured = capsys.readouterr()
    assert "status-map fetch returned empty for TEST-E1" in captured.err
    # Leading newline keeps the alert on its own line when a spinner is
    # actively rewriting with \r above it.
    assert captured.err.startswith("\n[argus] ALERT:")


def test_alert_operator_bypasses_default_log_silencing(capsys, caplog):
    """Even when the argus logger is at ERROR (the non-verbose default),
    the alert must still be visible on stderr. log.warning alone would
    be silent here."""
    import logging
    extractor.configure_logging(verbose=False)
    # Defensively set the level in case a previous test mutated it.
    logging.getLogger("argus").setLevel(logging.ERROR)

    extractor._alert_operator("must be visible even at ERROR level")
    captured = capsys.readouterr()
    assert "must be visible even at ERROR level" in captured.err


# ---------------------------------------------------------------------------
# Phase 3 hardening: Tracker 429 retry + LLM Runtime image-dimension resizing
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    """requests.Response.headers-like dict that preserves dict semantics."""


class _FakeResponse:
    """Minimal stand-in for requests.Response covering the surface
    fetch_test_results + download_attachment touch."""

    def __init__(self, status_code=200, json_body=None, body_bytes=b"",
                 headers=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self._body = body_bytes
        self.headers = _FakeHeaders(headers or {})
        self._closed = False

    def json(self):
        return self._json

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            import requests
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    # Streaming support for download_attachment.
    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self):
        self._closed = True

    # Context-manager support: `with session.get(...) as resp:`
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _FakeSession:
    """Canned-response session. Returns responses from the queue in
    order. Records each call for inspection."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers = {}

    def get(self, url, params=None, stream=False, timeout=None):
        self.calls.append({"url": url, "params": params, "stream": stream,
                           "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeSession ran out of scripted responses")
        return self._responses.pop(0)


def test_fetch_test_results_retries_on_429(monkeypatch):
    """Two 429s then a 200 with a real body — fetch_test_results should
    return the final body without raising."""
    # Speed up the backoff so tests run instantly.
    monkeypatch.setattr(extractor.time, "sleep", lambda s: None) \
        if hasattr(extractor, "time") else None

    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    session = _FakeSession([
        _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
        _FakeResponse(status_code=429),
        _FakeResponse(status_code=200, json_body={"id": 1, "steps": []}),
    ])
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/unused"),
        execution_key="QA-E1",
    )
    result = extractor.fetch_test_results(session, cfg)
    # Returned a list with the single successful response body.
    assert result == [{"id": 1, "steps": []}]
    # All three attempts made.
    assert len(session.calls) == 3


def test_fetch_test_results_raises_after_max_attempts(monkeypatch):
    """Six consecutive 429s (current default max_attempts=6 in
    extractor._get_with_retry) must surface as a requests.HTTPError
    via _raise_friendly — not silently degrade. Was 4 attempts in
    earlier versions; bumped to 6 when the project moved from a
    single-key extract path to parallel folder-mode (more contention
    on Tracker → more transient 429s, deeper retry budget needed)."""
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    session = _FakeSession([_FakeResponse(status_code=429) for _ in range(6)])
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/unused"),
        execution_key="QA-E1",
    )
    import requests
    with pytest.raises(requests.HTTPError):
        extractor.fetch_test_results(session, cfg)
    assert len(session.calls) == 6


def test_download_attachment_retries_on_429(tmp_path: Path, monkeypatch):
    """One 429 then a 200 with body bytes — download_attachment must
    stream the bytes from the successful response, not the 429."""
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    payload = b"fake-pdf-bytes"
    session = _FakeSession([
        _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
        _FakeResponse(status_code=200, body_bytes=payload),
    ])
    dest = tmp_path / "out.pdf"
    extractor.download_attachment(session, "https://tracker.test", 42, dest)
    assert dest.read_bytes() == payload
    assert len(session.calls) == 2


def test_download_attachment_surfaces_terminal_429(
        tmp_path: Path, monkeypatch):
    """If every attempt fails with 429, the final response's HTTPError
    must propagate so the caller records the failure."""
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    session = _FakeSession([_FakeResponse(status_code=429) for _ in range(4)])
    dest = tmp_path / "out.pdf"
    import requests
    with pytest.raises(requests.HTTPError):
        extractor.download_attachment(session, "https://tracker.test", 42, dest)


# ---------------------------------------------------------------------------
# _resize_if_oversized: pymupdf-backed in-place image resize
# ---------------------------------------------------------------------------


def _write_solid_png(path: Path, width: int, height: int) -> None:
    """Write a valid PNG of `width`×`height` using pymupdf so tests
    don't need Pillow or hand-rolled PNG writers beyond the existing
    tiny-1×1 fixture."""
    import fitz
    # Create a blank pixmap (3-channel, no alpha) and fill it.
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height))
    pix.clear_with(200)  # mid-grey
    pix.save(str(path))


def _write_png_with_dpi(path: Path, width: int, height: int, dpi: int) -> None:
    """Write a grayscale PNG with a specific DPI in its pHYs chunk.

    Simulates a Retina screenshot: raw pixels are width×height but the
    PNG metadata declares a higher-than-72 DPI. Hand-rolled so tests
    don't need Pillow. Stays minimal — 8-bit grayscale, solid-color —
    because we only care about the dimension and DPI metadata.
    """
    import struct
    import zlib

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR: width, height, bit_depth=8, color_type=0 (grayscale), rest zero.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    # pHYs: px/m (x, y), unit (1=meter). DPI -> px/m conversion.
    px_per_meter = int(round(dpi / 0.0254))
    phys = struct.pack(">IIB", px_per_meter, px_per_meter, 1)
    # IDAT: every row prefixed with filter byte (0=None), one byte per pixel.
    raw_row = b"\x00" + b"\x80" * width
    idat = zlib.compress(raw_row * height)
    path.write_bytes(
        sig + _chunk(b"IHDR", ihdr) + _chunk(b"pHYs", phys)
        + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def test_resize_if_oversized_shrinks_oversized_image(tmp_path: Path):
    """A 3000×2000 PNG (both dimensions > 1568 cap) must be resized
    to fit within 1568px on the long edge, preserving aspect ratio."""
    path = tmp_path / "screenshot.png"
    _write_solid_png(path, 3000, 2000)

    changed = extractor._resize_if_oversized(path)
    assert changed is True

    import fitz
    with fitz.open(str(path)) as doc:
        page = doc[0]
        long_edge = max(page.rect.width, page.rect.height)
        short_edge = min(page.rect.width, page.rect.height)
    assert long_edge <= extractor._MAX_STEP_IMAGE_EDGE_PX
    # Aspect ratio preserved (3:2) — short edge ≈ long_edge * 2/3.
    assert short_edge == pytest.approx(long_edge * 2 / 3, rel=0.02)


def test_resize_if_oversized_passes_through_small_image(tmp_path: Path):
    """A 1000×800 PNG is already within the cap — no resize, no rewrite."""
    path = tmp_path / "small.png"
    _write_solid_png(path, 1000, 800)
    before_mtime = path.stat().st_mtime_ns

    changed = extractor._resize_if_oversized(path)
    assert changed is False
    # File not rewritten: mtime unchanged.
    assert path.stat().st_mtime_ns == before_mtime


def test_resize_if_oversized_fails_open_on_corrupt_file(tmp_path: Path):
    """Unsupported / corrupt file: log a warning, leave untouched,
    return False. The dimension cap is a guard, not a gate — we don't
    want to drop evidence because of a cosmetic resize failure."""
    path = tmp_path / "corrupt.png"
    path.write_bytes(b"not actually a png")
    before = path.read_bytes()

    changed = extractor._resize_if_oversized(path)
    assert changed is False
    # Original bytes preserved.
    assert path.read_bytes() == before


def test_resize_if_oversized_respects_custom_cap(tmp_path: Path):
    """Caller can override the max edge (e.g. to match a smaller limit
    for a specific experiment)."""
    path = tmp_path / "medium.png"
    _write_solid_png(path, 1200, 900)

    # Default cap (1568) should not resize this image.
    assert extractor._resize_if_oversized(path) is False

    # Tighter cap (500) should resize.
    assert extractor._resize_if_oversized(path, max_edge=500) is True
    import fitz
    with fitz.open(str(path)) as doc:
        assert max(doc[0].rect.width, doc[0].rect.height) <= 500


def test_resize_if_oversized_uses_pixel_dimensions_not_points(tmp_path: Path):
    """Regression: Retina screenshots store DPI in the PNG pHYs chunk.
    fitz.open(path)[0].rect returns POINTS (pixel_count × 72 / dpi),
    so a 2370×474 PNG at 144 DPI reports rect=1185×237 — under the
    1568 cap — even though the actual pixel count is oversized and
    LLM Runtime rejects it with 'image dimensions exceed 2000 pixels'.
    The fix is to use fitz.Pixmap(path).width/height which reports
    pixel counts directly.
    """
    import fitz
    # Pixel size 2370×474 — above the 1568 cap — but pointsize 1185×237
    # (half, because DPI=144 and points = pixels × 72 / 144 = pixels/2).
    # Matches the real-world QA-E176234 / E176251 failure shape.
    path = tmp_path / "retina.png"
    _write_png_with_dpi(path, 2370, 474, dpi=144)

    # Sanity: Pixmap reports pixel dimensions (what actually reaches
    # LLM Runtime). fitz.open().rect would report halved points, which is
    # the trap this regression test exists to prevent.
    pix = fitz.Pixmap(str(path))
    assert pix.width == 2370
    assert pix.height == 474

    changed = extractor._resize_if_oversized(path)
    assert changed is True, (
        "oversized-by-pixels image must be resized even when "
        "fitz.open().rect would report it as under-cap (points)"
    )

    # After resize, actual pixel count must be within cap — this is
    # what LLM Runtime sees.
    pix2 = fitz.Pixmap(str(path))
    assert max(pix2.width, pix2.height) <= extractor._MAX_STEP_IMAGE_EDGE_PX


# ---------------------------------------------------------------------------
# Platform parser tests — surface, browser, device parsed from testrun names
# ---------------------------------------------------------------------------
class TestParsePlatformFromTestrunName:
    def test_desktop_chrome(self):
        n = "E2E Flow - GMA - AU Desktop - (Chrome)"
        assert extractor.parse_surface_from_testrun_name(n) == "Desktop"
        assert extractor.parse_browser_from_testrun_name(n) == "Chrome"
        assert extractor.parse_device_from_testrun_name(n) is None

    def test_desktop_firefox(self):
        n = "E2E Flow - GMA - CA Desktop - (Firefox)"
        assert extractor.parse_surface_from_testrun_name(n) == "Desktop"
        assert extractor.parse_browser_from_testrun_name(n) == "Firefox"

    def test_desktop_edge_no_parens(self):
        n = "E2E Flow - GMB - IT  Desktop - Edge"
        assert extractor.parse_surface_from_testrun_name(n) == "Desktop"
        assert extractor.parse_browser_from_testrun_name(n) == "Edge"

    def test_mobile_edge_samsung(self):
        n = "E2E Flow - GMA - AU Mobile - (Edge - Samsung)"
        assert extractor.parse_surface_from_testrun_name(n) == "Mobile"
        assert extractor.parse_browser_from_testrun_name(n) == "Edge"
        assert extractor.parse_device_from_testrun_name(n) == "Samsung"

    def test_mobile_firefox_iphone(self):
        n = "E2E Flow - GMB - IT Mobile - (Firefox - iphone)"
        assert extractor.parse_surface_from_testrun_name(n) == "Mobile"
        assert extractor.parse_browser_from_testrun_name(n) == "Firefox"
        assert extractor.parse_device_from_testrun_name(n) == "iPhone"

    def test_arya_android(self):
        n = "Arya Regression - US Mobile - Playback - Android (cloned) (cloned)"
        assert extractor.parse_surface_from_testrun_name(n) == "Mobile"
        assert extractor.parse_device_from_testrun_name(n) == "Android"

    def test_arya_ios(self):
        n = "Arya Regression - US Mobile - Playback - iOS (cloned) (cloned)"
        assert extractor.parse_surface_from_testrun_name(n) == "Mobile"
        assert extractor.parse_device_from_testrun_name(n) == "iOS"

    def test_webview(self):
        n = "Webview E2E - JP (cloned) (cloned)"
        assert extractor.parse_surface_from_testrun_name(n) == "Webview"

    def test_additional_non_automated_no_platform(self):
        # "Additional Non Automated cases" runs don't carry browser/device
        # info — parsers must return None, not guess.
        n = "Additional Non Automated cases - GMA - AU (cloned) (cloned)"
        assert extractor.parse_surface_from_testrun_name(n) is None
        assert extractor.parse_browser_from_testrun_name(n) is None
        assert extractor.parse_device_from_testrun_name(n) is None

    def test_empty_and_none(self):
        for n in (None, "", "   "):
            assert extractor.parse_surface_from_testrun_name(n) is None
            assert extractor.parse_browser_from_testrun_name(n) is None
            assert extractor.parse_device_from_testrun_name(n) is None


# ---------------------------------------------------------------------------
# get_testresult_summary — Layer 1 re-audit detection plumbing
# ---------------------------------------------------------------------------
def test_get_testresult_summary_includes_execution_date():
    """The /testresult/{id} call MUST request executionDate alongside
    id/key/userKey, and the helper MUST surface it as `execution_date`
    (snake_case to match metadata.json's shape) so argus can compare it
    to the cached date and trigger re-audits when testers re-execute.
    """
    payload = {
        "id": 1,
        "key": "QA-E1",
        "userKey": "USER1",
        "executionDate": "2026-05-27T10:00:00Z",
    }
    session = _FakeSession([_FakeResponse(status_code=200, json_body=payload)])

    result = extractor.get_testresult_summary(session, "https://tracker.test", 42)

    # The returned dict carries the cached date in snake_case.
    assert result == {
        "key": "QA-E1",
        "user_key": "USER1",
        "execution_date": "2026-05-27T10:00:00Z",
    }
    # The HTTP call asked Tracker for executionDate — without this field in
    # the `fields` param, Tracker returns the testresult object minus the
    # date, which would silently break re-audit detection.
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://tracker.test/rest/tests/1.0/testresult/42"
    fields = call["params"]["fields"]
    assert "executionDate" in fields
    # Existing fields still requested — adding the new one must not
    # accidentally drop key/userKey resolution that downstream code
    # (chat grouping by tester, audit prompt) depends on.
    assert "id" in fields
    assert "key" in fields
    assert "userKey" in fields


def test_get_testresult_summary_handles_missing_execution_date():
    """Older test-management responses (or responses where the field is null)
    must not crash. Missing executionDate -> execution_date=None, which
    the caller treats as "live date unknown" and falls through to
    re-audit (the safe direction).
    """
    payload = {"id": 1, "key": "QA-E1", "userKey": "USER1"}
    session = _FakeSession([_FakeResponse(status_code=200, json_body=payload)])

    result = extractor.get_testresult_summary(session, "https://tracker.test", 7)

    assert result == {
        "key": "QA-E1",
        "user_key": "USER1",
        "execution_date": None,
    }


def test_get_testresult_summary_returns_none_on_http_error():
    """Non-200 -> None preserves the existing contract. Caller's
    `if not summary` branch counts it as unresolved and skips the key
    without emitting a half-formed entry to the batch runner.
    """
    session = _FakeSession([_FakeResponse(status_code=404)])
    result = extractor.get_testresult_summary(session, "https://tracker.test", 99)
    assert result is None


# ---------------------------------------------------------------------------
# build_coverage_items (R15 data-shape helper)
# ---------------------------------------------------------------------------
def test_build_coverage_items_returns_executed_and_unexecuted():
    """Every item from items_by_run becomes a CoverageItem — no PASS/FAIL
    filtering. Unlike argus._run_folder_list (which DOES filter), R15
    needs the unexecuted ones to detect coverage holes."""
    runs = [
        {"id": 100, "key": "QA-C1",
         "name": "E2E Flow - GMA - US Desktop"},
    ]
    items_by_run = {
        100: [
            {"id": 1, "$lastTestResult": {
                "id": 11, "testResultStatusId": 39,  # Pass
                "testCase": {"key": "QA-T1"},
                "userKey": "USER_A",
            }},
            {"id": 2, "$lastTestResult": {
                "id": 12, "testResultStatusId": 41,  # Blocked
                "testCase": {"key": "QA-T2"},
                "userKey": "USER_B",
            }},
            {"id": 3, "$lastTestResult": {
                "id": 13, "testResultStatusId": 37,  # Not Executed
                "testCase": {"key": "QA-T3"},
                "userKey": "USER_C",
            }},
        ],
    }
    status_map = {39: "Pass", 40: "Fail", 41: "Blocked",
                  37: "Not Executed"}

    # No HTTP should occur because items_by_run carries everything.
    # Pass a session that fails on .get() to prove that.
    class _NoHTTPSession:
        def get(self, *a, **kw):
            raise AssertionError("no HTTP expected — items_by_run pre-fed")

    items = extractor.build_coverage_items(
        _NoHTTPSession(), "https://tracker.test",
        runs, status_map,
        user_cache={"USER_A": "Alice", "USER_B": "Bob",
                    "USER_C": "Carol"},
        items_by_run=items_by_run,
    )
    # All three items appear, regardless of executed state.
    assert len(items) == 3
    statuses = {it.status for it in items}
    assert statuses == {"Pass", "Blocked", "Not Executed"}
    assert [it.e_key for it in items] == [None, None, None]


def test_build_coverage_items_resolves_e_keys_only_when_opted_in(
    tmp_path,
    monkeypatch,
):
    """Roster E-key enrichment is intentionally opt-in because it makes
    per-testresult HTTP calls on a cold cache."""
    monkeypatch.setattr(extractor, "_EKEY_CACHE_PATH", tmp_path / "ekeys.json")
    runs = [
        {"id": 100, "key": "QA-C1",
         "name": "E2E Flow - GMA - US Desktop"},
    ]
    items_by_run = {
        100: [
            {"id": 1, "$lastTestResult": {
                "id": 11, "testResultStatusId": 39,
                "testCase": {"key": "QA-T1"},
                "userKey": "USER_A",
            }},
            {"id": 2, "$lastTestResult": {
                "id": 12, "testResultStatusId": 41,
                "testCase": {"key": "QA-T2"},
                "userKey": "USER_B",
            }},
            {"id": 3, "$lastTestResult": {
                "id": 13, "testResultStatusId": 37,
                "testCase": {"key": "QA-T3"},
            }},
        ],
    }
    status_map = {39: "Pass", 41: "Blocked", 37: "Not Executed"}
    session = _FakeSession([
        _FakeResponse(status_code=200, json_body={"key": "QA-E11"}),
        _FakeResponse(status_code=200, json_body={"key": "QA-E12"}),
    ])

    items = extractor.build_coverage_items(
        session, "https://tracker.test", runs, status_map,
        user_cache={"USER_A": "Alice", "USER_B": "Bob"},
        items_by_run=items_by_run,
        resolve_e_keys=True,
    )

    assert [it.e_key for it in items] == ["QA-E11", "QA-E12", None]
    assert [call["url"] for call in session.calls] == [
        "https://tracker.test/rest/tests/1.0/testresult/11",
        "https://tracker.test/rest/tests/1.0/testresult/12",
    ]
    assert all(call["params"] == {"fields": "id,key"}
               for call in session.calls)

    class _NoHTTPSession:
        def get(self, *a, **kw):
            raise AssertionError("warm E-key cache should avoid HTTP")

    cached_items = extractor.build_coverage_items(
        _NoHTTPSession(), "https://tracker.test", runs, status_map,
        user_cache={"USER_A": "Alice", "USER_B": "Bob"},
        items_by_run=items_by_run,
        resolve_e_keys=True,
    )
    assert [it.e_key for it in cached_items] == [
        "QA-E11", "QA-E12", None,
    ]


def test_build_coverage_items_parses_marketplace_from_testrun_name():
    """testrun_name -> marketplace via the deterministic parser; the
    resulting CoverageItem.marketplace must match the same value
    parse_marketplace_from_testrun_name returns directly."""
    runs = [
        {"id": 200, "key": "QA-C2",
         "name": "Webview E2E - BR (cloned)"},
        {"id": 201, "key": "QA-C3",
         "name": "Arya Regression - JP Mobile - Playback - iOS"},
    ]
    items_by_run = {
        200: [{"id": 1, "$lastTestResult": {
            "id": 11, "testResultStatusId": 39,
            "testCase": {"key": "QA-T1"},
        }}],
        201: [{"id": 2, "$lastTestResult": {
            "id": 12, "testResultStatusId": 41,
            "testCase": {"key": "QA-T2"},
        }}],
    }
    status_map = {39: "Pass", 41: "Blocked"}

    class _NoHTTPSession:
        def get(self, *a, **kw):
            raise AssertionError("no HTTP expected")

    items = extractor.build_coverage_items(
        _NoHTTPSession(), "https://tracker.test", runs, status_map,
        items_by_run=items_by_run,
    )
    # Two items — one BR (Webview), one JP (Mobile iOS).
    by_run = {it.testrun_key: it for it in items}
    assert by_run["QA-C2"].marketplace == "BR"
    assert by_run["QA-C3"].marketplace == "JP"
