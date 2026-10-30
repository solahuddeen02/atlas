# core/security.py
from __future__ import annotations

import os
from functools import lru_cache

MIN_SECRET_KEY_LENGTH = 43  # 32 random bytes, base64url-encoded
MIN_SECRET_KEY_DISTINCT_CHARS = 16

# Every placeholder ever committed to this repo, plus common weak values.
DENYLISTED_SECRET_KEYS = {
    "dev-secret-change-in-production",
    "change-this-in-production",
    "changeme",
    "secret",
    "password",
}

GENERATE_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'


class InsecureConfigError(RuntimeError):
    """Raised when required security configuration is missing or weak."""


def validate_secret_key(value: str | None) -> str:
    """Return the key if it is strong enough; raise without echoing the value otherwise."""
    if not value:
        raise InsecureConfigError(f"SECRET_KEY is not set. Generate one with: {GENERATE_HINT}")

    lowered = value.lower()
    if lowered in DENYLISTED_SECRET_KEYS or ("change" in lowered and "production" in lowered):
        raise InsecureConfigError(
            f"SECRET_KEY is a known placeholder and must not be used. Generate one with: {GENERATE_HINT}"
        )
    if len(value) < MIN_SECRET_KEY_LENGTH:
        raise InsecureConfigError(
            f"SECRET_KEY is too short (minimum {MIN_SECRET_KEY_LENGTH} characters). "
            f"Generate one with: {GENERATE_HINT}"
        )
    if len(set(value)) < MIN_SECRET_KEY_DISTINCT_CHARS:
        raise InsecureConfigError(
            f"SECRET_KEY is too repetitive (minimum {MIN_SECRET_KEY_DISTINCT_CHARS} distinct characters). "
            f"Generate one with: {GENERATE_HINT}"
        )
    return value


@lru_cache(maxsize=1)
def get_secret_key() -> str:
    return validate_secret_key(os.getenv("SECRET_KEY"))


DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def get_max_upload_bytes() -> int:
    raw = os.getenv("ATLAS_MAX_UPLOAD_BYTES")
    if raw is None or raw == "":
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise InsecureConfigError("ATLAS_MAX_UPLOAD_BYTES must be a positive integer (bytes)")
    if value <= 0:
        raise InsecureConfigError("ATLAS_MAX_UPLOAD_BYTES must be a positive integer (bytes)")
    return value
