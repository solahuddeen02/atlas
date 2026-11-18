"""Encryption of what Atlas stores for a session (ADR-003 §4, "Login state and stored tokens").

- Keys come from `SECRET_KEY` with HKDF-SHA256, with a separate `info` label for each use,
  so a key for one use never decrypts another's data.
- AES-256-GCM with a random 96-bit nonce per encryption.
- The row's id is the associated data: a ciphertext cut out of one row and pasted into
  another does not decrypt.
- `SECRET_KEY_PREVIOUS` only decrypts, and never encrypts. A blob that neither key opens
  raises `TokenBoxError`, which says nothing about why.

Blob layout: one version byte (0x01), the 12-byte nonce, then ciphertext and tag.
"""

from __future__ import annotations

import os
from enum import StrEnum
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32
_INFO_PREFIX = b"atlas/kernel/tokenbox/v1/"


class Purpose(StrEnum):
    """One HKDF label per use. Add a label for a new use; never reuse one."""

    REFRESH_TOKEN = "refresh-token"
    PKCE_VERIFIER = "pkce-verifier"
    ID_TOKEN_HINT = "id-token-hint"


class TokenBoxError(Exception):
    """A blob could not be opened. Deliberately says nothing more."""

    def __init__(self) -> None:
        super().__init__("the stored value could not be decrypted")


def _derive(secret: SecretStr, purpose: Purpose) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=_INFO_PREFIX + purpose.value.encode("ascii"),
    ).derive(secret.get_secret_value().encode("utf-8"))


class TokenBox:
    def __init__(self, secret_key: SecretStr, previous: SecretStr | None = None) -> None:
        self._current = secret_key
        self._previous = previous

    def __repr__(self) -> str:
        return "TokenBox(<redacted>)"

    __str__ = __repr__

    def seal(self, purpose: Purpose, row_id: UUID, plaintext: bytes) -> bytes:
        """Encrypt `plaintext` for the row `row_id`, under the current `SECRET_KEY` only."""
        nonce = os.urandom(NONCE_BYTES)
        key = _derive(self._current, purpose)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, row_id.bytes)
        return bytes([VERSION]) + nonce + ciphertext

    def open(self, purpose: Purpose, row_id: UUID, blob: bytes) -> bytes:
        """Decrypt a blob made by `seal` for this purpose and row, with either key."""
        if len(blob) < 1 + NONCE_BYTES + 16 or blob[0] != VERSION:
            raise TokenBoxError
        nonce, ciphertext = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        for secret in (self._current, self._previous):
            if secret is None:
                continue
            try:
                return AESGCM(_derive(secret, purpose)).decrypt(nonce, ciphertext, row_id.bytes)
            except InvalidTag:
                continue
        raise TokenBoxError

    def needs_reseal(self, purpose: Purpose, row_id: UUID, blob: bytes) -> bool:
        """True when `blob` opens only with `SECRET_KEY_PREVIOUS`: seal it again."""
        if len(blob) < 1 + NONCE_BYTES + 16 or blob[0] != VERSION:
            raise TokenBoxError
        nonce, ciphertext = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        try:
            AESGCM(_derive(self._current, purpose)).decrypt(nonce, ciphertext, row_id.bytes)
        except InvalidTag:
            return True
        return False
