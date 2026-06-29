"""Redaction helpers for Agent trace payloads."""

from __future__ import annotations

import re
from typing import Any


SENSITIVE_KEYS = {
    "access_key",
    "access_key_id",
    "access_key_secret",
    "api_key",
    "authorization",
    "cookie",
    "db_url",
    "dsn",
    "jwt",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "security_token",
    "session",
    "token",
}

_AUTH_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/=-]{16,}")
_COOKIE_RE = re.compile(r"(?i)\b(cookie\s*[:=]\s*)([^;\n]{8,}(?:;[^\n]{0,200})?)")
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key[_-]?secret|token|password|private[_-]?key|secret)\s*[:=]\s*([^\s,;]+)"
)
_DB_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:\s/@]+:)([^@\s]+)(@)")


def redact_payload(value: Any, *, max_string_length: int = 12000) -> Any:
    """Return a JSON-safe payload copy with common credentials removed."""
    return _redact(value, max_string_length=max_string_length, depth=0)


def _redact(value: Any, *, max_string_length: int, depth: int) -> Any:
    if depth > 12:
        return "[TRUNCATED_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _redact_text(value)
        if len(text) > max_string_length:
            return text[:max_string_length] + "...[TRUNCATED]"
        return text
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                out[key_text] = "[REDACTED]"
            else:
                out[key_text] = _redact(item, max_string_length=max_string_length, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_redact(item, max_string_length=max_string_length, depth=depth + 1) for item in list(value)[:200]]
    try:
        return _redact_text(str(value))
    except Exception:  # noqa: BLE001
        return "[UNSERIALIZABLE]"


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEYS)


def _redact_text(text: str) -> str:
    text = _DB_URL_RE.sub(r"\1[REDACTED]\3", text or "")
    text = _AUTH_RE.sub(r"\1[REDACTED]", text)
    text = _COOKIE_RE.sub(r"\1[REDACTED]", text)
    text = _KEY_VALUE_RE.sub(r"\1=[REDACTED]", text)
    return text
