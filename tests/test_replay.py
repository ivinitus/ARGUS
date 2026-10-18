"""Tests for replay.py."""
from __future__ import annotations

import io
import json
import struct
import zlib
from pathlib import Path

import pytest

from argus.pipeline import auditor
from argus.pipeline import replay




def _make_tiny_png(path: Path) -> None:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw)
    path.write_bytes(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


class FakeModelClient:
    """Canned-response model API client."""

    def __init__(self, responses):
        if isinstance(responses, str):
            self._responses = [responses]
            self._cycle = True
        else:
            self._responses = list(responses)
            self._cycle = False
        self._i = 0
        self.calls: list[dict] = []

    def invoke_model(self, model_id, body):
        self.calls.append({"model_id": model_id, "body": body})
        if self._cycle:
            text = self._responses[0]
        else:
            text = self._responses[self._i]
            self._i += 1
        payload = {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        }
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def _audit_fixture(exec_dir: Path) -> None:
    """Seed metadata.json + one PNG page so auditor.run has something to audit."""
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")
    (exec_dir / "metadata.json").write_text(
        json.dumps({"test_results": [{"steps": []}]})
    )


def _system_text(body: dict) -> str:
    """Extract the system prompt text from a captured request body."""
    sys_field = body["system"]
    if isinstance(sys_field, list):
        return "".join(b.get("text", "") for b in sys_field)
    return sys_field


def _canned_audit(verdict: str = "pass", findings: list | None = None) -> str:
    return json.dumps({
        "overall_verdict": verdict,
        "summary": "test",
        "findings": findings or [],
    })


def test_resolve_variant_v1_returns_pinned_historical_prompt():
    from argus.pipeline import auditor
    assert replay._resolve_variant("v1") is auditor.SYSTEM_PROMPT_V1
    assert isinstance(replay._resolve_variant("v1"), str)
    assert replay._resolve_variant("v1")


def test_resolve_variant_returns_string_for_registered_custom_variant(monkeypatch):
    monkeypatch.setitem(replay.VARIANTS, "vtest", "CUSTOM PROMPT TEXT")
    assert replay._resolve_variant("vtest") == "CUSTOM PROMPT TEXT"


def test_resolve_variant_loads_packaged_team_prompt():
    assert "Portal WEBSITE EVIDENCE POLICY" in replay._resolve_variant("portal_v1")


def test_resolve_variant_raises_on_unknown_name():
    with pytest.raises(ValueError) as exc:
        replay._resolve_variant("v999")
    msg = str(exc.value)
    assert "v999" in msg
    assert "known variants" in msg


def test_find_execution_dir_flat_layout(tmp_path: Path):
    target = tmp_path / "QA-E1"
    target.mkdir()
    assert replay._find_execution_dir(tmp_path, "QA-E1") == target


def test_find_execution_dir_testrun_tester_layout(tmp_path: Path):
    target = tmp_path / "My_Testrun" / "alice" / "QA-E1"
    target.mkdir(parents=True)
    assert replay._find_execution_dir(tmp_path, "QA-E1") == target


def test_find_execution_dir_returns_none_when_missing(tmp_path: Path):
    assert replay._find_execution_dir(tmp_path, "QA-E-GHOST") is None


def test_read_keys_dedupes_preserving_order():
    assert replay._read_keys(None, ["K-1", "K-2", "K-1", "K-3"]) == \
           ["K-1", "K-2", "K-3"]


def test_read_keys_from_file_skips_blanks_and_comments(tmp_path: Path):
    p = tmp_path / "keys.txt"
    p.write_text("K-1\n\n# commented\nK-2\n  K-1  \n")
    assert replay._read_keys(p, []) == ["K-1", "K-2"]


def test_read_keys_strips_inline_trailing_comments(tmp_path: Path):
    p = tmp_path / "keys.txt"
    p.write_text(
        "K-1  # first key — has annotation\n"
        "K-2\t# tab-separated annotation\n"
        "K-3#no-space-before-hash\n"
    )
    assert replay._read_keys(p, []) == ["K-1", "K-2", "K-3"]


def test_read_keys_positional_wins_over_file(tmp_path: Path):
    p = tmp_path / "keys.txt"
    p.write_text("FILE-K\n")
    assert replay._read_keys(p, ["POS-K"]) == ["POS-K"]


def test_read_keys_empty_returns_empty():
    assert replay._read_keys(None, []) == []


def test_replay_writes_to_variant_subdir_only(tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    canonical = exec_dir / "audit.json"
    canonical.write_text(json.dumps({"overall_verdict": "pass",
                                     "summary": "canonical",
                                     "findings": []}))
    canonical_bytes_before = canonical.read_bytes()

    fake = FakeModelClient(_canned_audit("concerns",
                                           [{"severity": "medium",
                                             "page": 1, "step_index": 1,
                                             "description": "x"}]))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    rc = replay.replay_audit(exec_dir, "v1")
    assert rc == 0
    replay_json = exec_dir / "_replay" / "v1" / "audit.json"
    assert replay_json.exists()
    replay_audit = json.loads(replay_json.read_text())
    assert replay_audit["overall_verdict"] == "concerns"
    assert canonical.read_bytes() == canonical_bytes_before


def test_replay_writes_canonical_audit_md_alongside(tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    fake = FakeModelClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "v1")
    replay_md = exec_dir / "_replay" / "v1" / "audit.md"
    assert replay_md.exists()
    assert "Audit Report" in replay_md.read_text()


def test_replay_injects_custom_prompt_into_model_body(
        tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    marker = "SENTINEL_VARIANT_MARKER_XYZZY"
    monkeypatch.setitem(replay.VARIANTS, "vtest", f"SYSTEM PROMPT {marker}")

    fake = FakeModelClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "vtest")
    system_text = _system_text(fake.calls[0]["body"])
    assert marker in system_text


def test_replay_v1_sends_pinned_historical_prompt(tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    fake = FakeModelClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "v1")
    body_system = _system_text(fake.calls[0]["body"])
    assert body_system == auditor.SYSTEM_PROMPT_V1
    assert body_system != auditor.SYSTEM_PROMPT_V4


def test_replay_unknown_variant_raises(tmp_path: Path):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    with pytest.raises(ValueError):
        replay.replay_audit(exec_dir, "v999-unknown")


def test_replay_overwrites_previous_replay_of_same_variant(
        tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)

    fake = FakeModelClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)
    replay.replay_audit(exec_dir, "v1")
    first = json.loads((exec_dir / "_replay" / "v1" / "audit.json")
                       .read_text())
    assert first["overall_verdict"] == "pass"

    fake2 = FakeModelClient(_canned_audit("concerns",
                                            [{"severity": "medium",
                                              "page": 1, "step_index": 1,
                                              "description": "y"}]))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake2)
    replay.replay_audit(exec_dir, "v1")
    second = json.loads((exec_dir / "_replay" / "v1" / "audit.json")
                        .read_text())
    assert second["overall_verdict"] == "concerns"


def test_replay_two_variants_side_by_side(tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    monkeypatch.setitem(replay.VARIANTS, "v2", "alternate prompt")

    fake1 = FakeModelClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake1)
    replay.replay_audit(exec_dir, "v1")
    fake2 = FakeModelClient(_canned_audit("fail",
                                            [{"severity": "high",
                                              "page": 1, "step_index": 1,
                                              "description": "contradiction"}]))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake2)
    replay.replay_audit(exec_dir, "v2")

    v1 = json.loads((exec_dir / "_replay" / "v1" / "audit.json").read_text())
    v2 = json.loads((exec_dir / "_replay" / "v2" / "audit.json").read_text())
    assert v1["overall_verdict"] == "pass"
    assert v2["overall_verdict"] == "fail"


def test_replay_stamps_variant_name_into_audit_json(
        tmp_path: Path, monkeypatch):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    monkeypatch.setitem(replay.VARIANTS, "vxyz", "custom prompt")

    fake = FakeModelClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "vxyz")
    out = json.loads((exec_dir / "_replay" / "vxyz" / "audit.json")
                     .read_text())
    assert out["variant_name"] == "vxyz"
    assert out["schema_version"] == auditor.AUDIT_SCHEMA_VERSION


def test_finding_signature_preserves_problem_and_page_identity():
    a = {"severity": "high", "step_index": 3, "page": 5,
         "description": "long desc 1"}
    b = {"severity": "high", "step_index": 3, "page": 99,
         "description": "different desc"}
    assert replay._finding_signature(a) != replay._finding_signature(b)


def test_finding_signature_distinguishes_step_and_severity():
    a = {"severity": "high", "step_index": 3}
    b = {"severity": "medium", "step_index": 3}
    c = {"severity": "high", "step_index": 4}
    assert replay._finding_signature(a) != replay._finding_signature(b)
    assert replay._finding_signature(a) != replay._finding_signature(c)


def test_finding_signature_falls_back_to_page_then_desc():
    page_anchored = {"severity": "low", "step_index": None, "page": 7,
                     "description": "whatever"}
    anon = {"severity": "low", "step_index": None, "page": None,
            "description": "only desc matters here " * 5}
    assert "page:7" in replay._finding_signature(page_anchored)
    assert "anon:" in replay._finding_signature(anon)
    assert len(replay._finding_signature(anon)) < 100


def test_sev_counts_zero_when_audit_missing():
    assert replay._sev_counts(None) == {"high": 0, "medium": 0, "low": 0}


def test_sev_counts_counts_by_severity():
    audit = {
        "findings": [
            {"severity": "high"}, {"severity": "high"},
            {"severity": "medium"}, {"severity": "low"},
            {"severity": "unknown-value"},
        ]
    }
    assert replay._sev_counts(audit) == {"high": 2, "medium": 1, "low": 1}


def test_sev_counts_no_findings_key():
    assert replay._sev_counts({}) == {"high": 0, "medium": 0, "low": 0}


def _seed_replay(exec_dir: Path, variant: str, audit: dict) -> None:
    d = exec_dir / "_replay" / variant
    d.mkdir(parents=True, exist_ok=True)
    (d / "audit.json").write_text(json.dumps(audit))


def test_compute_diff_detects_verdict_change(tmp_path: Path):
    ex = tmp_path / "K"
    ex.mkdir()
    _seed_replay(ex, "v1", {"overall_verdict": "concerns",
                            "findings": [{"severity": "medium",
                                          "step_index": 1}]})
    _seed_replay(ex, "v2", {"overall_verdict": "fail",
                            "findings": [{"severity": "high",
                                          "step_index": 1}]})
    d = replay.compute_diff(ex, "v1", "v2")
    assert d.key == "K"
    assert d.verdict_a == "concerns"
    assert d.verdict_b == "fail"
    assert d.sev_a == {"high": 0, "medium": 1, "low": 0}
    assert d.sev_b == {"high": 1, "medium": 0, "low": 0}


def test_compute_diff_identifies_new_and_removed_findings(tmp_path: Path):
    ex = tmp_path / "K"
    ex.mkdir()
    _seed_replay(ex, "v1", {"overall_verdict": "concerns", "findings": [
        {"severity": "medium", "step_index": 1},
        {"severity": "low", "step_index": 5},
    ]})
    _seed_replay(ex, "v2", {"overall_verdict": "fail", "findings": [
        {"severity": "medium", "step_index": 1},
        {"severity": "high", "step_index": 9},
    ]})
    d = replay.compute_diff(ex, "v1", "v2")
    assert any("step:5" in s for s in d.only_in_a)
    assert any("step:9" in s for s in d.only_in_b)
    assert not any("step:1" in s for s in d.only_in_a)
    assert not any("step:1" in s for s in d.only_in_b)


def test_compute_diff_handles_missing_variant(tmp_path: Path):
    ex = tmp_path / "K"
    ex.mkdir()
    _seed_replay(ex, "v1", {"overall_verdict": "pass", "findings": []})
    d = replay.compute_diff(ex, "v1", "v2")
    assert d.verdict_a == "pass"
    assert d.verdict_b is None
    assert d.sev_b == {"high": 0, "medium": 0, "low": 0}


def test_render_compare_counts_verdict_transitions(tmp_path: Path):
    diffs = [
        replay.PerKeyDiff(key="A", verdict_a="concerns", verdict_b="fail",
                          sev_a={"high": 0, "medium": 1, "low": 0},
                          sev_b={"high": 1, "medium": 0, "low": 0},
                          only_in_a=[], only_in_b=["high|step:1"]),
        replay.PerKeyDiff(key="B", verdict_a="concerns", verdict_b="fail",
                          sev_a={"high": 0, "medium": 2, "low": 0},
                          sev_b={"high": 1, "medium": 1, "low": 0},
                          only_in_a=[], only_in_b=["high|step:2"]),
        replay.PerKeyDiff(key="C", verdict_a="pass", verdict_b="pass",
                          sev_a={"high": 0, "medium": 0, "low": 0},
                          sev_b={"high": 0, "medium": 0, "low": 0},
                          only_in_a=[], only_in_b=[]),
    ]
    md = replay.render_compare_report(diffs, "v1", "v2")
    assert "## Verdict transitions" in md
    assert "| concerns | fail | 2 |" in md
    assert "| pass | pass | 1 |" in md
    assert "promotions" in md.lower()
    assert "2" in md


def test_render_compare_lists_per_key_signatures(tmp_path: Path):
    diffs = [
        replay.PerKeyDiff(key="K", verdict_a="concerns", verdict_b="fail",
                          sev_a={"high": 0, "medium": 1, "low": 0},
                          sev_b={"high": 1, "medium": 0, "low": 0},
                          only_in_a=["medium|step:1"],
                          only_in_b=["high|step:2"]),
    ]
    md = replay.render_compare_report(diffs, "v1", "v2")
    assert "### K" in md
    assert "medium|step:1" in md
    assert "high|step:2" in md


def test_cli_replay_requires_variant(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        replay.main(["replay", "K-1"])
    assert exc.value.code == 2


def test_cli_replay_empty_keys_returns_error(
        tmp_path: Path, monkeypatch, capsys):
    cfg_path = tmp_path / "empty.toml"
    cfg_path.write_text("")
    rc = replay.main(["replay", "--variant", "v1",
                      "--config", str(cfg_path)])
    assert rc == 1
    assert "no keys" in capsys.readouterr().err.lower()


def test_cli_replay_unknown_variant_returns_error(tmp_path: Path, capsys):
    cfg_path = tmp_path / "empty.toml"
    cfg_path.write_text("")
    rc = replay.main(["replay", "--variant", "nope",
                      "--config", str(cfg_path), "K-1"])
    assert rc == 1
    assert "unknown variant" in capsys.readouterr().err.lower()


def test_cli_replay_missing_key_prints_skipped(tmp_path: Path, monkeypatch,
                                               capsys):
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(
        f'[extractor]\nout_dir = "{tmp_path}"\n'
    )
    rc = replay.main([
        "replay", "--variant", "v1", "--config", str(cfg_path), "GHOST-KEY",
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "SKIPPED" in err
    assert "GHOST-KEY" in err


def test_cli_compare_writes_replay_compare_md(tmp_path: Path, capsys):
    ex = tmp_path / "K-1"
    ex.mkdir()
    _seed_replay(ex, "v1", {"overall_verdict": "concerns",
                            "findings": [{"severity": "medium",
                                          "step_index": 1}]})
    _seed_replay(ex, "v2", {"overall_verdict": "fail",
                            "findings": [{"severity": "high",
                                          "step_index": 1}]})
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(f'[extractor]\nout_dir = "{tmp_path}"\n')
    rc = replay.main(["compare", "v1", "v2", "K-1",
                      "--config", str(cfg_path)])
    assert rc == 0
    compare_path = tmp_path / "_replay_compare.md"
    assert compare_path.exists()
    md = compare_path.read_text()
    assert "Replay comparison: v1 vs v2" in md
    assert "### K-1" in md


def test_cli_compare_stdout_flag_echoes_report(tmp_path: Path, capsys):
    ex = tmp_path / "K-1"
    ex.mkdir()
    _seed_replay(ex, "v1", {"overall_verdict": "pass", "findings": []})
    _seed_replay(ex, "v2", {"overall_verdict": "pass", "findings": []})
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(f'[extractor]\nout_dir = "{tmp_path}"\n')
    rc = replay.main(["compare", "v1", "v2", "K-1",
                      "--config", str(cfg_path),
                      "--stdout", "--no-report-file"])
    assert rc == 0
    assert not (tmp_path / "_replay_compare.md").exists()
    assert "Replay comparison" in capsys.readouterr().out
