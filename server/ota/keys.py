"""OTA signing key material (RESEARCH.md §9).

The prototype uses one symmetric HMAC-SHA256 key shared by the server and the
device. That is a deliberate, documented downgrade from what production would
need, and the limitation is worth stating plainly rather than burying:

  - Anyone holding the key can mint a package the device will accept. There is
    no notion of *which* server signed something, only that someone with the key
    did.
  - A real deployment needs an asymmetric device identity — the device holding a
    public key and the signer holding a private one in secure storage — so a
    compromised device cannot forge updates for its fleet.

The key is read from the environment and is never written to disk, never logged
and never checked in. If none is configured, a single-process run may mint an
ephemeral one so the demo and the test suite work with no setup; that key exists
only in memory, so the moment the server and the device are separate processes
the environment variable becomes mandatory and its absence is an error rather
than a silent downgrade.
"""

import hashlib
import logging
import os
import secrets

import config

log = logging.getLogger("caef.ota.keys")

KEY_ENV_VAR = "CAEF_OTA_HMAC_KEY"

# Minted at most once per process, and only when no key is configured.
_ephemeral_key: bytes | None = None


class MissingSigningKey(RuntimeError):
    """No key is configured and ephemeral keys are disabled."""


def _derive(material: str) -> bytes:
    """Accept either raw hex or a passphrase.

    A passphrase is hashed rather than used verbatim so a short one still yields
    a full-length key. This is not a password-hashing KDF and is not a substitute
    for a random key — the documented way to produce one is
    `python -c 'import secrets; print(secrets.token_hex(32))'`.
    """
    text = material.strip()
    try:
        return bytes.fromhex(text)
    except ValueError:
        return hashlib.sha256(text.encode()).digest()


def signing_key() -> bytes:
    """The key used to sign and to verify. Same value on both sides."""
    global _ephemeral_key

    configured = os.getenv(KEY_ENV_VAR) or config.OTA_HMAC_KEY
    if configured:
        return _derive(configured)

    if not config.OTA_ALLOW_EPHEMERAL_KEY:
        raise MissingSigningKey(
            f"{KEY_ENV_VAR} is not set and ephemeral keys are disabled; refusing to "
            "sign or verify firmware packages"
        )

    if _ephemeral_key is None:
        _ephemeral_key = secrets.token_bytes(32)
        log.warning(
            "%s is not set; minted an ephemeral in-memory signing key. This works only "
            "while the signer and the device are the same process — set %s for anything "
            "else.",
            KEY_ENV_VAR,
            KEY_ENV_VAR,
        )
    return _ephemeral_key


def reset_ephemeral_key() -> None:
    """Drop the process-local ephemeral key. For tests that simulate a device
    which never received the server's key."""
    global _ephemeral_key
    _ephemeral_key = None
