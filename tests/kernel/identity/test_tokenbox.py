"""Encryption of stored IdP tokens and PKCE verifiers (ADR-003 §4, H2)."""

import uuid

import pytest
from pydantic import SecretStr

from atlas.kernel.identity import tokenbox
from atlas.kernel.identity.tokenbox import NONCE_BYTES, Purpose, TokenBox, TokenBoxError

KEY_A = SecretStr("k3Y-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop-A")
KEY_B = SecretStr("k3Y-ZYXWVUTSRQPONMLKJIHGFEDCBAabcdefghijklmnop-B")
ROW = uuid.UUID("0194f1c0-0000-7000-8000-000000000001")
OTHER_ROW = uuid.UUID("0194f1c0-0000-7000-8000-000000000002")
PLAIN = b"refresh-token-value-0123456789"


def test_round_trip() -> None:
    box = TokenBox(KEY_A)
    blob = box.seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    assert box.open(Purpose.REFRESH_TOKEN, ROW, blob) == PLAIN


def test_the_blob_is_versioned_and_hides_the_plaintext() -> None:
    blob = TokenBox(KEY_A).seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    assert blob[0] == 1
    assert len(blob) == 1 + NONCE_BYTES + len(PLAIN) + 16  # version, nonce, ciphertext, tag
    assert PLAIN not in blob


def test_every_encryption_uses_a_fresh_nonce() -> None:
    box = TokenBox(KEY_A)
    blobs = {box.seal(Purpose.REFRESH_TOKEN, ROW, PLAIN) for _ in range(50)}
    assert len(blobs) == 50
    assert len({b[1 : 1 + NONCE_BYTES] for b in blobs}) == 50


def test_a_ciphertext_cannot_be_moved_to_another_row() -> None:
    box = TokenBox(KEY_A)
    blob = box.seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    with pytest.raises(TokenBoxError):
        box.open(Purpose.REFRESH_TOKEN, OTHER_ROW, blob)


def test_a_ciphertext_cannot_be_used_for_another_purpose() -> None:
    box = TokenBox(KEY_A)
    blob = box.seal(Purpose.PKCE_VERIFIER, ROW, PLAIN)
    for purpose in (Purpose.REFRESH_TOKEN, Purpose.ID_TOKEN_HINT):
        with pytest.raises(TokenBoxError):
            box.open(purpose, ROW, blob)
    assert box.open(Purpose.PKCE_VERIFIER, ROW, blob) == PLAIN


def test_the_labels_derive_different_keys() -> None:
    keys = {tokenbox._derive(KEY_A, purpose) for purpose in Purpose}
    assert len(keys) == len(Purpose)
    assert all(len(k) == 32 for k in keys)
    assert tokenbox._derive(KEY_A, Purpose.REFRESH_TOKEN) != tokenbox._derive(
        KEY_B, Purpose.REFRESH_TOKEN
    )


@pytest.mark.parametrize("where", ["version", "nonce", "ciphertext", "tag"])
def test_any_change_to_the_blob_is_refused(where: str) -> None:
    box = TokenBox(KEY_A)
    blob = bytearray(box.seal(Purpose.REFRESH_TOKEN, ROW, PLAIN))
    index = {"version": 0, "nonce": 1, "ciphertext": 1 + NONCE_BYTES, "tag": len(blob) - 1}[where]
    blob[index] ^= 0x01
    with pytest.raises(TokenBoxError):
        box.open(Purpose.REFRESH_TOKEN, ROW, bytes(blob))


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x01" + b"\x00" * 20, b"\x02" + b"\x00" * 40])
def test_junk_is_refused(blob: bytes) -> None:
    with pytest.raises(TokenBoxError):
        TokenBox(KEY_A).open(Purpose.REFRESH_TOKEN, ROW, blob)


def test_the_previous_key_only_decrypts() -> None:
    old = TokenBox(KEY_B).seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    rotated = TokenBox(KEY_A, previous=KEY_B)
    # A blob of the old key still opens...
    assert rotated.open(Purpose.REFRESH_TOKEN, ROW, old) == PLAIN
    assert rotated.needs_reseal(Purpose.REFRESH_TOKEN, ROW, old) is True
    # ...but whatever the rotated box seals is under the current key alone.
    fresh = rotated.seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    assert rotated.needs_reseal(Purpose.REFRESH_TOKEN, ROW, fresh) is False
    assert TokenBox(KEY_A).open(Purpose.REFRESH_TOKEN, ROW, fresh) == PLAIN
    with pytest.raises(TokenBoxError):
        TokenBox(KEY_B).open(Purpose.REFRESH_TOKEN, ROW, fresh)


def test_without_the_previous_key_old_blobs_are_unreadable() -> None:
    old = TokenBox(KEY_B).seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    with pytest.raises(TokenBoxError):
        TokenBox(KEY_A).open(Purpose.REFRESH_TOKEN, ROW, old)


def test_nothing_shows_a_key_or_a_value() -> None:
    box = TokenBox(KEY_A, previous=KEY_B)
    for text in (repr(box), str(box), f"{box}", f"{box!r}"):
        assert KEY_A.get_secret_value() not in text
        assert KEY_B.get_secret_value() not in text
        assert "redacted" in text
    blob = box.seal(Purpose.REFRESH_TOKEN, ROW, PLAIN)
    with pytest.raises(TokenBoxError) as caught:
        box.open(Purpose.REFRESH_TOKEN, OTHER_ROW, blob)
    message = f"{caught.value!s} {caught.value!r}"
    assert PLAIN.decode() not in message
    assert KEY_A.get_secret_value() not in message
    assert "could not be decrypted" in message
