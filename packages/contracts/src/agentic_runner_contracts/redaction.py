"""Scrub secret-shaped text before it is persisted (ADR-0013 §4).

Both sides write Evidence, and both must scrub it the same way: the Runner's verifier
output, git evidence and hook transcripts, and the platform's Slack intake record and
Incident payloads. One copy of the patterns, in contracts, so a token form either side
learns to recognise is recognised by both. Stdlib ``re`` only.
"""

from __future__ import annotations

import re

_PRIVATE_KEY_RE = re.compile(
    r"[+\- ]?-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"(?:[+\- ]?-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|pwd|"
    r"secret|client[_-]?secret|private[_-]?key)\b(?P<assignment>\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^'\"\s]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)\b(https?://)[^\s/@:]+(?::[^\s/@]*)?@")
_TRUNCATED_URL_USERINFO_RE = re.compile(
    r"(?i)\b(https?://)(?:x-token-auth|oauth2|token|[^\s/@:]*token[^\s/@:]*):[^\s/@]*"
)
_GENERIC_TRUNCATED_URL_USERINFO_RE = re.compile(
    r"(?i)\b(https?://)[^\s/@:]+:(?=[^\s/@/]*[^\d\s/@/])[^\s/@/]*"
)
_NUMERIC_TRUNCATED_URL_USERINFO_RE = re.compile(r"(?i)\b(https?://)[^\s/@:]+:\d+(?=\s|\Z)")
_OPENAI_TOKEN_RE = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b")
_STRIPE_TOKEN_RE = re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9_-]+\b")
_BARE_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"eyJ[A-Za-z0-9_-]{8,127}\."
    r"[A-Za-z0-9_-]{10,4096}\."
    r"[A-Za-z0-9_-]{0,1024}"
    r"(?![A-Za-z0-9_.-])"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact_secret_like_text(text: str) -> str:
    """Redact common credential, token, URL-userinfo, and private-key evidence forms."""

    redacted = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", redacted)
    redacted = _TRUNCATED_URL_USERINFO_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _GENERIC_TRUNCATED_URL_USERINFO_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _NUMERIC_TRUNCATED_URL_USERINFO_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _KEY_VALUE_RE.sub(_redact_key_value_match, redacted)
    redacted = _OPENAI_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _GITHUB_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _SLACK_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _STRIPE_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _BARE_JWT_RE.sub("[REDACTED]", redacted)
    return _BEARER_RE.sub("Bearer [REDACTED]", redacted)


def _redact_key_value_match(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if value.startswith(("'", '"')) else ""
    if quote:
        return f"{match.group(1)}{match.group('assignment')}{quote}[REDACTED]{quote}"
    return f"{match.group(1)}{match.group('assignment')}[REDACTED]"
