"""Lossless visual containers for H.264 recordings labelled as WEBM."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, TYPE_CHECKING

from argus.pipeline import attachment_quarantine
from argus.pipeline.video_evidence import VideoEvidenceError

if TYPE_CHECKING:
    from argus.pipeline.auditor import AuditConfig

_MAX_BYTES = 128 * 1024 * 1024
_MAX_PACKETS = 500_000
_MAX_SECONDS = 60


def _remux_h264(source: Path, destination: Path) -> bool:
    """Copy H.264 packets and timestamps; leave audio in the original file."""
    import av

    if source.stat().st_size > _MAX_BYTES:
        raise VideoEvidenceError("visual container input exceeds the video size limit")
    started = time.monotonic()
    with av.open(
        str(source), format="matroska", options={"protocol_whitelist": "file"},
    ) as incoming:
        if len(incoming.streams.video) != 1:
            raise VideoEvidenceError("WEBM must contain exactly one video stream")
        stream = incoming.streams.video[0]
        if stream.codec_context.name != "h264":
            return False
        with av.open(str(destination), "w", format="mp4") as outgoing:
            target = outgoing.add_stream_from_template(stream)
            count = 0
            for packet in incoming.demux(stream):
                if time.monotonic() - started > _MAX_SECONDS:
                    raise VideoEvidenceError("visual container preparation exceeded its time limit")
                if not packet.size:
                    continue
                if packet.dts is None or packet.pts is None:
                    # Some Matroska encoders omit initial decode timestamps.
                    # Never drop those packets (which can include a keyframe)
                    # or invent timing; let the native WEBM reader handle it.
                    return False
                count += 1
                if count > _MAX_PACKETS or (
                    destination.exists() and destination.stat().st_size > _MAX_BYTES
                ):
                    raise VideoEvidenceError("visual container preparation exceeded its size limit")
                packet.stream = target
                outgoing.mux(packet)
            if not count:
                raise VideoEvidenceError("WEBM contained no usable video packets")
    if destination.stat().st_size > _MAX_BYTES:
        raise VideoEvidenceError("visual container output exceeds the video size limit")
    return True


def prepare_h264_visual_record(
    cfg: AuditConfig, record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a scanned MP4 visual copy, or None for other WEBM codecs.

    Screen recorders can label a Matroska/H.264 capture as WEBM, which video
    models may misread. Remuxing gives those exact frames an MP4 container.
    The caller must continue audio analysis from the original record.
    """
    source = record.get("_path")
    root = cfg.execution_dir.resolve()
    if not isinstance(source, Path) or not source.resolve().is_relative_to(root):
        raise VideoEvidenceError("visual container source escapes the execution directory")
    with tempfile.TemporaryDirectory(prefix="argus-visual-container-") as directory:
        remuxed = Path(directory) / "visual.mp4"
        try:
            if not _remux_h264(source, remuxed):
                return None
        except VideoEvidenceError:
            raise
        except Exception as exc:
            raise VideoEvidenceError("could not prepare the WEBM visual container") from exc
        with remuxed.open("rb") as body:
            digest = hashlib.file_digest(body, "sha256").hexdigest()
        destination = root / "video_evidence" / f"visual-{digest[:24]}.mp4"

        def copy_visual(_session, _base_url, _attachment_id, path, *, max_bytes):
            if remuxed.stat().st_size > max_bytes:
                raise VideoEvidenceError("visual container exceeds the quarantine size limit")
            shutil.copyfile(remuxed, path)
            return "video/mp4"

        released = attachment_quarantine.download_and_release(
            session=None, base_url="", execution_key=root.name,
            attachment_id=int(record.get("id") or 0),
            attachment_name=destination.name, destination=destination,
            downloader=copy_visual,
        )
        return {
            **record, "format": "mp4", "_path": released.path,
            "path": destination.relative_to(root).as_posix(),
        }
