"""Signed firmware package and the device's acceptance rules (RESEARCH.md §9).

The v0.1 OTA path checked one thing: that `sha256(code)` matched the hash the
sender claimed. That authenticates nothing — anyone who can reach the port can
send code and a matching hash. **Hash verification is not authorization**, and
this module is the correction.

A package binds an artifact to a device, an origin, a point in a sequence and a
bounded lease. The device rejects, by name:

    invalid_signature        not signed with the shared key
    wrong_device             addressed to a different device
    artifact_mismatch        the artifact does not hash to its declared hash
    manifest_mismatch        the artifact was compiled from a different manifest
    stale_base_firmware      built against firmware this device is no longer running
    replayed_sequence        a sequence number at or below one already accepted
    lease_too_long           a lease beyond the device's own configured ceiling
    unsupported_registry     a capability registry version this device cannot serve
    malformed_package        not a package at all

Check order is internal consistency first, then the signature, then identity and
freshness. A tampered artifact fails the signature too, but "artifact_mismatch"
tells an operator what actually happened and "invalid_signature" does not.
"""

import hmac
import logging
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import config
from server.compiler.program import ControllerProgram
from server.manifest.canonical import canonical_bytes, content_hash
from server.manifest.registry import is_supported_version
from server.ota import keys

log = logging.getLogger("caef.ota")

SIGNATURE_FIELD = "signature"


class Rejection(StrEnum):
    INVALID_SIGNATURE = "invalid_signature"
    WRONG_DEVICE = "wrong_device"
    ARTIFACT_MISMATCH = "artifact_mismatch"
    MANIFEST_MISMATCH = "manifest_mismatch"
    STALE_BASE_FIRMWARE = "stale_base_firmware"
    REPLAYED_SEQUENCE = "replayed_sequence"
    LEASE_TOO_LONG = "lease_too_long"
    UNSUPPORTED_REGISTRY = "unsupported_registry"
    MALFORMED_PACKAGE = "malformed_package"


class FirmwarePackage(BaseModel):
    """DATA_SCHEMAS.md §17. Frozen: a package is evidence, not a working buffer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package_version: str
    device_id: str
    manifest_hash: str
    artifact_hash: str
    base_firmware_hash: str
    capability_registry_version: str
    sequence_number: int = Field(ge=0)
    # None means a durable install (an auto-patch); an integer is a temporary
    # morph that the device expires locally (RESEARCH.md §8).
    lease_duration_seconds: int | None = None
    issued_at: int
    artifact: ControllerProgram
    signature: str = ""

    def signing_payload(self) -> dict:
        """Everything the signature covers: the whole package but the signature.

        The artifact is included in full rather than by hash. Covering the hash
        alone would leave the bytes unsigned, which is exactly the gap the v0.1
        integrity check had.
        """
        payload = self.model_dump(mode="json")
        payload.pop(SIGNATURE_FIELD, None)
        return payload

    def signed_bytes(self) -> bytes:
        return canonical_bytes(self.signing_payload())


class PackageVerdict(BaseModel):
    accepted: bool
    reason: str
    rejection: Rejection | None = None
    device_id: str
    artifact_hash: str
    sequence_number: int

    @property
    def status(self) -> Literal["accepted", "rejected"]:
        return "accepted" if self.accepted else "rejected"


def sign(package: FirmwarePackage, key: bytes | None = None) -> FirmwarePackage:
    """Return a copy carrying its signature."""
    signing_key = key or keys.signing_key()
    signature = hmac.new(signing_key, package.signed_bytes(), sha256).hexdigest()
    return package.model_copy(update={SIGNATURE_FIELD: signature})


def build_package(
    program: ControllerProgram,
    sequence_number: int,
    issued_at: int,
    lease_duration_seconds: int | None = None,
    key: bytes | None = None,
) -> FirmwarePackage:
    """Assemble and sign a package for a compiled artifact.

    `issued_at` is passed in rather than read from the clock here, so a caller
    that needs a reproducible package (the experiment harness, a test) can have
    one without this module deciding what time it is.
    """
    unsigned = FirmwarePackage(
        package_version=config.PACKAGE_VERSION,
        device_id=program.device_id,
        manifest_hash=program.manifest_hash,
        artifact_hash=program.artifact_hash,
        base_firmware_hash=program.source_firmware_hash,
        capability_registry_version=program.capability_registry_version,
        sequence_number=sequence_number,
        lease_duration_seconds=lease_duration_seconds,
        issued_at=issued_at,
        artifact=program,
    )
    return sign(unsigned, key)


def signature_matches(package: FirmwarePackage, key: bytes | None = None) -> bool:
    signing_key = key or keys.signing_key()
    expected = hmac.new(signing_key, package.signed_bytes(), sha256).hexdigest()
    # Constant-time compare. The margin it buys against a remote timing attack
    # on an HMAC is thin, but there is no reason to hand it away.
    return hmac.compare_digest(expected, package.signature or "")


def verify_package(
    package: FirmwarePackage,
    device_id: str,
    current_firmware_hash: str | None,
    last_accepted_sequence: int,
    key: bytes | None = None,
    max_lease_seconds: int | None = None,
) -> PackageVerdict:
    """The device's acceptance decision. Deterministic, local, offline.

    No server is contacted and no model is consulted: a device must be able to
    decide whether to trust an update while it is the only thing running
    (RESEARCH.md §9).
    """

    def reject(rejection: Rejection, reason: str) -> PackageVerdict:
        log.warning("rejecting package seq=%s: %s", package.sequence_number, reason)
        return PackageVerdict(
            accepted=False,
            reason=reason,
            rejection=rejection,
            device_id=package.device_id,
            artifact_hash=package.artifact_hash,
            sequence_number=package.sequence_number,
        )

    # 1. Internal consistency, so a tampered package is named for what it is.
    actual_artifact_hash = content_hash(package.artifact.model_dump(mode="json"))
    if actual_artifact_hash != package.artifact_hash:
        return reject(
            Rejection.ARTIFACT_MISMATCH,
            f"artifact hashes to {actual_artifact_hash[:16]}, package claims "
            f"{package.artifact_hash[:16]}",
        )
    if package.artifact.manifest_hash != package.manifest_hash:
        return reject(
            Rejection.MANIFEST_MISMATCH,
            "the artifact was compiled from a different manifest than the package declares",
        )

    # 2. Origin.
    if not signature_matches(package, key):
        return reject(Rejection.INVALID_SIGNATURE, "signature does not verify under this key")

    # 3. Identity.
    if package.device_id != device_id:
        return reject(
            Rejection.WRONG_DEVICE,
            f"package addresses {package.device_id!r}, this device is {device_id!r}",
        )
    if package.artifact.device_id != device_id:
        return reject(
            Rejection.WRONG_DEVICE,
            f"artifact targets {package.artifact.device_id!r}, this device is {device_id!r}",
        )

    # 4. Freshness. A package built against firmware this device is no longer
    #    running was reasoned about from a state that no longer exists.
    if current_firmware_hash is not None and package.base_firmware_hash != current_firmware_hash:
        return reject(
            Rejection.STALE_BASE_FIRMWARE,
            f"built against base {package.base_firmware_hash[:16]}, device is running "
            f"{current_firmware_hash[:16]}",
        )
    if package.sequence_number <= last_accepted_sequence:
        return reject(
            Rejection.REPLAYED_SEQUENCE,
            f"sequence {package.sequence_number} is not above the last accepted "
            f"{last_accepted_sequence}",
        )

    # 5. Local policy. The device's own ceiling, not the server's.
    ceiling = config.MAX_LEASE_SECONDS if max_lease_seconds is None else max_lease_seconds
    lease = package.lease_duration_seconds
    if lease is not None and (lease <= 0 or lease > ceiling):
        return reject(
            Rejection.LEASE_TOO_LONG,
            f"lease {lease}s is outside this device's permitted (0, {ceiling}]",
        )
    if not is_supported_version(package.capability_registry_version):
        return reject(
            Rejection.UNSUPPORTED_REGISTRY,
            f"capability registry {package.capability_registry_version} is not supported here",
        )

    return PackageVerdict(
        accepted=True,
        reason="package verified",
        device_id=package.device_id,
        artifact_hash=package.artifact_hash,
        sequence_number=package.sequence_number,
    )
