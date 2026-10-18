from __future__ import annotations

from fractions import Fraction
import hashlib

import av
import pytest

from argus.config.models import ArgusConfig
from argus.pipeline import attachment_quarantine, auditor, video_container as vc
from argus.pipeline.video_evidence import VideoEvidenceError


@pytest.fixture
def recording(tmp_path, request):
    path = tmp_path / "capture.webm"
    variant = getattr(request, "param", "libx264")
    with av.open(str(path), "w", format="matroska") as container:
        stream = container.add_stream("libx264" if variant == "bframes" else variant, rate=25)
        stream.width = 32
        stream.height = 32
        stream.pix_fmt = "yuv420p"
        if variant == "libx264":
            stream.options = {"tune": "zerolatency"}
        for index in range(25):
            frame = av.VideoFrame(32, 32, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes([index * 5]) * plane.buffer_size)
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def frames(path):
    with av.open(str(path)) as container:
        return [
            (float(frame.pts * frame.time_base), hashlib.sha256(frame.to_image().tobytes()).hexdigest())
            for frame in container.decode(video=0)
        ]


def test_remux_preserves_every_frame_timestamp_and_original_bytes(recording, tmp_path):
    attachment_quarantine.configure(ArgusConfig())
    before = recording.read_bytes()
    original = {"id": 123, "format": "webm", "_path": recording}
    cfg = auditor.AuditConfig(execution_dir=tmp_path)
    prepared = vc.prepare_h264_visual_record(cfg, original)
    assert prepared["format"] == "mp4"
    assert frames(prepared["_path"]) == frames(recording)
    assert recording.read_bytes() == before
    assert original["format"] == "webm"


def test_visual_copy_cannot_bypass_quarantine(recording, tmp_path, monkeypatch):
    def reject(**kwargs):
        raise attachment_quarantine.QuarantineError("scan rejected")
    monkeypatch.setattr(attachment_quarantine, "download_and_release", reject)
    with pytest.raises(attachment_quarantine.QuarantineError, match="scan rejected"):
        vc.prepare_h264_visual_record(
            auditor.AuditConfig(execution_dir=tmp_path),
            {"id": 123, "format": "webm", "_path": recording},
        )


def test_visual_copy_rejects_path_outside_execution(recording, tmp_path):
    with pytest.raises(VideoEvidenceError, match="escapes"):
        vc.prepare_h264_visual_record(
            auditor.AuditConfig(execution_dir=tmp_path / "other"),
            {"format": "webm", "_path": recording},
        )


def test_visual_copy_enforces_packet_budget(recording, tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "_MAX_PACKETS", 1)
    with pytest.raises(VideoEvidenceError, match="size limit"):
        vc._remux_h264(recording, tmp_path / "visual.mp4")


@pytest.mark.parametrize("recording", ["libvpx", "bframes"], indirect=True)
def test_other_codecs_and_missing_timestamps_keep_the_original(recording, tmp_path):
    assert vc.prepare_h264_visual_record(
        auditor.AuditConfig(execution_dir=tmp_path),
        {"format": "webm", "_path": recording},
    ) is None
