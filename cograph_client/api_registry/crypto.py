"""Pluggable secret cipher for tenant-custom API credentials (ONTA-2xx, Child 2).

A tenant-custom API source may need a private credential (an API key / bearer
token for the tenant's own internal API). Those secrets are **envelope-encrypted
at rest** in the durable store and decrypted **only at call time** inside the
executor. This module is the cipher seam.

Two implementations, mirroring the layer split:

- ``LocalAesGcmCipher`` — the OSS default. A single local symmetric key from the
  environment (``OMNIX_SECRETS_KEY``), used with AES-256-GCM (authenticated
  encryption). Works for any self-hoster with one env var — no cloud dependency.
- A **KMS-backed cipher** is the premium binding: the deployed image registers an
  AWS-KMS data-key cipher via :func:`register_secret_cipher` (the same plugin
  shape as ``register_adapter`` / ``register_api_source_layer``). That code lives
  in the proprietary ``cograph/`` tree — this OSS module never imports it.

Ciphertext format (opaque to callers): ``v1.<scheme>.<base64url payload>``. The
scheme tag lets a future/premium cipher be distinguished at decrypt time and lets
us migrate schemes without ambiguity. The OSS scheme is ``aesgcm``.

Boundary: OSS. Imports stdlib + ``cryptography`` only — no ``from cograph.*`` and
no cloud-provider identifiers. ``cryptography`` is a first-party dependency
(pyproject); a deployment that wants KMS registers it over this seam.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

_FORMAT_VERSION = "v1"
_OSS_SCHEME = "aesgcm"


class SecretCipherError(Exception):
    """Raised when encryption/decryption fails (bad key, tampered ciphertext,
    unknown scheme). Never carries plaintext or key material in its message."""


# --------------------------------------------------------------------------- #
# The cipher protocol
# --------------------------------------------------------------------------- #
class SecretCipher(Protocol):
    #: Short scheme tag stamped into the ciphertext envelope (e.g. "aesgcm",
    #: "kms"). Used to route decryption back to the right cipher.
    scheme: str

    def encrypt(self, plaintext: str, *, aad: str = "") -> str:
        """Return an opaque ciphertext string. ``aad`` (additional authenticated
        data) binds the ciphertext to a context (e.g. ``tenant/slug/name``) so it
        can't be moved to a different tenant/slot and still decrypt."""
        ...

    def decrypt(self, token: str, *, aad: str = "") -> str:
        """Reverse :meth:`encrypt`. Must raise ``SecretCipherError`` on any
        integrity failure — never return a partial/garbled plaintext."""
        ...


# --------------------------------------------------------------------------- #
# Envelope framing (shared by every cipher)
# --------------------------------------------------------------------------- #
def _frame(scheme: str, payload: bytes) -> str:
    return f"{_FORMAT_VERSION}.{scheme}.{base64.urlsafe_b64encode(payload).decode('ascii')}"


def _unframe(token: str) -> tuple[str, bytes]:
    try:
        version, scheme, b64 = token.split(".", 2)
    except ValueError as exc:
        raise SecretCipherError("malformed ciphertext envelope") from exc
    if version != _FORMAT_VERSION:
        raise SecretCipherError(f"unsupported ciphertext version {version!r}")
    try:
        payload = base64.urlsafe_b64decode(b64.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise SecretCipherError("corrupt ciphertext payload") from exc
    return scheme, payload


def ciphertext_scheme(token: str) -> str:
    """The scheme tag of an envelope (for routing / diagnostics). Never decrypts."""
    return _unframe(token)[0]


# --------------------------------------------------------------------------- #
# OSS default: AES-256-GCM with a local key
# --------------------------------------------------------------------------- #
class LocalAesGcmCipher:
    """AES-256-GCM using one local symmetric key from ``OMNIX_SECRETS_KEY``.

    The env var holds the key as either base64/base64url (any length that decodes
    to 16/24/32 bytes) or raw text (any length); a non-32-byte key is stretched to
    exactly 32 bytes via SHA-256 so operators can paste an arbitrary passphrase.
    A random 12-byte nonce is generated per encryption and stored alongside the
    ciphertext; GCM authenticates both the ciphertext and the ``aad``.
    """

    scheme = _OSS_SCHEME
    _NONCE_LEN = 12

    def __init__(self, key: bytes) -> None:
        if not key:
            raise SecretCipherError("empty secrets key")
        # Normalize to a 32-byte AES-256 key. A key that is already exactly 16/24/
        # 32 bytes is used as-is; anything else is hashed to 32 bytes (deterministic
        # so the same passphrase always yields the same key).
        self._key = key if len(key) in (16, 24, 32) else hashlib.sha256(key).digest()

    @classmethod
    def from_env(cls, env_value: str) -> "LocalAesGcmCipher":
        raw = (env_value or "").strip()
        if not raw:
            raise SecretCipherError("OMNIX_SECRETS_KEY is empty")
        key = _decode_key(raw)
        return cls(key)

    def encrypt(self, plaintext: str, *, aad: str = "") -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(self._NONCE_LEN)
        ct = AESGCM(self._key).encrypt(
            nonce, plaintext.encode("utf-8"), aad.encode("utf-8")
        )
        return _frame(self.scheme, nonce + ct)

    def decrypt(self, token: str, *, aad: str = "") -> str:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        scheme, payload = _unframe(token)
        if scheme != self.scheme:
            raise SecretCipherError(
                f"ciphertext scheme {scheme!r} not handled by {self.scheme!r} cipher"
            )
        if len(payload) < self._NONCE_LEN + 1:
            raise SecretCipherError("ciphertext too short")
        nonce, ct = payload[: self._NONCE_LEN], payload[self._NONCE_LEN :]
        try:
            pt = AESGCM(self._key).decrypt(nonce, ct, aad.encode("utf-8"))
        except InvalidTag as exc:
            # Wrong key OR tampered ciphertext OR mismatched aad. Do NOT leak which.
            raise SecretCipherError("secret decryption failed (bad key or tampered)") from exc
        return pt.decode("utf-8")


def _decode_key(raw: str) -> bytes:
    """Decode the env key. Try base64url then standard base64 (if the decoded
    length is a valid AES key size); otherwise treat the string as a raw
    passphrase (UTF-8 bytes, hashed to 32 bytes by the cipher)."""
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            candidate = decoder(_pad_b64(raw))
        except Exception:  # noqa: BLE001
            continue
        if len(candidate) in (16, 24, 32):
            return candidate
    return raw.encode("utf-8")


def _pad_b64(s: str) -> bytes:
    # base64 requires length % 4 == 0; pad so an unpadded env value still decodes.
    b = s.encode("ascii", errors="ignore")
    return b + b"=" * (-len(b) % 4)


# --------------------------------------------------------------------------- #
# Cipher selection + plugin seam
# --------------------------------------------------------------------------- #
_registered_cipher: Optional[SecretCipher] = None
_cipher_singleton: Optional[SecretCipher] = None


def register_secret_cipher(cipher: SecretCipher) -> None:
    """Register the process cipher (premium KMS binding calls this at startup).

    Overrides the OSS default. Idempotent — the last registration wins. Resets the
    memoized selection so the next :func:`get_secret_cipher` returns the new one.
    """
    global _registered_cipher, _cipher_singleton
    _registered_cipher = cipher
    _cipher_singleton = None
    logger.info("api_registry: secret cipher registered (scheme=%s)", getattr(cipher, "scheme", "?"))


def get_secret_cipher() -> Optional[SecretCipher]:
    """Return the active cipher, or ``None`` if secret storage is not configured.

    Precedence: a registered cipher (premium KMS) wins; otherwise the OSS
    ``LocalAesGcmCipher`` is built from ``OMNIX_SECRETS_KEY`` if that env var is
    set. ``None`` means "no cipher available" — the routes must then REFUSE to
    store a secret (fail closed) rather than store it in the clear.
    """
    global _cipher_singleton
    if _cipher_singleton is not None:
        return _cipher_singleton
    if _registered_cipher is not None:
        _cipher_singleton = _registered_cipher
        return _cipher_singleton
    env_key = os.environ.get("OMNIX_SECRETS_KEY", "").strip()
    if env_key:
        _cipher_singleton = LocalAesGcmCipher.from_env(env_key)
        return _cipher_singleton
    return None


def reset_secret_cipher() -> None:
    """Test helper — clear the registered + memoized cipher."""
    global _registered_cipher, _cipher_singleton
    _registered_cipher = None
    _cipher_singleton = None


__all__ = [
    "SecretCipher",
    "SecretCipherError",
    "LocalAesGcmCipher",
    "register_secret_cipher",
    "get_secret_cipher",
    "reset_secret_cipher",
    "ciphertext_scheme",
]
