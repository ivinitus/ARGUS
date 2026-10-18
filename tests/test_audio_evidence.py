from array import array
import base64
from fractions import Fraction
import io
import json
import math
from unittest import mock
import wave

import pytest

from argus.config.models import ArgusConfig
from argus.pipeline import auditor, model_client, audio_evidence as audio
from argus.pipeline.video_evidence import VideoEvidenceError


@pytest.fixture(autouse=True)
def reset_model_client():
    model_client.configure(ArgusConfig())
    yield
    model_client.configure(ArgusConfig())


def observation(**changes):
    return {
        "audio_status": "speech_present",
        "audio_summary": "A short phrase is example.",
        "transcript": "Playback pauses now.",
        **changes,
    }


def response(observed, *, stop="stop"):
    return {"body": io.BytesIO(json.dumps({
        "choices": [{"finish_reason": stop, "message": {"content": json.dumps(observed)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }).encode())}


def record(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"validated video")
    return {"_path": path}


@pytest.mark.parametrize("pcm,limitations,status", [
    (b"\0" * 32000, [], "no_audible_audio"),
    (b"", ["No audio track"], "unavailable"),
    (b"", ["Recording exceeds the verification limit"], "unavailable"),
])
def test_silence_or_missing_audio_never_invokes_a_model(tmp_path, monkeypatch, pcm, limitations, status):
    monkeypatch.setattr(audio, "_decode_audio", lambda path: (pcm, limitations))
    invoke = mock.Mock()
    monkeypatch.setattr(model_client, "invoke_model", invoke)
    result = audio.observe_audio(auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path), record(tmp_path))
    assert result["audio_status"] == status
    assert result["observations"] == []
    assert result["limitations"] == limitations
    invoke.assert_not_called()


def test_audio_retry_preserves_waveform_guardrails_and_usage(tmp_path, monkeypatch):
    settings = ArgusConfig()
    model_client.configure(settings)
    client = mock.MagicMock()
    client.invoke_model.side_effect = [response([]), response(observation())]
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: client)
    pcm = array("h", [500] * 16000).tobytes()
    cfg = auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path)
    observed = audio._observe_chunk(cfg, pcm, offset=30)
    assert observed["observations"][0]["start_seconds"] == 30
    assert observed["observations"][0]["end_seconds"] == 31
    assert observed["observations"][0]["speech_excerpt"] == "Playback pauses now."
    requests = [call.kwargs["body"] for call in client.invoke_model.call_args_list]
    first_audio = requests[0]["messages"][0]["content"][1]["input_audio"]
    assert first_audio == requests[1]["messages"][0]["content"][1]["input_audio"]
    with wave.open(io.BytesIO(base64.b64decode(first_audio["data"])), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.readframes(16000) == pcm
    assert cfg.usage_tracker.summary(cfg.cost_rates)["calls"] == 2


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"usage":[]}'])
def test_unreadable_audio_responses_are_counted_before_repair(tmp_path, monkeypatch, body):
    client = mock.MagicMock()
    client.invoke_model.side_effect = [{"body": io.BytesIO(body)}, response(observation())]
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: client)
    cfg = auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path, cost_rates={
        "audio-model": {
            "input_per_million": 1, "output_per_million": 2,
            "cache_write_per_million": 0, "cache_read_per_million": 0,
        },
    })
    audio._observe_chunk(cfg, array("h", [500] * 16000).tobytes(), offset=0)
    usage = cfg.usage_tracker.summary(cfg.cost_rates)
    assert usage["calls"] == 2
    assert usage["unpriced_calls"] == 1
    assert usage["estimated_cost_usd"] == pytest.approx(0.00014)


@pytest.mark.parametrize("changes", [
    {"audio_status": "other_audio"}, {"transcript": ""}, {"transcript": []},
    {"transcript": "x" * 4001},
])
def test_invalid_audio_observations_fail_after_bounded_repair(tmp_path, monkeypatch, changes):
    invalid = observation()
    invalid.update(changes)
    client = mock.MagicMock()
    client.invoke_model.side_effect = [response(invalid), response(invalid)]
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: client)
    with pytest.raises(VideoEvidenceError, match="after one repair"):
        audio._observe_chunk(auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path), b"\0" * 32000, offset=0)
    assert client.invoke_model.call_count == 2


def test_truncated_audio_recovers_with_all_samples_and_timestamps_preserved(tmp_path, monkeypatch):
    settings = ArgusConfig()
    model_client.configure(settings)
    client = mock.Mock()
    client.invoke_model.side_effect = [
        response(observation(), stop="length"),
        response(observation(), stop="length"),
        response(observation(transcript="First half.")),
        response(observation(audio_status="uncertain", transcript="")),
    ]
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: client)
    pcm = array("h", [500] * (15 * 16000) + [-500] * (15 * 16000)).tobytes()
    cfg = auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path)
    result = audio._observe_chunk(cfg, pcm, offset=60)
    assert result["audio_status"] == "uncertain"
    assert [(r["start_seconds"], r["end_seconds"]) for r in result["observations"]] == [
        (60, 75), (75, 90),
    ]
    assert result["observations"][0]["speech_excerpt"] == "First half."
    assert result["observations"][1]["speech_excerpt"] == ""
    sent = []
    for call in client.invoke_model.call_args_list:
        body = call.kwargs["body"]
        encoded = body["messages"][0]["content"][1]["input_audio"]["data"]
        with wave.open(io.BytesIO(base64.b64decode(encoded)), "rb") as handle:
            sent.append(handle.readframes(handle.getnframes()))
    assert sent[:2] == [pcm, pcm]
    assert sent[2] + sent[3] == pcm
    assert cfg.usage_tracker.summary(cfg.cost_rates)["calls"] == 4


def test_shorter_audio_recovery_does_not_split_recursively(tmp_path, monkeypatch):
    client = mock.Mock()
    client.invoke_model.side_effect = [response(observation(), stop="length") for _ in range(4)]
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: client)
    with pytest.raises(VideoEvidenceError, match="audio model response is incomplete"):
        audio._observe_chunk(
            auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path),
            array("h", [500] * (30 * 16000)).tobytes(), offset=0,
        )
    assert client.invoke_model.call_count == 4


def test_shorter_audio_recovery_never_sends_silent_half_to_model(tmp_path, monkeypatch):
    client = mock.Mock()
    client.invoke_model.side_effect = [
        response(observation(), stop="length"),
        response(observation(), stop="length"),
        response(observation()),
    ]
    monkeypatch.setattr(auditor, "_make_model_client", lambda *a, **kw: client)
    pcm = b"\0" * (15 * 32000) + array("h", [500] * (15 * 16000)).tobytes()
    result = audio._observe_chunk(auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path), pcm, offset=60)
    assert client.invoke_model.call_count == 3
    assert [(r["start_seconds"], r["end_seconds"]) for r in result["observations"]] == [(75, 90)]


@pytest.mark.parametrize("seconds", [1, 30])
def test_audio_guardrail_intervention_is_not_retried(tmp_path, monkeypatch, seconds):
    invoke = mock.Mock(side_effect=model_client.ModelInterventionError("blocked"))
    monkeypatch.setattr(model_client, "invoke_model", invoke)
    monkeypatch.setattr(auditor, "_make_model_client", mock.Mock())
    with pytest.raises(model_client.ModelInterventionError):
        audio._observe_chunk(
            auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path), b"\0" * (seconds * 32000), offset=0,
        )
    assert invoke.call_count == 1


def test_chunk_offsets_include_skipped_silence(tmp_path, monkeypatch):
    pcm = b"\0" * (30 * 32000) + array("h", [500] * 16000).tobytes()
    monkeypatch.setattr(audio, "_decode_audio", lambda path: (pcm, []))
    observe = mock.Mock(return_value={**observation(), "observations": []})
    monkeypatch.setattr(audio, "_observe_chunk", observe)
    result = audio.observe_audio(auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path), record(tmp_path))
    assert result["audio_status"] == "speech_present"
    assert observe.call_count == 1
    assert observe.call_args.kwargs["offset"] == 30
    assert result["quiet_intervals_seconds"] == [[0, 30]]


@pytest.mark.parametrize("quiet_seconds", [0.4, 0.5, 0.6])
@pytest.mark.parametrize("trailing", [False, True])
def test_quiet_intervals_require_half_a_second(tmp_path, monkeypatch, quiet_seconds, trailing):
    quiet = b"\0" * round(quiet_seconds * 16000 * 2)
    audible = array("h", [500] * 16000).tobytes()
    pcm = audible + quiet if trailing else quiet + audible
    monkeypatch.setattr(audio, "_decode_audio", lambda path: (pcm, []))
    monkeypatch.setattr(audio, "_observe_chunk", lambda *args, **kwargs: {
        **observation(), "observations": [],
    })
    result = audio.observe_audio(auditor.AuditConfig(video_audio_model_id="audio-model", execution_dir=tmp_path), record(tmp_path))
    expected = [[1, 1 + quiet_seconds] if trailing else [0, quiet_seconds]]
    assert result["quiet_intervals_seconds"] == (expected if quiet_seconds >= 0.5 else [])


def test_real_mp4_silent_audio_decode(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "silent.mp4"
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("aac", rate=16000)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=16000)
        frame.sample_rate = 16000
        frame.pts = 0
        frame.planes[0].update(b"\0" * 32000)
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    pcm, limitations = audio._decode_audio(path)
    assert len(pcm) >= 32000
    assert audio._peak(pcm) == 0
    assert limitations == []


def test_real_webm_opus_audio_is_decoded_with_timing_and_signal_preserved(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "recording.webm"
    with av.open(str(path), mode="w", format="webm") as container:
        stream = container.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        for offset in range(0, 48000, 960):
            frame = av.AudioFrame(format="flt", layout="mono", samples=960)
            frame.sample_rate = 48000
            frame.time_base = Fraction(1, 48000)
            frame.pts = offset
            values = array("f", (
                0.25 * math.sin(2 * math.pi * 440 * sample / 48000)
                for sample in range(offset, offset + 960)
            ))
            frame.planes[0].update(values.tobytes())
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    pcm, limitations = audio._decode_audio(path)
    assert limitations == []
    assert len(pcm) / 32000 == pytest.approx(1, abs=0.05)
    assert audio._peak(pcm) > 5000
