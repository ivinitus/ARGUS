import json

import pytest

from argus.pipeline import video_evidence


def test_load_video_records_requires_in_tree_file(tmp_path):
    path = tmp_path / "recording.mp4"
    path.write_bytes(b"video")
    (tmp_path / "video_evidence.json").write_text(json.dumps({"videos": [{"format": "mp4", "path": path.name}]}))
    records = video_evidence.load_video_records(tmp_path)

    assert len(records) == 1
    assert records[0]["_path"] == path


def test_merge_video_audit_replaces_video_only_placeholder():
    merged = video_evidence.merge_video_audit(
        {
            "overall_verdict": "pass",
            "summary": "No still-image evidence was supplied.",
            "findings": [],
        },
        {
            "overall_verdict": "concerns",
            "summary": "A visible interruption occurred.",
            "findings": [{"severity": "medium"}],
            "_video_evidence": {"videos_reviewed": 1},
        },
    )

    assert merged["summary"] == "A visible interruption occurred."
    assert merged["findings"] == [{"severity": "medium"}]


@pytest.mark.parametrize("format_name,codec,extension", [
    ("mp4", "libx264", "mp4"), ("webm", "libvpx-vp9", "webm"),
])
def test_local_video_decoding_preserves_timestamps_and_bounds(tmp_path, format_name, codec, extension):
    av = pytest.importorskip("av")
    from PIL import Image
    path = tmp_path / ("recording." + extension)
    with av.open(str(path), "w", format=format_name) as container:
        stream = container.add_stream(codec, rate=2)
        stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
        for index in range(5):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 64), (index * 30, 0, 0)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    frames, limits = video_evidence.video_frames({"_path": path, "format": format_name})
    assert [frame["seconds"] for frame in frames] == [0, 1, 2]
    assert all(frame["image"]["source"]["media_type"] == "image/jpeg" for frame in frames)
    assert "events between frames remain unverified" in limits[0]


def test_video_frames_require_validated_local_file(tmp_path):
    with pytest.raises(video_evidence.VideoEvidenceError, match="validated local"):
        video_evidence.video_frames({"format": "mp4", "_path": tmp_path / "missing.mp4"})
