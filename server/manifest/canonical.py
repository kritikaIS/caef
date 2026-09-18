"""Canonical serialization — identical manifests must produce identical bytes.

Every hash in the contract-constrained pipeline (`manifest_hash`,
`artifact_hash`, the registry's content hash, the bytes an OTA signature covers)
is taken over the output of `canonical_json`. If two components disagreed on
key order or float formatting, a signature verified on the server would fail on
the device for no reason other than serialization — so there is exactly one
implementation and everything routes through it (RESEARCH.md §2).

Rules, in order of why they matter:

  - keys sorted, so field order in the model or the wire payload is irrelevant;
  - no insignificant whitespace, so a pretty-printer cannot change a hash;
  - ASCII-escaped, so a non-UTF-8 transport cannot change a hash;
  - NaN/Infinity rejected, because they are not JSON and compare unequal to
    themselves — a value that breaks determinism must not reach a hash;
  - `-0.0` normalised to `0.0`, the one float pair that is `==` but serialises
    differently.
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel

# All content hashes in this pipeline are full SHA-256 hex. The v0.1 `fw_hash`
# stays 16 chars for wire compatibility with the baseline; the two are
# deliberately different lengths so a reader can tell them apart at a glance.
HASH_ALGORITHM = "sha256"


def _normalise(value: Any) -> Any:
    """Recursively coerce a payload into canonical-safe primitives."""
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite float {value!r} cannot be canonicalised")
        return 0.0 if value == 0.0 else value
    return value


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _normalise(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def content_hash(payload: Any) -> str:
    """Full SHA-256 hex of the canonical bytes."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
