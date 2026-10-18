"""Public HTTP model clients and shared invocation safeguards.

Text and image requests use the Anthropic Messages schema. Optional audio
requests use a separately configured chat-completions endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
import threading
from typing import Any, Iterator
from urllib.parse import urlsplit

import requests

DEFAULT_MODEL_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_API_KEY_ENV = "ARGUS_MODEL_API_KEY"
DEFAULT_AUXILIARY_MODEL_ID = "claude-haiku-5-5"
DEFAULT_MAX_INFLIGHT_MODEL_CALLS = 12
_settings: Any = None
_inflight_limit = 0
_inflight_limiter: threading.BoundedSemaphore | None = None
_inflight_lock = threading.Lock()
_GUARD_CONTENT_OPEN = "<argus-guard-content>"
_GUARD_CONTENT_CLOSE = "</argus-guard-content>"


class ModelConfigurationError(RuntimeError):
    """The public model endpoint or credentials are not configured."""


class ModelInterventionError(RuntimeError):
    """A provider refused a request; callers must not bypass the refusal."""


class ModelInputError(ValueError):
    """A model request is malformed or contains reserved boundary markers."""


class ModelResponseError(ValueError):
    """A returned model response is not a complete JSON object."""


def configure_inflight_limit(max_inflight: int | None) -> None:
    """Bound concurrent limited invocations process-wide (<=0 disables)."""
    global _inflight_limit, _inflight_limiter
    limit = int(max_inflight or 0)
    with _inflight_lock:
        if limit <= 0:
            _inflight_limit = 0
            _inflight_limiter = None
            return
        if limit != _inflight_limit or _inflight_limiter is None:
            _inflight_limit = limit
            _inflight_limiter = threading.BoundedSemaphore(limit)


def inflight_limit() -> int:
    """Configured cap on concurrent limited invocations (0 = unlimited)."""
    return _inflight_limit


@contextmanager
def inflight_permit(enabled: bool = True) -> Iterator[None]:
    """Hold one permit of the process-wide in-flight limiter while active."""
    limiter = _inflight_limiter if enabled else None
    if limiter is None:
        yield
        return
    with limiter:
        yield


def parse_single_json_object(text: str) -> dict[str, Any]:
    """Parse exactly one complete top-level JSON object from a text envelope."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("response contained no text")

    starts: list[int] = []
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if depth == 0:
            if char == "{":
                starts.append(index)
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for start in starts:
        try:
            parsed, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)

    if not candidates:
        raise ValueError("response contained no complete JSON object")
    if len(candidates) > 1:
        raise ValueError("response contained multiple JSON objects")
    return candidates[0]


def guard_content(text: str) -> str:
    """Delimit untrusted evidence and reject attempts to forge its boundary."""
    value = str(text)
    if _GUARD_CONTENT_OPEN in value or _GUARD_CONTENT_CLOSE in value:
        raise ModelInputError("untrusted text contains reserved ARGUS guard markers")
    return f"{_GUARD_CONTENT_OPEN}{value}{_GUARD_CONTENT_CLOSE}"


def _prepare_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _prepare_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_prepare_value(item) for item in value]
    if not isinstance(value, str):
        return value
    if value.count(_GUARD_CONTENT_OPEN) != value.count(_GUARD_CONTENT_CLOSE):
        raise ModelInputError("unbalanced ARGUS guard-content markers")
    # The prompt itself already distinguishes evidence from instructions.
    return value.replace(_GUARD_CONTENT_OPEN, "").replace(_GUARD_CONTENT_CLOSE, "")


def configure(settings: Any) -> None:
    global _settings
    _settings = settings.auditor
    configure_inflight_limit(getattr(
        settings.auditor, "max_inflight_model_calls", DEFAULT_MAX_INFLIGHT_MODEL_CALLS,
    ))


def auxiliary_model_id() -> str:
    return getattr(_settings, "auxiliary_model_id", DEFAULT_AUXILIARY_MODEL_ID)


class ModelClient:
    """Small stateless HTTP client; secrets are read only when a call is made."""

    def __init__(self, *, endpoint: str = DEFAULT_MODEL_API_URL,
                 api_key_env: str = DEFAULT_API_KEY_ENV,
                 protocol: str = "anthropic", timeout: float = 300):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ModelConfigurationError("model endpoint must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelConfigurationError("model endpoint must not contain credentials or URL parameters")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ModelConfigurationError("remote model endpoints require HTTPS")
        if protocol not in {"anthropic", "openai"}:
            raise ModelConfigurationError("unsupported model API format")
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.protocol = protocol
        self.timeout = timeout

    def invoke_model(self, *, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        key = os.environ.get(self.api_key_env, "")
        if not key and self.api_key_env == DEFAULT_API_KEY_ENV:
            key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ModelConfigurationError(f"set {self.api_key_env} before invoking a model")
        payload = {**body, "model": model_id}
        headers = {"Content-Type": "application/json"}
        if self.protocol == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = f"Bearer {key}"
        response = requests.post(
            self.endpoint, headers=headers, json=payload,
            timeout=(30, self.timeout), allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            raise ModelConfigurationError("model endpoint redirects are not allowed")
        if response.status_code in {401, 403}:
            raise ModelInterventionError("model provider refused the request; check access and credentials")
        if not response.ok:
            # Provider error bodies can echo input or credentials. Do not log them.
            raise RuntimeError(f"model request failed with HTTP {response.status_code}")
        try:
            result = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            result = None
        if isinstance(result, dict):
            choices = result.get("choices")
            refused = result.get("stop_reason") == "refusal" or (
                isinstance(choices, list)
                and any(
                    isinstance(choice, dict) and choice.get("finish_reason") == "content_filter"
                    for choice in choices
                )
            )
            if refused:
                raise ModelInterventionError("model provider declined to process the supplied evidence")
        return {"body": io.BytesIO(response.content)}


def create_client(cfg: Any = None, *, timeout: float = 300,
                  audio: bool = False, model_api_url: str | None = None,
                  api_key_env: str | None = None) -> ModelClient:
    settings = cfg if cfg is not None else _settings
    if audio:
        endpoint = getattr(settings, "audio_api_url", "")
        if not endpoint:
            raise ModelConfigurationError("audio verification requires auditor.audio_api_url")
        return ModelClient(
            endpoint=endpoint,
            api_key_env=getattr(settings, "audio_api_key_env", "ARGUS_AUDIO_API_KEY"),
            protocol=getattr(settings, "audio_api_format", "openai"), timeout=timeout,
        )
    return ModelClient(
        endpoint=model_api_url or getattr(settings, "model_api_url", DEFAULT_MODEL_API_URL),
        api_key_env=api_key_env or getattr(settings, "api_key_env", DEFAULT_API_KEY_ENV),
        timeout=timeout,
    )


def invoke_model(client: Any, *, model_id: str, body: str,
                 content_type: str = "application/json", accept: str = "application/json",
                 allow_untagged_multimodal: bool = False, limit_inflight: bool = False) -> Any:
    """Invoke once while preserving ownership and process-wide concurrency limits."""
    from argus.operations import audit_lock
    audit_lock.ensure_ownership()
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise ModelInputError("model request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ModelInputError("model request body must be a JSON object")
    with inflight_permit(limit_inflight):
        return client.invoke_model(model_id=model_id, body=_prepare_value(payload))
