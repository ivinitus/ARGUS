"""Local video decoding and model-independent QA evidence handling."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from argus.pipeline.auditor import AuditConfig

_MAX_VIDEOS_PER_AUDIT = 4
_MAX_VIDEO_SECONDS = 300
_MAX_VIDEO_FRAMES = 60


class VideoEvidenceError(RuntimeError):
    """Video evidence cannot be safely prepared or interpreted."""


def load_video_records(execution_dir: Path) -> list[dict[str, Any]]:
    """Load validated records whose local files still exist."""
    manifest_path = execution_dir / "video_evidence.json"
    try:
        payload = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoEvidenceError("video evidence manifest is unreadable") from exc
    rows = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise VideoEvidenceError("video evidence manifest has no videos list")
    if len(rows) > _MAX_VIDEOS_PER_AUDIT:
        raise VideoEvidenceError(
            f"recording count exceeds {_MAX_VIDEOS_PER_AUDIT}; full evidence was not assessed"
        )

    records: list[dict[str, Any]] = []
    root = execution_dir.resolve()
    for row in rows[:_MAX_VIDEOS_PER_AUDIT]:
        if not isinstance(row, dict) or row.get("format") not in {"mp4", "webm"}:
            continue
        relative = Path(str(row.get("path") or ""))
        path = (execution_dir / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise VideoEvidenceError(
                "video evidence path escapes the execution directory"
            ) from exc
        if path.is_file():
            records.append({**row, "_path": path})
    return records


def video_format(record: dict[str, Any]) -> str:
    format_name = record.get("format", "mp4")
    if format_name not in {"mp4", "webm"}:
        raise VideoEvidenceError("unsupported video container")
    return format_name


def video_frames(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode bounded, timestamped visual samples from a validated local recording."""
    video_format(record)
    path = record.get("_path")
    if not isinstance(path, Path) or not path.is_file():
        raise VideoEvidenceError("validated local video is required")
    try:
        import av
    except ImportError as exc:
        raise VideoEvidenceError("video analysis requires the optional 'video' dependency") from exc
    frames: list[dict[str, Any]] = []
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise VideoEvidenceError("recording contains no video track")
            stream = container.streams.video[0]
            duration = (
                float(stream.duration * stream.time_base)
                if stream.duration is not None and stream.time_base is not None else
                container.duration / 1_000_000 if container.duration is not None else None
            )
            if duration is not None and duration > _MAX_VIDEO_SECONDS:
                raise VideoEvidenceError("recording exceeds the 300-second verification limit")
            interval = max(1.0, (duration or _MAX_VIDEO_SECONDS) / (_MAX_VIDEO_FRAMES - 1))
            next_sample = 0.0
            for index, frame in enumerate(container.decode(stream)):
                if index >= 100_000:
                    raise VideoEvidenceError("recording exceeds the bounded frame decoding limit")
                if frame.time is None:
                    raise VideoEvidenceError("video frame has no reliable timestamp")
                timestamp = float(frame.time)
                if timestamp > _MAX_VIDEO_SECONDS:
                    raise VideoEvidenceError("recording exceeds the 300-second verification limit")
                if timestamp < next_sample or len(frames) >= _MAX_VIDEO_FRAMES:
                    continue
                image = frame.to_image().convert("RGB")
                image.thumbnail((1280, 1280))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85)
                frames.append({
                    "seconds": round(timestamp, 3),
                    "image": {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
                    }},
                })
                next_sample = timestamp + interval
    except VideoEvidenceError:
        raise
    except Exception as exc:
        raise VideoEvidenceError("video decoding failed") from exc
    if not frames:
        raise VideoEvidenceError("recording contains no decodable timestamped frames")
    return frames, [
        f"Visual analysis used {len(frames)} timestamped frames sampled at approximately "
        f"{interval:g}-second intervals; events between frames remain unverified. "
        "Audio is analyzed separately from the original recording."
    ]


def read_model_response(
    cfg: AuditConfig, response: dict[str, Any], *, model_id: str, operation: str,
) -> dict[str, Any]:
    """Count a returned model API call even when its body cannot be decoded."""
    try:
        raw = json.loads(response["body"].read())
        if not isinstance(raw, dict):
            raise VideoEvidenceError("model response must be an object")
    except Exception:
        cfg.usage_tracker.record_unpriced_call(model_id=model_id, operation=operation)
        raise
    return raw


def audit_videos(cfg: AuditConfig, steps_text: str,
                 records: list[dict[str, Any]]) -> dict[str, Any]:
    """Assess every selected recording using timestamped visual observations."""
    if not records or len(records) > _MAX_VIDEOS_PER_AUDIT:
        raise VideoEvidenceError(f"supply between 1 and {_MAX_VIDEOS_PER_AUDIT} video records")
    from argus.pipeline.video_observations import audit_with_observations
    return audit_with_observations(cfg, steps_text, records)


def merge_video_audit(
    base: dict[str, Any],
    video: dict[str, Any],
) -> dict[str, Any]:
    """Merge video findings without allowing either modality to mask the other."""
    merged = dict(base)
    rank = {"pass": 0, "concerns": 1, "fail": 2}
    merged["overall_verdict"] = max(
        (base.get("overall_verdict", "concerns"),
         video.get("overall_verdict", "concerns")),
        key=lambda verdict: rank.get(verdict, 1),
    )
    merged["findings"] = [
        *(base.get("findings") or []),
        *(video.get("findings") or []),
    ]
    if (
        base.get("review_status") == "required"
        or video.get("review_status") == "required"
        or any(
            item.get("overall_verdict") in {"concerns", "fail"}
            and not item.get("findings")
            for item in (base, video)
        )
    ):
        merged["review_status"] = "required"
    if (
        video.get("review_status") == "required"
        or video.get("overall_verdict") in {"concerns", "fail"} and not video.get("findings")
    ):
        merged["_video_review_required"] = True
    base_summary = str(base.get("summary") or "").strip()
    video_summary = str(video.get("summary") or "").strip()
    if base_summary == "No still-image evidence was supplied.":
        merged["summary"] = video_summary
    elif video_summary:
        merged["summary"] = (
            f"{base_summary} Video evidence: {video_summary}"
        ).strip()
    merged["_video_evidence"] = video.get("_video_evidence")
    return merged
