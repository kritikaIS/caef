"""M17 — signed firmware packages (RESEARCH.md §9).

The v0.1 OTA path accepted anything whose content hash matched the hash the
sender supplied, which authenticates nothing. These tests pin the correction:
each named rejection is exercised, and the accept path is exercised too, so a
"reject everything" implementation could not pass this file.
"""

import pytest

import config
from server.compiler.compiler import compile_manifest
from server.manifest.registry import load_registry
from server.ota import keys
from server.ota import package as ota
from server.ota.package import Rejection
from server.schemas import load_hardware_schema
from tests.fixtures import manifests

BASE_HASH = manifests.COOLING["current_firmware_hash"]
DEVICE = "pi_node_alpha"
ISSUED_AT = 1770000000


@pytest.fixture
def registry():
    return load_registry("1.0.0")


@pytest.fixture
def schema():
    return load_hardware_schema(DEVICE)


@pytest.fixture
def program(registry, schema):
    return compile_manifest(manifests.cooling(), registry, schema).program


@pytest.fixture
def key():
    return b"\x01" * 32


@pytest.fixture
def package(program, key):
    return ota.build_package(
        program, sequence_number=5, issued_at=ISSUED_AT, lease_duration_seconds=300, key=key
    )


def check(package, key, device=DEVICE, base=BASE_HASH, last_sequence=0):
    return ota.verify_package(package, device, base, last_sequence, key=key)


# --- the accept path ---------------------------------------------------------


def test_a_well_formed_package_is_accepted(package, key):
    verdict = check(package, key)
    assert verdict.accepted is True
    assert verdict.rejection is None
    assert verdict.sequence_number == 5


def test_signing_is_deterministic(program, key):
    first = ota.build_package(program, 5, ISSUED_AT, 300, key=key)
    second = ota.build_package(program, 5, ISSUED_AT, 300, key=key)
    assert first.signature == second.signature


def test_a_durable_install_carries_no_lease(program, key):
    """An auto-patch is meant to persist; only a morph expires (LOOPS.md §4.5)."""
    package = ota.build_package(program, 5, ISSUED_AT, lease_duration_seconds=None, key=key)
    assert check(package, key).accepted is True
    assert package.lease_duration_seconds is None


# --- signature ---------------------------------------------------------------


def test_an_unsigned_package_is_rejected(package, key):
    assert check(package.model_copy(update={"signature": ""}), key).rejection is (
        Rejection.INVALID_SIGNATURE
    )


def test_a_package_signed_with_another_key_is_rejected(program, key):
    stranger = ota.build_package(program, 5, ISSUED_AT, 300, key=b"\x02" * 32)
    assert check(stranger, key).rejection is Rejection.INVALID_SIGNATURE


def test_changing_any_signed_field_invalidates_the_signature(package, key):
    """The signature covers the whole package, not a subset of it."""
    for field, value in [
        ("sequence_number", 6),
        ("lease_duration_seconds", 60),
        ("issued_at", ISSUED_AT + 1),
        ("base_firmware_hash", "0" * 16),
        ("capability_registry_version", "1.0.0"),
        ("package_version", "9.9"),
    ]:
        mutated = package.model_copy(update={field: value})
        if mutated.model_dump() == package.model_dump():
            continue  # the value was already what it is
        assert not ota.signature_matches(mutated, key), f"{field} is outside the signature"


def test_hash_verification_alone_is_not_authorization(program, key):
    """The v0.1 failure mode, stated as a test: a package whose artifact hashes
    correctly but carries no valid signature is still refused."""
    forged = ota.FirmwarePackage(
        package_version=config.PACKAGE_VERSION,
        device_id=DEVICE,
        manifest_hash=program.manifest_hash,
        artifact_hash=program.artifact_hash,  # correct hash …
        base_firmware_hash=program.source_firmware_hash,
        capability_registry_version=program.capability_registry_version,
        sequence_number=99,
        lease_duration_seconds=60,
        issued_at=ISSUED_AT,
        artifact=program,
        signature="f" * 64,  # … and a signature the attacker made up
    )
    assert check(forged, key).rejection is Rejection.INVALID_SIGNATURE


# --- integrity ---------------------------------------------------------------


def test_a_modified_artifact_is_rejected(package, key):
    tampered = package.model_copy(
        update={"artifact": package.artifact.model_copy(update={"maximum_duration_seconds": 9999})}
    )
    assert check(tampered, key).rejection is Rejection.ARTIFACT_MISMATCH


def test_an_artifact_from_a_different_manifest_is_rejected(package, key, program):
    swapped = package.model_copy(
        update={"artifact": program.model_copy(update={"manifest_hash": "a" * 64})}
    )
    assert check(swapped, key).rejection in (
        Rejection.ARTIFACT_MISMATCH,
        Rejection.MANIFEST_MISMATCH,
    )


def test_a_relabelled_artifact_hash_is_rejected(package, key):
    """Re-hashing the tampered artifact defeats the consistency check and then
    fails the signature instead. Both paths end in a rejection."""
    tampered_artifact = package.artifact.model_copy(update={"maximum_duration_seconds": 9999})
    relabelled = package.model_copy(
        update={
            "artifact": tampered_artifact,
            "artifact_hash": tampered_artifact.artifact_hash,
            "manifest_hash": tampered_artifact.manifest_hash,
        }
    )
    assert check(relabelled, key).rejection is Rejection.INVALID_SIGNATURE


# --- identity and freshness --------------------------------------------------


def test_a_package_for_another_device_is_rejected(package, key):
    assert check(package, key, device="pi_node_beta").rejection is Rejection.WRONG_DEVICE


def test_an_artifact_targeting_another_device_is_rejected(program, key):
    """The envelope and the artifact must agree; signing both is what makes
    checking both meaningful."""
    other = program.model_copy(update={"device_id": "pi_node_beta"})
    package = ota.build_package(other, 5, ISSUED_AT, 300, key=key)
    assert check(package, key, device=DEVICE).rejection is Rejection.WRONG_DEVICE


def test_a_stale_base_firmware_hash_is_rejected(package, key):
    """The package was reasoned about from a device state that no longer exists."""
    assert check(package, key, base="0" * 16).rejection is Rejection.STALE_BASE_FIRMWARE


def test_a_replayed_sequence_number_is_rejected(package, key):
    assert check(package, key, last_sequence=5).rejection is Rejection.REPLAYED_SEQUENCE
    assert check(package, key, last_sequence=6).rejection is Rejection.REPLAYED_SEQUENCE
    assert check(package, key, last_sequence=4).accepted is True


def test_a_device_with_no_firmware_yet_accepts_any_base(package, key):
    """First provisioning: the device has nothing to be stale against."""
    assert check(package, key, base=None).accepted is True


# --- local policy ------------------------------------------------------------


def test_an_excessive_lease_is_rejected_by_the_device(program, key):
    package = ota.build_package(
        program, 5, ISSUED_AT, lease_duration_seconds=config.MAX_LEASE_SECONDS + 1, key=key
    )
    assert check(package, key).rejection is Rejection.LEASE_TOO_LONG


def test_the_device_applies_its_own_ceiling_not_the_servers(program, key):
    """A device may be stricter than the fleet default, and its own limit wins."""
    package = ota.build_package(program, 5, ISSUED_AT, lease_duration_seconds=300, key=key)
    verdict = ota.verify_package(package, DEVICE, BASE_HASH, 0, key=key, max_lease_seconds=60)
    assert verdict.rejection is Rejection.LEASE_TOO_LONG


def test_an_unsupported_registry_version_is_rejected(program, key):
    package = ota.build_package(
        program.model_copy(update={"capability_registry_version": "42.0.0"}),
        5,
        ISSUED_AT,
        300,
        key=key,
    )
    assert check(package, key).rejection is Rejection.UNSUPPORTED_REGISTRY


# --- key handling ------------------------------------------------------------


def test_a_configured_key_is_used_over_an_ephemeral_one(monkeypatch):
    keys.reset_ephemeral_key()
    monkeypatch.setenv(keys.KEY_ENV_VAR, "ab" * 32)
    assert keys.signing_key() == bytes.fromhex("ab" * 32)


def test_a_passphrase_is_stretched_to_a_full_length_key(monkeypatch):
    keys.reset_ephemeral_key()
    monkeypatch.setenv(keys.KEY_ENV_VAR, "not-hex-at-all")
    assert len(keys.signing_key()) == 32


def test_an_ephemeral_key_is_stable_within_a_process(monkeypatch):
    keys.reset_ephemeral_key()
    monkeypatch.delenv(keys.KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "OTA_HMAC_KEY", "")
    try:
        assert keys.signing_key() == keys.signing_key()
    finally:
        keys.reset_ephemeral_key()


def test_ephemeral_keys_can_be_refused(monkeypatch):
    """A multi-process deployment must fail loudly rather than sign with a key
    the device cannot possibly have."""
    keys.reset_ephemeral_key()
    monkeypatch.delenv(keys.KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "OTA_HMAC_KEY", "")
    monkeypatch.setattr(config, "OTA_ALLOW_EPHEMERAL_KEY", False)
    with pytest.raises(keys.MissingSigningKey):
        keys.signing_key()


def test_no_key_material_is_hardcoded():
    from pathlib import Path

    for path in (Path("server/ota/keys.py"), Path("server/ota/package.py")):
        source = path.read_text()
        assert "secrets.token_bytes" in source or "hmac" in source
        # No 32+ character hex literal anywhere: a checked-in key is a checked-in
        # compromise, and this is the cheapest possible tripwire.
        import re

        assert not re.search(r"['\"][0-9a-fA-F]{32,}['\"]", source), f"{path} may embed a key"
