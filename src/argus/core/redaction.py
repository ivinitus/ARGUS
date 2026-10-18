"""Mask test-account passwords in tester-authored text."""
from __future__ import annotations

import re

ACCOUNT_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_CREDENTIAL_WINDOW_CHARS = 120
_SECRET_SYMBOLS = frozenset("#$%!&*?^")
_STRONG_SECRET_SYMBOLS = frozenset("#$%&*^")
_SECRET_TOKEN_RE = re.compile(
    r"(?<!\S)(?![^\s]*[@:/])(?=[^\s]*[a-z])"
    r"[A-Za-z0-9#$%!&*?^+=_.-]{8,64}(?!\S)")
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(?P<label>password|passwd|pwd|passcode)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<secret>[^<\s]+)"
)
_LABELED_IS_SECRET_RE = re.compile(
    r"(?i)\b(?P<label>password|passwd|pwd|passcode)"
    r"(?P<separator>\s+is\s+)"
    r"(?P<secret>(?=[^<\s]*[a-z])(?=[^<\s]*\d)"
    r"[A-Za-z0-9#$%!&*?^+=_.-]{8,64})"
)


def _mask_labeled_secret(match: re.Match[str]) -> str:
    return f"{match.group('label')}{match.group('separator')}[redacted]"


def _looks_like_unlabeled_secret(token: str) -> bool:
    if any(char.isdigit() for char in token):
        return True
    symbols = [char for char in token if char in _SECRET_SYMBOLS]
    if len(symbols) < 2:
        return False
    if token.startswith(("**", "__", "~~")):
        return False
    uppercase_count = sum(char.isupper() for char in token)
    has_strong_symbol = any(
        char in _STRONG_SECRET_SYMBOLS for char in symbols
    )
    if uppercase_count == 0:
        return has_strong_symbol
    if uppercase_count == 1:
        return has_strong_symbol or token[-1] in _SECRET_SYMBOLS
    return token[-1] in _SECRET_SYMBOLS


def _mask_unlabeled_secret(match: re.Match[str]) -> str:
    token = match.group(0)
    return "[redacted]" if _looks_like_unlabeled_secret(token) else token


def redact_credentials(text: str) -> str:
    """Mask explicit passwords and password-shaped tokens after an email.

    Explicit ``password:``, ``password=``, ``passwd`` and ``pwd`` labels
    are masked wherever they occur. The fallback heuristic masks only
    whitespace-delimited tokens of 8-64 chars containing a lowercase letter
    and either a digit or a password-like multi-symbol shape, within 120 chars
    after an email. Multi-symbol reviewer prose and Markdown remain visible.
    Tokens containing '@', ':' or '/' are left alone
    (emails, URLs), as are all-letter and all-digit tokens (names, order ids).
    Widen with care — over-masking destroys reviewer-critical comments.
    """
    if not text:
        return text
    text = _LABELED_SECRET_RE.sub(_mask_labeled_secret, text)
    text = _LABELED_IS_SECRET_RE.sub(_mask_labeled_secret, text)
    if "@" not in text:
        return text
    out = []
    cursor = 0
    for m in ACCOUNT_EMAIL_RE.finditer(text):
        window_end = min(len(text), m.end() + _CREDENTIAL_WINDOW_CHARS)
        # Never cut mid-token: a secret straddling the window edge would
        # otherwise be masked only up to the cut and leak its tail.
        while window_end < len(text) and not text[window_end].isspace():
            window_end += 1
        if window_end <= cursor:
            continue
        window_start = max(m.end(), cursor)
        out.append(text[cursor:window_start])
        out.append(_SECRET_TOKEN_RE.sub(
            _mask_unlabeled_secret,
            text[window_start:window_end],
        ))
        cursor = window_end
    out.append(text[cursor:])
    return "".join(out)
