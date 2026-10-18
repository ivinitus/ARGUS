"""Unit tests for the pure helpers in extractor.py (no network)."""
from copy import deepcopy
from pathlib import Path

import pytest
import requests

from argus.pipeline import extractor


def test_webm_recordings_are_supported_and_keep_their_container_extension(tmp_path):
    records = [{
        "attachments": [{"id": 11, "name": "recording.WEBM", "fileSize": 100}],
        "testScriptResults": [{"index": 1, "attachments": [
            {"id": 12, "name": "second.mp4"},
        ]}],
    }]
    videos = extractor.collect_video_attachments(records)
    assert [video["id"] for video in videos] == [11, 12]
    assert extractor.collect_unsupported_attachments(records) == []
    assert extractor._video_attachment_path(tmp_path, videos[0]) == tmp_path / "top_11.webm"
    assert extractor._video_attachment_path(tmp_path, videos[1]) == tmp_path / "step_001_12.mp4"


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
    for name in [
        "E2E Flow (cloned)",
        "E2E Flow cloned",
        "E2E Flow  (CLONED)",
        "E2E Flow",
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
    assert (
        extractor._derive_workdir(cfg, tr, tester_name=None)
        == Path("/tmp/out/Sprint_47_Regression/unknown/QA-E1")
    )


def test_derive_workdir_falls_back_when_name_missing():
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/out"),
        execution_key="QA-E1",
    )
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
            {"id": 2, "name": "screenshot.png"},
        ],
        "testScriptResults": [],
    }]
    assert extractor.collect_pdf_attachments(test_results) == [
        (1, "Session_Export.pdf"),
    ]


def test_collect_pdf_attachments_on_step_only():
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
    test_results = [
        {},
        {"attachments": None, "testScriptResults": None},
        {"attachments": [{"id": None, "name": "bad.pdf"}]},
    ]
    assert extractor.collect_pdf_attachments(test_results) == []


def test_collect_pdf_attachments_rejects_non_positive_integer_ids():
    test_results = [{
        "attachments": [
            {"id": "../../escape", "name": "path.pdf"},
            {"id": True, "name": "boolean.pdf"},
            {"id": 0, "name": "zero.pdf"},
            {"id": -1, "name": "negative.pdf"},
            {"id": 42, "name": "../allowed display name.pdf"},
        ],
    }]

    assert extractor.collect_pdf_attachments(test_results) == [
        (42, "../allowed display name.pdf"),
    ]


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




def test_strip_html_basic():
    html = "it wont be reflected immediately since its a downgrade&nbsp;"
    assert extractor._strip_html(html) == "it wont be reflected immediately since its a downgrade"


def test_strip_html_images_only_returns_empty():
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
    raw = [{"key": "AUD-42"}, {"url": "https://x/y"}, {"id": 7}]
    assert extractor._normalize_trace_links(raw) == ["AUD-42", "https://x/y", "7"]


def test_normalize_trace_links_mixed_and_skips_nonstring_items():
    raw = ["AUD-9", {"link": "AUD-10"}, 42, None, {"type": "issue"}]
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
    assert tr["steps"][0]["status"] == "Pass"
    assert tr["steps"][1]["status"] == "Fail"


def test_build_metadata_status_none_when_id_not_in_map():
    test_results = [{
        "id": 1,
        "testResultStatusId": 9999,
        "testScriptResults": [],
    }]
    meta = extractor.build_metadata(test_results, status_map={39: "Pass"})
    assert meta["test_results"][0]["status_id"] == 9999
    assert meta["test_results"][0]["status"] is None


def test_build_metadata_no_status_map_degrades_gracefully():
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


def test_build_metadata_sanitizes_comments_and_retains_web_links():
    test_results = [{
        "id": 1,
        "comment": "account qa@example.test Fixture9Secret",
        "webLinks": [{"link": "https://issues.example.com/D517315454"}],
        "testScriptResults": [{
            "index": 1,
            "comment": "passcode: Mobile7Code",
        }],
    }]
    tr = extractor.build_metadata(test_results)["test_results"][0]
    assert tr["comment"] == "account qa@example.test [redacted]"
    assert tr["steps"][0]["comment"] == "passcode: [redacted]"
    assert tr["weblinks"] == [
        "https://issues.example.com/D517315454"
    ]


def test_build_metadata_defaults_when_new_fields_missing():
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
    assert tr["evidence_fingerprint"] == extractor.build_evidence_fingerprint(
        test_results)
    assert tr["steps"][0]["comment"] == ""
    assert tr["steps"][0]["trace_links"] == []




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


def _base_evidence_result():
    return {
        "attachments": [
            {"id": 101, "name": "session.pdf", "fileSize": 1234},
            {"id": 102, "name": "top.png", "fileSize": 2345},
            {"id": 103, "name": "debug.txt", "fileSize": 3456},
        ],
        "comment": '<img src="../rest/tests/1.0/attachment/image/104">',
        "testScriptResults": [
            {
                "index": 2,
                "attachments": [
                    {"id": 201, "name": "step2.jpg", "fileSize": 4567}
                ],
                "comment": '<img src="../rest/tests/1.0/attachment/image/202">',
            },
            {
                "index": 1,
                "attachments": [
                    {"id": 203, "name": "step1.pdf", "fileSize": 5678}
                ],
            },
        ],
    }


def test_build_evidence_fingerprint_stable_when_attachment_order_changes():
    left = _base_evidence_result()
    right = deepcopy(left)
    right["attachments"] = list(reversed(right["attachments"]))
    right["testScriptResults"] = list(reversed(right["testScriptResults"]))

    assert (
        extractor.build_evidence_fingerprint([left])
        == extractor.build_evidence_fingerprint([right])
    )


def test_build_evidence_fingerprint_changes_for_audit_evidence_changes():
    base = _base_evidence_result()
    base_fp = extractor.build_evidence_fingerprint([base])

    changed_id = deepcopy(base)
    changed_id["attachments"][0]["id"] = 999

    changed_name = deepcopy(base)
    changed_name["attachments"][0]["name"] = "replacement.pdf"

    changed_size = deepcopy(base)
    changed_size["attachments"][0]["fileSize"] = 9999

    changed_step = deepcopy(base)
    changed_step["testScriptResults"][0]["index"] = 9

    changed_inline = deepcopy(base)
    changed_inline["testScriptResults"][0]["comment"] = (
        '<img src="../rest/tests/1.0/attachment/image/909">'
    )

    for changed in (
        changed_id,
        changed_name,
        changed_size,
        changed_step,
        changed_inline,
    ):
        assert extractor.build_evidence_fingerprint([changed]) != base_fp


def test_build_evidence_fingerprint_ignores_non_evidence_attachments():
    base = _base_evidence_result()
    changed = deepcopy(base)
    changed["attachments"][2]["id"] = 999
    changed["attachments"][2]["name"] = "different.txt"
    changed["attachments"][2]["fileSize"] = 9999

    assert (
        extractor.build_evidence_fingerprint([changed])
        == extractor.build_evidence_fingerprint([base])
    )


def test_build_evidence_fingerprint_tracks_unsupported_evidence_attachments():
    base = _base_evidence_result()
    base["attachments"][2] = {
        "id": 103,
        "name": "recording.mov",
        "fileSize": 3456,
    }
    changed = deepcopy(base)
    changed["attachments"][2]["id"] = 999
    changed["attachments"][2]["name"] = "replacement.mkv"
    changed["attachments"][2]["fileSize"] = 9999

    assert (
        extractor.build_evidence_fingerprint([changed])
        != extractor.build_evidence_fingerprint([base])
    )


def test_build_evidence_fingerprint_accepts_cached_metadata_shape():
    raw = {
        "attachments": [{"id": 101, "name": "session.pdf", "fileSize": 1234}],
        "testScriptResults": [{
            "index": 1,
            "attachments": [{"id": 201, "name": "step.png", "fileSize": 456}],
        }],
    }
    cached_metadata = {
        "attachments": [{"id": 101, "name": "session.pdf", "size": 1234}],
        "steps": [{
            "index": 1,
            "attachments": [{"id": 201, "name": "step.png", "size": 456}],
        }],
    }

    assert (
        extractor.build_evidence_fingerprint([cached_metadata])
        == extractor.build_evidence_fingerprint([raw])
    )


def test_collect_image_attachments_top_level_image():
    test_results = [{
        "attachments": [
            {"id": 1, "name": "Session.pdf"},
            {"id": 2, "name": "cool.png"},
        ],
    }]
    out = extractor.collect_image_attachments(test_results)
    assert out == [
        {"id": 2, "name": "cool.png", "step_index": None,
         "source": "top_attachment"},
    ]


def test_collect_image_attachments_top_comment_inline():
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
             "attachments": [{"id": 600, "name": "note.pdf"}],
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
    test_results = [{
        "attachments": [{"id": 42, "name": "x.png"}],
        "testScriptResults": [
            {"index": 3,
             "attachments": [{"id": 42, "name": "x.png"}]},
        ],
    }]
    out = extractor.collect_image_attachments(test_results)
    step_indices = sorted((r["step_index"] or -1 for r in out))
    assert step_indices == [-1, 3]


def test_collect_image_attachments_empty_input():
    assert extractor.collect_image_attachments([]) == []
    assert extractor.collect_image_attachments([{}]) == []


def test_collect_video_attachments_handles_raw_and_cached_shapes():
    raw = [{
        "attachments": [
            {"id": 1109069, "name": "video.mp4", "fileSize": 39838260},
            {"id": 2, "name": "session.pdf", "fileSize": 42},
        ],
        "testScriptResults": [{
            "index": 3,
            "attachments": [{"id": 3, "name": "step-video.MP4", "size": 7}],
        }],
    }]
    cached = [{
        "attachments": [
            {"id": 1109069, "name": "video.mp4", "size": 39838260},
        ],
        "steps": [{
            "index": 3,
            "attachments": [{"id": 3, "name": "step-video.MP4", "size": 7}],
        }],
    }]

    expected = [
        {
            "id": 1109069, "name": "video.mp4", "size": 39838260,
            "step_index": None, "source": "top_attachment",
        },
        {
            "id": 3, "name": "step-video.MP4", "size": 7,
            "step_index": 3, "source": "step_attachment",
        },
    ]
    assert extractor.collect_video_attachments(raw) == expected
    assert extractor.collect_video_attachments(cached) == expected


def test_collect_unsupported_attachments_handles_raw_and_cached_shapes():
    raw = [{
        "attachments": [
            {"id": 1, "name": "recording.mov", "fileSize": 39838260},
            {"id": 2, "name": "session.pdf", "fileSize": 42},
            {"id": 4, "name": "debug.txt", "fileSize": 10},
            {"id": 5, "name": None, "fileSize": 20},
        ],
        "testScriptResults": [{
            "index": 3,
            "attachments": [{"id": 3, "name": "recording.mov", "size": 7}],
        }],
    }]
    cached = [{
        "attachments": [
            {"id": 1, "name": "recording.mov", "size": 39838260},
            {"id": 2, "name": "session.pdf", "size": 42},
            {"id": 4, "name": "debug.txt", "size": 10},
            {"id": 5, "name": None, "size": 20},
        ],
        "steps": [{
            "index": 3,
            "attachments": [{"id": 3, "name": "recording.mov", "size": 7}],
        }],
    }]

    expected = [
        {
            "id": 1,
            "name": "recording.mov",
            "size": 39838260,
            "step_index": None,
            "source": "top_attachment",
        },
        {
            "id": 3,
            "name": "recording.mov",
            "size": 7,
            "step_index": 3,
            "source": "step_attachment",
        },
    ]
    assert extractor.collect_unsupported_attachments(raw) == expected
    assert extractor.collect_unsupported_attachments(cached) == expected


def test_step_attachment_path_per_step():
    rec = {"id": 42, "name": "screenshot.png", "step_index": 5}
    p = extractor._step_attachment_path(Path("/tmp/step"), rec)
    assert p == Path("/tmp/step/step_005_42.png")


def test_step_attachment_path_top_level():
    rec = {"id": 99, "name": "top.png", "step_index": None}
    p = extractor._step_attachment_path(Path("/tmp/step"), rec)
    assert p == Path("/tmp/step/top_99.png")


def test_step_attachment_path_sanitizes_spaces_and_specials():
    rec = {"id": 1, "name": "../My Screenshot 日本語: 2.49.09 PM.PNG",
           "step_index": 2}
    p = extractor._step_attachment_path(Path("/tmp/step"), rec)
    assert p == Path("/tmp/step/step_002_1.png")




def test_alert_operator_writes_to_stderr(capsys):
    extractor._alert_operator("status-map fetch returned empty for TEST-E1")
    captured = capsys.readouterr()
    assert "status-map fetch returned empty for TEST-E1" in captured.err
    assert captured.err.startswith("\n[ARGUS] ALERT:")


def test_alert_operator_bypasses_default_log_silencing(capsys, caplog):
    import logging
    extractor.configure_logging(verbose=False)
    logging.getLogger("argus").setLevel(logging.ERROR)

    extractor._alert_operator("must be visible even at ERROR level")
    captured = capsys.readouterr()
    assert "must be visible even at ERROR level" in captured.err




class _FakeHeaders(dict):
    """requests.Response.headers-like dict that preserves dict semantics."""


class _FakeResponse:
    """Minimal stand-in for requests.Response covering the surface fetch_test_results + download_attachment touch."""

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

    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _FakeSession:
    """Canned-response session."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers = {}

    def get(self, url, params=None, stream=False, timeout=None):
        self.calls.append({"url": url, "params": params, "stream": stream,
                           "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeSession ran out of scripted responses")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_fetch_test_results_retries_on_429(monkeypatch):
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
    assert result == [{"id": 1, "steps": []}]
    assert len(session.calls) == 3


def test_fetch_test_results_raises_after_max_attempts(monkeypatch):
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


@pytest.mark.parametrize("attachment", [False, True])
def test_zero_retry_after_keeps_exponential_backoff(
        tmp_path, monkeypatch, attachment):
    import time as real_time
    sleeps = []
    monkeypatch.setattr(real_time, "sleep", sleeps.append)
    session = _FakeSession([
        _FakeResponse(status_code=429, headers={"Retry-After": "0"}),
        _FakeResponse(status_code=429, headers={"Retry-After": "0"}),
        _FakeResponse(status_code=429, headers={"Retry-After": "10"}),
        _FakeResponse(status_code=200, json_body={"id": 1}, body_bytes=b"ok"),
    ])
    if attachment:
        dest = tmp_path / "out.pdf"
        extractor.download_attachment(session, "https://jira.test", 42, dest)
        assert dest.read_bytes() == b"ok"
    else:
        cfg = extractor.ExtractorConfig(
            out_dir=tmp_path, execution_key="QA-E1",
        )
        assert extractor.fetch_test_results(session, cfg) == [{"id": 1}]
    assert sleeps == [1.0, 2.0, 10.0]
    assert len(session.calls) == 4


def test_fetch_test_results_retries_on_read_timeout(monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    import requests
    session = _FakeSession([
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectionError("reset by peer"),
        _FakeResponse(status_code=200, json_body={"key": "QA-E1"}),
    ])
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/unused"),
        execution_key="QA-E1",
    )
    results = extractor.fetch_test_results(session, cfg)
    assert results == [{"key": "QA-E1"}]
    assert len(session.calls) == 3


def test_fetch_test_results_raises_after_persistent_timeouts(monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    import requests
    session = _FakeSession([
        requests.exceptions.ReadTimeout("read timed out") for _ in range(6)
    ])
    cfg = extractor.ExtractorConfig(
        out_dir=Path("/tmp/unused"),
        execution_key="QA-E1",
    )
    with pytest.raises(requests.exceptions.ReadTimeout):
        extractor.fetch_test_results(session, cfg)
    assert len(session.calls) == 6


def test_download_attachment_retries_on_429(tmp_path: Path, monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    payload = b"fake-pdf-bytes"
    session = _FakeSession([
        _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
        _FakeResponse(status_code=200, body_bytes=payload),
    ])
    dest = tmp_path / "out.pdf"
    extractor.download_attachment(session, "https://jira.test", 42, dest)
    assert dest.read_bytes() == payload
    assert len(session.calls) == 2


def test_download_attachment_surfaces_terminal_429(
        tmp_path: Path, monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    session = _FakeSession([_FakeResponse(status_code=429) for _ in range(6)])
    dest = tmp_path / "out.pdf"
    import requests
    with pytest.raises(requests.HTTPError):
        extractor.download_attachment(session, "https://jira.test", 42, dest)


class _MidStreamFailResponse(_FakeResponse):
    def iter_content(self, chunk_size=None):
        yield b"first-64k-chunk"
        raise requests.exceptions.ChunkedEncodingError("connection reset")


def test_download_attachment_removes_truncated_file_on_stream_failure(
        tmp_path: Path):
    session = _FakeSession([_MidStreamFailResponse(status_code=200)])
    dest = tmp_path / "step_3_img.png"
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        extractor.download_attachment(session, "https://jira.test", 42, dest)
    assert not dest.exists()


def test_download_attachment_keeps_complete_file(tmp_path: Path):
    payload = b"complete-image-bytes"
    session = _FakeSession([_FakeResponse(status_code=200,
                                          body_bytes=payload)])
    dest = tmp_path / "ok.png"
    extractor.download_attachment(session, "https://jira.test", 42, dest)
    assert dest.read_bytes() == payload


def test_download_attachment_retries_transport_errors(tmp_path: Path,
                                                      monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    payload = b"pdf-after-retry"
    session = _FakeSession([
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectionError("reset by peer"),
        _FakeResponse(status_code=200, body_bytes=payload),
    ])
    dest = tmp_path / "out.pdf"
    extractor.download_attachment(session, "https://jira.test", 42, dest)
    assert dest.read_bytes() == payload
    assert len(session.calls) == 3


def test_download_attachment_transport_error_exhausts_and_raises(
        tmp_path: Path, monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    session = _FakeSession([
        requests.exceptions.ReadTimeout("t") for _ in range(6)])
    dest = tmp_path / "out.pdf"
    with pytest.raises(requests.exceptions.ReadTimeout):
        extractor.download_attachment(session, "https://jira.test", 42, dest)
    assert not dest.exists()
    assert len(session.calls) == 6


def test_get_project_status_ids_retries_on_429(monkeypatch):
    import time as real_time
    monkeypatch.setattr(real_time, "sleep", lambda s: None)

    body = [{"i18nKey": "TEST_RESULT.STATUS.PASS", "id": 39},
            {"i18nKey": "TEST_RESULT.STATUS.FAIL", "id": 40}]
    session = _FakeSession([
        _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
        _FakeResponse(status_code=200, json_body=body),
    ])
    out = extractor.get_project_status_ids(session, "https://jira.test", "1")
    assert out == {"PASS": 39, "FAIL": 40}
    assert len(session.calls) == 2




def _write_solid_png(path: Path, width: int, height: int) -> None:
    """Write a solid PNG fixture with PyMuPDF."""
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height))
    pix.clear_with(200)
    pix.save(str(path))


def _write_png_with_dpi(path: Path, width: int, height: int, dpi: int) -> None:
    """Write a grayscale PNG with a specific DPI in its pHYs chunk."""
    import struct
    import zlib

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    px_per_meter = int(round(dpi / 0.0254))
    phys = struct.pack(">IIB", px_per_meter, px_per_meter, 1)
    raw_row = b"\x00" + b"\x80" * width
    idat = zlib.compress(raw_row * height)
    path.write_bytes(
        sig + _chunk(b"IHDR", ihdr) + _chunk(b"pHYs", phys)
        + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def test_resize_if_oversized_shrinks_oversized_image(tmp_path: Path):
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
    assert short_edge == pytest.approx(long_edge * 2 / 3, rel=0.02)


def test_resize_if_oversized_passes_through_small_image(tmp_path: Path):
    """A 1000×800 PNG is already within the cap — no resize, no rewrite."""
    path = tmp_path / "small.png"
    _write_solid_png(path, 1000, 800)
    before_mtime = path.stat().st_mtime_ns

    changed = extractor._resize_if_oversized(path)
    assert changed is False
    assert path.stat().st_mtime_ns == before_mtime


def test_resize_if_oversized_fails_open_on_corrupt_file(tmp_path: Path):
    path = tmp_path / "corrupt.png"
    path.write_bytes(b"not actually a png")
    before = path.read_bytes()

    changed = extractor._resize_if_oversized(path)
    assert changed is False
    assert path.read_bytes() == before


def test_resize_if_oversized_respects_custom_cap(tmp_path: Path):
    path = tmp_path / "medium.png"
    _write_solid_png(path, 1200, 900)

    assert extractor._resize_if_oversized(path) is False

    assert extractor._resize_if_oversized(path, max_edge=500) is True
    import fitz
    with fitz.open(str(path)) as doc:
        assert max(doc[0].rect.width, doc[0].rect.height) <= 500


def test_resize_if_oversized_uses_pixel_dimensions_not_points(tmp_path: Path):
    import fitz
    path = tmp_path / "retina.png"
    _write_png_with_dpi(path, 2370, 474, dpi=144)

    pix = fitz.Pixmap(str(path))
    assert pix.width == 2370
    assert pix.height == 474

    changed = extractor._resize_if_oversized(path)
    assert changed is True, (
        "oversized-by-pixels image must be resized even when "
        "fitz.open().rect would report it as under-cap (points)"
    )

    pix2 = fitz.Pixmap(str(path))
    assert max(pix2.width, pix2.height) <= extractor._MAX_STEP_IMAGE_EDGE_PX


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
        n = "Additional Non Automated cases - GMA - AU (cloned) (cloned)"
        assert extractor.parse_surface_from_testrun_name(n) is None
        assert extractor.parse_browser_from_testrun_name(n) is None
        assert extractor.parse_device_from_testrun_name(n) is None

    def test_empty_and_none(self):
        for n in (None, "", "   "):
            assert extractor.parse_surface_from_testrun_name(n) is None
            assert extractor.parse_browser_from_testrun_name(n) is None
            assert extractor.parse_device_from_testrun_name(n) is None


def test_get_testresult_summary_includes_execution_date():
    payload = {
        "id": 1,
        "key": "QA-E1",
        "userKey": "JIRAUSER1",
        "executionDate": "2026-05-27T10:00:00Z",
        "attachments": [{"id": 77, "name": "proof.pdf", "fileSize": 123}],
    }
    session = _FakeSession([_FakeResponse(status_code=200, json_body=payload)])

    result = extractor.get_testresult_summary(session, "https://jira.test", 42)

    assert result == {
        "key": "QA-E1",
        "user_key": "JIRAUSER1",
        "execution_date": "2026-05-27T10:00:00Z",
        "evidence_fingerprint": extractor.build_evidence_fingerprint([payload]),
        "evidence_fingerprint_legacy": extractor.legacy_evidence_fingerprint([payload]),
    }
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://jira.test/rest/tests/1.0/testresult/42"
    fields = call["params"]["fields"]
    assert "executionDate" in fields
    assert "attachments" in fields
    assert "testScriptResults" in fields
    assert "testResultStatusId" in fields
    assert "traceLinks" in fields
    assert "comment" in fields
    assert "id" in fields
    assert "key" in fields
    assert "userKey" in fields


def test_get_testresult_summary_handles_missing_execution_date():
    payload = {"id": 1, "key": "QA-E1", "userKey": "JIRAUSER1"}
    session = _FakeSession([_FakeResponse(status_code=200, json_body=payload)])

    result = extractor.get_testresult_summary(session, "https://jira.test", 7)

    assert result == {
        "key": "QA-E1",
        "user_key": "JIRAUSER1",
        "execution_date": None,
        "evidence_fingerprint": extractor.build_evidence_fingerprint([payload]),
        "evidence_fingerprint_legacy": extractor.legacy_evidence_fingerprint([payload]),
    }


def test_get_testresult_summary_returns_none_on_http_error():
    session = _FakeSession([_FakeResponse(status_code=404)])
    result = extractor.get_testresult_summary(session, "https://jira.test", 99)
    assert result is None


def test_build_coverage_items_returns_executed_and_unexecuted():
    runs = [
        {"id": 100, "key": "QA-C1",
         "name": "E2E Flow - GMA - US Desktop"},
    ]
    items_by_run = {
        100: [
            {"id": 1, "$lastTestResult": {
                "id": 11, "testResultStatusId": 39,
                "testCase": {"key": "QA-T1"},
                "userKey": "JIRAUSER_A",
            }},
            {"id": 2, "$lastTestResult": {
                "id": 12, "testResultStatusId": 41,
                "testCase": {"key": "QA-T2"},
                "userKey": "JIRAUSER_B",
            }},
            {"id": 3, "$lastTestResult": {
                "id": 13, "testResultStatusId": 37,
                "testCase": {"key": "QA-T3"},
                "userKey": "JIRAUSER_C",
            }},
        ],
    }
    status_map = {39: "Pass", 40: "Fail", 41: "Blocked",
                  37: "Not Executed"}

    class _NoHTTPSession:
        def get(self, *a, **kw):
            raise AssertionError("no HTTP expected — items_by_run pre-fed")

    items = extractor.build_coverage_items(
        _NoHTTPSession(), "https://jira.test",
        runs, status_map,
        user_cache={"JIRAUSER_A": "Alice", "JIRAUSER_B": "Bob",
                    "JIRAUSER_C": "Carol"},
        items_by_run=items_by_run,
    )
    assert len(items) == 3
    statuses = {it.status for it in items}
    assert statuses == {"Pass", "Blocked", "Not Executed"}
    assert [it.e_key for it in items] == [None, None, None]


def test_build_coverage_items_excludes_ignored_execution_statuses():
    runs = [{"id": 100, "key": "MOBILE-C1", "name": "iOS Regression"}]
    items_by_run = {
        100: [
            {"$lastTestResult": {
                "testResultStatusId": 39,
                "testCase": {"key": "MOBILE-T1"},
            }},
            {"$lastTestResult": {
                "testResultStatusId": 51,
                "testCase": {"key": "MOBILE-T2"},
            }},
            {"$lastTestResult": {
                "testResultStatusId": 52,
                "testCase": {"key": "MOBILE-T3"},
            }},
        ],
    }

    items = extractor.build_coverage_items(
        _FakeSession([]),
        "https://jira.test",
        runs,
        {39: "Pass", 51: "Automation Pass", 52: "Automation Fail"},
        items_by_run=items_by_run,
        ignored_status_ids={51, 52},
    )

    assert [item.test_case_key for item in items] == ["MOBILE-T1"]


def test_build_coverage_items_resolves_e_keys_only_when_opted_in(
    tmp_path,
    monkeypatch,
):
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
                "userKey": "JIRAUSER_A",
            }},
            {"id": 2, "$lastTestResult": {
                "id": 12, "testResultStatusId": 41,
                "testCase": {"key": "QA-T2"},
                "userKey": "JIRAUSER_B",
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
        session, "https://jira.test", runs, status_map,
        user_cache={"JIRAUSER_A": "Alice", "JIRAUSER_B": "Bob"},
        items_by_run=items_by_run,
        resolve_e_keys=True,
    )

    assert [it.e_key for it in items] == ["QA-E11", "QA-E12", None]
    assert [call["url"] for call in session.calls] == [
        "https://jira.test/rest/tests/1.0/testresult/11",
        "https://jira.test/rest/tests/1.0/testresult/12",
    ]
    assert all(call["params"] == {"fields": "id,key"}
               for call in session.calls)

    class _NoHTTPSession:
        def get(self, *a, **kw):
            raise AssertionError("warm E-key cache should avoid HTTP")

    cached_items = extractor.build_coverage_items(
        _NoHTTPSession(), "https://jira.test", runs, status_map,
        user_cache={"JIRAUSER_A": "Alice", "JIRAUSER_B": "Bob"},
        items_by_run=items_by_run,
        resolve_e_keys=True,
    )
    assert [it.e_key for it in cached_items] == [
        "QA-E11", "QA-E12", None,
    ]


def test_build_coverage_items_parses_marketplace_from_testrun_name():
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
        _NoHTTPSession(), "https://jira.test", runs, status_map,
        items_by_run=items_by_run,
    )
    by_run = {it.testrun_key: it for it in items}
    assert by_run["QA-C2"].marketplace == "BR"
    assert by_run["QA-C3"].marketplace == "JP"


@pytest.mark.parametrize(("name", "expected"), [
    ("[TABLET][OOBE][UK] Default is not set.", "UK"),
    ("(DE) OOBE smoke", "DE"),
    ("OOBE/US/desktop", "US"),
    ("region:AU pass", "AU"),
    ("Some INDIA test", None),
    ("OOBE regression", None),
])
def test_parse_marketplace_accepts_team_tag_delimiters(name, expected):
    assert extractor.parse_marketplace_from_testrun_name(name) == expected


def test_resolve_marketplace_uses_only_unambiguous_test_case_tag():
    assert extractor.resolve_marketplace(
        "TABLET OOBE regression",
        "[TABLET][OOBE][UK] Default is not set.",
    ) == "UK"
    assert extractor.resolve_marketplace(
        "Bookshelf Set B",
        "[IAP][UK,AU,CA,DE,IN] Podcast experience",
    ) is None
    assert extractor.resolve_marketplace(
        "HS Reg AU Desktop",
        "[IAP][UK,DE] Podcast experience",
    ) == "AU"


def test_expected_pdf_names_use_attachment_ids():
    got = extractor.expected_pdf_names([
        (111, "Session_A.pdf"),
        (222, "Session_A.pdf"),
        (333, "Session_B.pdf"),
    ])
    assert got == {
        "attachment_111.pdf",
        "attachment_222.pdf",
        "attachment_333.pdf",
    }


def _seed_evidence(work_dir: Path, pdf_name: str, pages: int = 2) -> None:
    (work_dir / pdf_name).write_bytes(b"%PDF-fake")
    sub = work_dir / "screenshots" / Path(pdf_name).stem
    sub.mkdir(parents=True, exist_ok=True)
    for i in range(1, pages + 1):
        (sub / f"page_{i:03d}.jpg").write_bytes(b"jpg")


def test_reconcile_removes_stale_pdf_and_pages(tmp_path: Path):
    work_dir = tmp_path / "E-1"
    work_dir.mkdir()
    _seed_evidence(work_dir, "Session_OLD.pdf")
    _seed_evidence(work_dir, "attachment_1.pdf")

    extractor._reconcile_stale_evidence(
        work_dir, [(1, "Session_NEW.pdf")])

    assert not (work_dir / "Session_OLD.pdf").exists()
    assert not (work_dir / "screenshots" / "Session_OLD").exists()
    assert (work_dir / "attachment_1.pdf").exists()
    assert (
        work_dir / "screenshots" / "attachment_1" / "page_001.jpg"
    ).exists()


def test_reconcile_removes_everything_when_no_attachments(tmp_path: Path):
    work_dir = tmp_path / "E-2"
    work_dir.mkdir()
    _seed_evidence(work_dir, "Session_OLD.pdf")

    extractor._reconcile_stale_evidence(work_dir, [])

    assert not (work_dir / "Session_OLD.pdf").exists()
    assert not (work_dir / "screenshots" / "Session_OLD").exists()


def test_reconcile_resets_step_attachments(tmp_path: Path):
    work_dir = tmp_path / "E-3"
    work_dir.mkdir()
    step_dir = work_dir / "screenshots" / "step_attachments"
    step_dir.mkdir(parents=True)
    (step_dir / "step_001_9_old.png").write_bytes(b"png")

    extractor._reconcile_stale_evidence(work_dir, [(1, "Session_NEW.pdf")])

    assert not step_dir.exists()


def test_reconcile_keeps_orphan_subdir_matching_expected(tmp_path: Path):
    work_dir = tmp_path / "E-4"
    work_dir.mkdir()
    sub = work_dir / "screenshots" / "attachment_1"
    sub.mkdir(parents=True)
    (sub / "page_001.jpg").write_bytes(b"jpg")

    extractor._reconcile_stale_evidence(work_dir, [(1, "Session_NEW.pdf")])

    assert (sub / "page_001.jpg").exists()



_NETSCAPE_COOKIE = (
    "# Netscape HTTP Cookie File\n"
    "jira.example.com\tFALSE\t/\tTRUE\t9999999999\tsession_token\tdeadbeef\n"
)


def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path




def test_build_session_uses_only_a_bearer_token(tmp_path: Path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_JIRA_PAT", "pat-123")

    session = extractor.build_session()

    assert session.headers["Authorization"] == "Bearer pat-123"
    assert len(session.cookies) == 0






def _redirect_hop(from_url: str, to_url: str):
    """Model one redirect hop as rebuild_auth() sees it."""
    old = requests.Request("GET", from_url).prepare()
    new = requests.Request(
        "GET", to_url, headers={"Authorization": "Bearer pat-123"}
    ).prepare()
    resp = requests.Response()
    resp.url = from_url
    resp.request = old
    return new, resp


_REDIRECT_CROSS_HOST_HOPS = [
    ("https://jira.example.com/rest/api/2/myself",
     "https://login.example.net/SSO/redirect"),
    ("https://login.example.net/SSO/redirect",
     "https://jira.example.com:443/rest/api/2/myself?id_token=x"),
]
_REDIRECT_SAME_HOST_HOP = [
    ("https://jira.example.com:443/rest/api/2/myself?id_token=x",
     "https://jira.example.com/rest/api/2/myself"),
]
_REDIRECT_CHAIN = _REDIRECT_CROSS_HOST_HOPS + _REDIRECT_SAME_HOST_HOP




@pytest.mark.parametrize("from_url,to_url", _REDIRECT_CROSS_HOST_HOPS)
def test_plain_requests_session_strips_authorization_on_cross_host_redirect(
    from_url: str, to_url: str
):
    req, resp = _redirect_hop(from_url, to_url)

    requests.Session().rebuild_auth(req, resp)

    assert "Authorization" not in req.headers


@pytest.mark.parametrize("to_url", [
    "https://evil.example.com/steal",
    "https://retail.example.com.evil.example.com/steal",
    "https://other.retail.example.com/internal",
])
def test_session_strips_authorization_on_untrusted_redirect(
    tmp_path: Path, monkeypatch, to_url: str
):
    """The PAT must NOT follow a redirect that changes the origin."""
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_JIRA_PAT", "pat-123")
    session = extractor.build_session()

    req, resp = _redirect_hop("https://jira.example.com/rest/api/2/myself", to_url)
    session.rebuild_auth(req, resp)

    assert "Authorization" not in req.headers




def test_load_jira_pat_refuses_world_readable_pat_file(tmp_path, monkeypatch):
    from argus.core import secret_files
    secret_files.reset_warned_cache()
    monkeypatch.delenv("ARGUS_JIRA_PAT", raising=False)
    monkeypatch.delenv("PROJECT_JIRA_PAT", raising=False)
    monkeypatch.chdir(tmp_path)
    pat_file = tmp_path / "jira.pat"
    pat_file.write_text("pat-secret-123")

    pat_file.chmod(0o644)
    assert extractor._load_jira_pat(pat_file=pat_file) is None

    secret_files.reset_warned_cache()
    pat_file.chmod(0o600)
    assert extractor._load_jira_pat(pat_file=pat_file) == "pat-secret-123"


def test_find_test_run_by_key_uses_quoted_exact_tql():
    session = _FakeSession([
        _FakeResponse(status_code=200, json_body={
            "results": [{
                "id": 39111,
                "key": "QA-C21462",
                "name": "Webview E2E -US",
                "projectId": 10000,
                "folderId": 18885,
            }]
        })
    ])

    run = extractor.find_test_run_by_key(
        session,
        "https://jira.example.com",
        "qa-c21462",
    )

    assert run is not None
    assert run["id"] == 39111
    assert session.calls[0]["params"]["query"] == (
        'testRun.key IN ("QA-C21462")'
    )
    assert session.calls[0]["params"]["archived"] == "false"


def test_find_test_run_by_key_rejects_tql_injection():
    with pytest.raises(ValueError, match="invalid Jira test-run key"):
        extractor.find_test_run_by_key(
            _FakeSession([]),
            "https://jira.example.com",
            'QA-C1") OR TRUE',
        )


def test_find_test_result_context_returns_parent_run_metadata():
    session = _FakeSession([
        _FakeResponse(status_code=200, json_body={
            "id": 99,
            "key": "QA-E123",
            "testRun": {
                "id": 39111,
                "key": "QA-C21462",
                "name": "Webview E2E -US",
                "projectId": 10000,
                "folderId": 18885,
            },
        })
    ])

    result = extractor.find_test_result_context(
        session,
        "https://jira.example.com",
        "qa-e123",
    )

    assert result is not None
    assert result["testRun"]["folderId"] == 18885
    assert session.calls[0]["url"].endswith(
        "/rest/tests/1.0/testresult/QA-E123"
    )
    assert "testRun(id,key,name,projectId,folderId)" in (
        session.calls[0]["params"]["fields"]
    )


def test_find_test_result_context_rejects_invalid_execution_key():
    with pytest.raises(ValueError, match="invalid Jira execution key"):
        extractor.find_test_result_context(
            _FakeSession([]),
            "https://jira.example.com",
            "QA-C1",
        )


def test_get_project_status_map_is_cached_per_project(monkeypatch):
    extractor.clear_status_map_cache()
    calls: list[str] = []

    class _Response:
        status_code = 200

        def json(self):
            return [{"id": 39, "name": "Pass"}, {"id": 40, "name": "Fail"}]

    def fake_get(session, url, **kwargs):
        calls.append(url)
        return _Response()

    monkeypatch.setattr(extractor, "_get_with_retry", fake_get)
    try:
        first = extractor.get_project_status_map(
            object(), "https://jira.example", 123)
        second = extractor.get_project_status_map(
            object(), "https://jira.example", "123")
        extractor.get_project_status_map(object(), "https://jira.example", 124)
        assert first == second == {39: "Pass", 40: "Fail"}
        assert len(calls) == 2  # project 123 fetched once, 124 once
        second[39] = "mutated"
        assert extractor.get_project_status_map(
            object(), "https://jira.example", 123)[39] == "Pass"
    finally:
        extractor.clear_status_map_cache()


def test_get_project_status_map_never_caches_empty_results(monkeypatch):
    extractor.clear_status_map_cache()
    calls: list[str] = []

    class _Failure:
        status_code = 503

        def json(self):
            return []

    def fake_get(session, url, **kwargs):
        calls.append(url)
        return _Failure()

    monkeypatch.setattr(extractor, "_get_with_retry", fake_get)
    try:
        for _ in range(2):
            assert extractor.get_project_status_map(
                object(), "https://jira.example", 7) == {}
        assert len(calls) == 2
    finally:
        extractor.clear_status_map_cache()


def test_run_uses_the_provided_session_instead_of_building_one(
        tmp_path: Path, monkeypatch):
    provided = object()
    seen: list[object] = []

    def fail_build(cfg):
        raise AssertionError("build_session must not be called")

    def fake_fetch(session, cfg):
        seen.append(session)
        raise extractor.AuthError("expired")

    monkeypatch.setattr(extractor, "build_session", fail_build)
    monkeypatch.setattr(extractor, "fetch_test_results", fake_fetch)
    monkeypatch.setattr(extractor, "_alert_operator", lambda message: None)

    cfg = extractor.ExtractorConfig(
        out_dir=tmp_path, execution_key="QA-E1", session=provided)
    assert extractor.run(cfg) == (2, None)
    assert seen == [provided]
