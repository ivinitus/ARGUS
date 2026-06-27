"""Tests for replay.py.

No network, no boto3 at runtime — the audit pipeline is monkey-patched
with the FakeLLMRuntimeClient pattern from test_audit_integration.py so
replay logic (output routing, variant resolution, key-list handling,
diff computation) can be verified without a real LLM Runtime call.
"""
from __future__ import annotations

import io
import json
import struct
import zlib
from pathlib import Path

import pytest

import auditor
import config as argus_config
import replay


@pytest.fixture(autouse=True)
def _null_cloud_profile(monkeypatch):
    """Force replay-tests to run with cloud_profile=None.

    Production config.toml sets cloud_profile to a real ada-managed
    profile; when present, auditor.run constructs a boto3.Session
    instead of calling boto3.client directly, which bypasses the
    `monkeypatch.setattr(auditor.boto3, "client", ...)` pattern these
    tests rely on. Patching argus_config.load to return a settings
    object with cloud_profile=None keeps the existing test wiring intact
    without forcing every test to thread an explicit settings argument.
    """
    real_load = argus_config.load

    def _load_with_null_profile(*args, **kwargs):
        s = real_load(*args, **kwargs)
        s.auditor.cloud_profile = None
        return s

    monkeypatch.setattr(argus_config, "load", _load_with_null_profile)
    monkeypatch.setattr(replay.argus_config, "load", _load_with_null_profile)


# ---------------------------------------------------------------------------
# Local copies of test fixtures from test_audit_integration.py.
# Duplicated deliberately so this test file stays self-contained and
# can be run in isolation (pytest tests/test_replay.py) without a
# cross-test-file import that pytest's collection rules don't love.
# ---------------------------------------------------------------------------
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


class FakeLLMRuntimeClient:
    """Canned-response LLM Runtime client. Accepts a single string or a list.

    Tracks every call so tests can inspect the system prompt that was
    actually sent — critical for verifying variants reach invoke_model.
    """

    def __init__(self, responses):
        if isinstance(responses, str):
            self._responses = [responses]
            self._cycle = True
        else:
            self._responses = list(responses)
            self._cycle = False
        self._i = 0
        self.calls: list[dict] = []

    def invoke_model(self, modelId, body, contentType, accept):  # noqa: N803
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
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


def _canned_audit(verdict: str = "pass", findings: list | None = None) -> str:
    return json.dumps({
        "overall_verdict": verdict,
        "summary": "test",
        "findings": findings or [],
    })


# ---------------------------------------------------------------------------
# Variant resolution
# ---------------------------------------------------------------------------
def test_resolve_variant_v1_returns_pinned_historical_prompt():
    # v1 is a historical snapshot of the pre-2026-05-09 canonical
    # prompt. It MUST NOT drift when auditor.SYSTEM_PROMPT is
    # retargeted (v1 was previously registered as None → whatever
    # SYSTEM_PROMPT happened to be; that sentinel pattern broke when
    # V4 got promoted). The registered value must be the explicit
    # SYSTEM_PROMPT_V1 constant so `replay compare v1 v4` stays
    # truthful regardless of future promotions.
    import auditor
    assert replay._resolve_variant("v1") is auditor.SYSTEM_PROMPT_V1
    # Guardrail against a future "who needs V1 history anyway" PR:
    # v1 must be a non-empty string, not None.
    assert isinstance(replay._resolve_variant("v1"), str)
    assert replay._resolve_variant("v1")


def test_resolve_variant_returns_string_for_registered_custom_variant(monkeypatch):
    monkeypatch.setitem(replay.VARIANTS, "vtest", "CUSTOM PROMPT TEXT")
    assert replay._resolve_variant("vtest") == "CUSTOM PROMPT TEXT"


def test_resolve_variant_raises_on_unknown_name():
    with pytest.raises(ValueError) as exc:
        replay._resolve_variant("v999")
    msg = str(exc.value)
    assert "v999" in msg
    assert "known variants" in msg


# ---------------------------------------------------------------------------
# Execution dir discovery
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Key-list helpers
# ---------------------------------------------------------------------------
def test_read_keys_dedupes_preserving_order():
    # Positional args path.
    assert replay._read_keys(None, ["K-1", "K-2", "K-1", "K-3"]) == \
           ["K-1", "K-2", "K-3"]


def test_read_keys_from_file_skips_blanks_and_comments(tmp_path: Path):
    p = tmp_path / "keys.txt"
    p.write_text("K-1\n\n# commented\nK-2\n  K-1  \n")
    assert replay._read_keys(p, []) == ["K-1", "K-2"]


def test_read_keys_strips_inline_trailing_comments(tmp_path: Path):
    # Sample files are self-documenting with `KEY  # description` — the
    # reader must strip the trailing # comment so the KEY resolves
    # correctly. Regression: before this was added, inline-annotated
    # sample lines looked up keys like "QA-E174988  # UK/US..."
    # which would never match an execution dir.
    p = tmp_path / "keys.txt"
    p.write_text(
        "K-1  # first key — has annotation\n"
        "K-2\t# tab-separated annotation\n"
        "K-3#no-space-before-hash\n"
    )
    assert replay._read_keys(p, []) == ["K-1", "K-2", "K-3"]


def test_read_keys_positional_wins_over_file(tmp_path: Path):
    # When both are provided, positional keys are used. Guards against
    # a change that would silently merge and produce a surprising set.
    p = tmp_path / "keys.txt"
    p.write_text("FILE-K\n")
    assert replay._read_keys(p, ["POS-K"]) == ["POS-K"]


def test_read_keys_empty_returns_empty():
    assert replay._read_keys(None, []) == []


# ---------------------------------------------------------------------------
# replay_audit core: routes outputs to _replay/<variant>/ without touching canonical
# ---------------------------------------------------------------------------
def test_replay_writes_to_variant_subdir_only(tmp_path: Path, monkeypatch):
    """Verify the guarantee: canonical audit.json is never created or
    overwritten by a replay."""
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    # Seed a canonical audit.json that MUST remain untouched.
    canonical = exec_dir / "audit.json"
    canonical.write_text(json.dumps({"overall_verdict": "pass",
                                     "summary": "canonical",
                                     "findings": []}))
    canonical_bytes_before = canonical.read_bytes()

    fake = FakeLLMRuntimeClient(_canned_audit("concerns",
                                           [{"severity": "medium",
                                             "page": 1, "step_index": 1,
                                             "description": "x"}]))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    rc = replay.replay_audit(exec_dir, "v1")
    assert rc == 0
    # Replay subdir exists with its own audit.json.
    replay_json = exec_dir / "_replay" / "v1" / "audit.json"
    assert replay_json.exists()
    replay_audit = json.loads(replay_json.read_text())
    assert replay_audit["overall_verdict"] == "concerns"
    # Canonical was not modified.
    assert canonical.read_bytes() == canonical_bytes_before


def test_replay_writes_canonical_audit_md_alongside(tmp_path: Path, monkeypatch):
    """audit.md is routed to the replay subdir too, so the replay dir is
    self-contained (human-readable report + machine-readable json)."""
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    fake = FakeLLMRuntimeClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "v1")
    replay_md = exec_dir / "_replay" / "v1" / "audit.md"
    assert replay_md.exists()
    assert "Audit Report" in replay_md.read_text()


def test_replay_injects_custom_prompt_into_llm_runtime_body(
        tmp_path: Path, monkeypatch):
    """A registered custom variant's prompt text must actually reach
    invoke_model — the whole point of the replay infrastructure is that
    different variants genuinely send different system prompts."""
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    marker = "SENTINEL_VARIANT_MARKER_XYZZY"
    monkeypatch.setitem(replay.VARIANTS, "vtest", f"SYSTEM PROMPT {marker}")

    fake = FakeLLMRuntimeClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "vtest")
    # Body captured by FakeLLMRuntimeClient carries the system prompt.
    system_text = fake.calls[0]["body"]["system"]
    assert marker in system_text


def test_replay_v1_sends_pinned_historical_prompt(tmp_path: Path, monkeypatch):
    """v1 is the pre-promotion canonical baseline. It MUST send the
    exact SYSTEM_PROMPT_V1 string — NOT whatever SYSTEM_PROMPT currently
    points at — so `replay compare v1 v4` measures the intended
    V1-vs-V4 delta, not "canonical vs itself."

    Before V4 promotion, SYSTEM_PROMPT == SYSTEM_PROMPT_V1 and the
    previous form of this test (`body.system == auditor.SYSTEM_PROMPT`)
    passed. After promotion SYSTEM_PROMPT == SYSTEM_PROMPT_V4 and
    that assertion would silently start measuring V4-vs-V4.
    """
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    fake = FakeLLMRuntimeClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "v1")
    body_system = fake.calls[0]["body"]["system"]
    assert body_system == auditor.SYSTEM_PROMPT_V1
    # After V4 promotion these two are NOT the same. v1 must track V1,
    # not the moving canonical.
    assert body_system != auditor.SYSTEM_PROMPT_V4


def test_replay_unknown_variant_raises(tmp_path: Path):
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    with pytest.raises(ValueError):
        replay.replay_audit(exec_dir, "v999-unknown")


def test_replay_overwrites_previous_replay_of_same_variant(
        tmp_path: Path, monkeypatch):
    """Re-running a replay for the same variant replaces the previous
    output in-place — otherwise operators have to manually clean up
    between runs, and the 'last replay wins' invariant is broken."""
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)

    fake = FakeLLMRuntimeClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)
    replay.replay_audit(exec_dir, "v1")
    first = json.loads((exec_dir / "_replay" / "v1" / "audit.json")
                       .read_text())
    assert first["overall_verdict"] == "pass"

    # Re-run with a different canned response. Second response wins.
    fake2 = FakeLLMRuntimeClient(_canned_audit("concerns",
                                            [{"severity": "medium",
                                              "page": 1, "step_index": 1,
                                              "description": "y"}]))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake2)
    replay.replay_audit(exec_dir, "v1")
    second = json.loads((exec_dir / "_replay" / "v1" / "audit.json")
                        .read_text())
    assert second["overall_verdict"] == "concerns"


def test_replay_two_variants_side_by_side(tmp_path: Path, monkeypatch):
    """v1 and v2 produce separate subdirs; each variant's output is
    isolated from the other and both coexist under _replay/."""
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    monkeypatch.setitem(replay.VARIANTS, "v2", "alternate prompt")

    # v1 first.
    fake1 = FakeLLMRuntimeClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake1)
    replay.replay_audit(exec_dir, "v1")
    # v2 next — custom prompt, different canned verdict.
    fake2 = FakeLLMRuntimeClient(_canned_audit("fail",
                                            [{"severity": "high",
                                              "page": 1, "step_index": 1,
                                              "description": "contradiction"}]))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake2)
    replay.replay_audit(exec_dir, "v2")

    v1 = json.loads((exec_dir / "_replay" / "v1" / "audit.json").read_text())
    v2 = json.loads((exec_dir / "_replay" / "v2" / "audit.json").read_text())
    assert v1["overall_verdict"] == "pass"
    assert v2["overall_verdict"] == "fail"


def test_replay_stamps_variant_name_into_audit_json(
        tmp_path: Path, monkeypatch):
    """replay.replay_audit must pass the variant name through to
    AuditConfig.variant_name so the written audit.json self-describes
    its provenance. This is the contract report.py relies on to
    distinguish old-audit-which-variant-unknown vs new-audit-which-
    variant-labeled."""
    exec_dir = tmp_path / "QA-E1"
    _audit_fixture(exec_dir)
    monkeypatch.setitem(replay.VARIANTS, "vxyz", "custom prompt")

    fake = FakeLLMRuntimeClient(_canned_audit("pass"))
    monkeypatch.setattr(auditor.boto3, "client", lambda *a, **kw: fake)

    replay.replay_audit(exec_dir, "vxyz")
    out = json.loads((exec_dir / "_replay" / "vxyz" / "audit.json")
                     .read_text())
    assert out["variant_name"] == "vxyz"
    # And schema_version is stamped too, so downstream aggregations
    # can gate on both.
    assert out["schema_version"] == auditor.AUDIT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Compare: finding signature and sev counts
# ---------------------------------------------------------------------------
def test_finding_signature_step_anchor_wins_over_page():
    a = {"severity": "high", "step_index": 3, "page": 5,
         "description": "long desc 1"}
    b = {"severity": "high", "step_index": 3, "page": 99,
         "description": "different desc"}
    assert replay._finding_signature(a) == replay._finding_signature(b)


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
    # Description prefix is truncated to 60 chars so long desc doesn't
    # blow up the signature.
    assert len(replay._finding_signature(anon)) < 100


def test_sev_counts_zero_when_audit_missing():
    assert replay._sev_counts(None) == {"high": 0, "medium": 0, "low": 0}


def test_sev_counts_counts_by_severity():
    audit = {
        "findings": [
            {"severity": "high"}, {"severity": "high"},
            {"severity": "medium"}, {"severity": "low"},
            {"severity": "unknown-value"},  # bucketed nowhere
        ]
    }
    assert replay._sev_counts(audit) == {"high": 2, "medium": 1, "low": 1}


def test_sev_counts_no_findings_key():
    assert replay._sev_counts({}) == {"high": 0, "medium": 0, "low": 0}


# ---------------------------------------------------------------------------
# compute_diff + render
# ---------------------------------------------------------------------------
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
        {"severity": "medium", "step_index": 1},          # shared
        {"severity": "high", "step_index": 9},            # new in v2
    ]})
    d = replay.compute_diff(ex, "v1", "v2")
    # Shared signature (medium|step:1) must not appear in either diff.
    assert any("step:5" in s for s in d.only_in_a)
    assert any("step:9" in s for s in d.only_in_b)
    assert not any("step:1" in s for s in d.only_in_a)
    assert not any("step:1" in s for s in d.only_in_b)


def test_compute_diff_handles_missing_variant(tmp_path: Path):
    # v1 replay exists, v2 doesn't. verdict_b should be None and B-side
    # findings empty — no crash.
    ex = tmp_path / "K"
    ex.mkdir()
    _seed_replay(ex, "v1", {"overall_verdict": "pass", "findings": []})
    d = replay.compute_diff(ex, "v1", "v2")
    assert d.verdict_a == "pass"
    assert d.verdict_b is None
    assert d.sev_b == {"high": 0, "medium": 0, "low": 0}


def test_render_compare_counts_verdict_transitions(tmp_path: Path):
    # Three execution dirs with varying v1/v2 verdicts. Transitions table
    # must list the three distinct (from, to) pairs with correct counts.
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
    # Two audits promoted concerns -> fail, one stayed pass.
    assert "| concerns | fail | 2 |" in md
    assert "| pass | pass | 1 |" in md
    # Aggregate promotions = 2 (both A and B added a high).
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


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------
def test_cli_replay_requires_variant(tmp_path: Path, capsys):
    # --variant is mandatory for the replay path. Without it argparse
    # errors via parser.error which sys.exits(2).
    with pytest.raises(SystemExit) as exc:
        replay.main(["replay", "K-1"])
    assert exc.value.code == 2


def test_cli_replay_empty_keys_returns_error(
        tmp_path: Path, monkeypatch, capsys):
    # No --keys-file and no positional keys -> rc=1 with stderr message.
    # Use --config pointing at a temp file so we don't read the real one.
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
    # Zero successes, one skipped: still rc=0 because nothing actually
    # failed — we just couldn't find the key.
    assert rc == 0
    err = capsys.readouterr().err
    assert "SKIPPED" in err
    assert "GHOST-KEY" in err


def test_cli_compare_writes_replay_compare_md(tmp_path: Path, capsys):
    # Seed two replays for one key, then run compare.
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
    # --no-report-file means no file on disk.
    assert not (tmp_path / "_replay_compare.md").exists()
    assert "Replay comparison" in capsys.readouterr().out
