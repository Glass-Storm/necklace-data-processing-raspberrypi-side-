"""Drift guard: the vendored contract must match the hub's proto exactly.

The proto is FROZEN and vendored byte-identically from the hub. This test
re-derives the hub proto's sha256 from the sibling hub checkout (a *live*
comparison, not a stored self-hash) and asserts it equals the vendored copy.
When the sibling checkout is absent the test SKIPS with a named reason — it
never silently passes, so a skipped run is visibly "unverified".
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED_PROTO = REPO_ROOT / "proto" / "ecosys" / "v1" / "ecosys.proto"

HUB_REPO = Path(os.environ.get("PI_HUB_REPO", str(REPO_ROOT.parent / "phone-manager")))
HUB_PROTO = (
    HUB_REPO / "contract" / "src" / "main" / "proto" / "ecosys" / "v1" / "ecosys.proto"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendored_proto_exists() -> None:
    assert VENDORED_PROTO.is_file(), f"vendored proto missing: {VENDORED_PROTO}"


def test_vendored_proto_matches_recorded_hash() -> None:
    """The vendored proto must match the sha256 recorded in PROTO_SOURCE."""
    recorded: dict[str, str] = {}
    source_file = REPO_ROOT / "proto" / "PROTO_SOURCE"
    for line in source_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            recorded[key.strip()] = value.strip()

    assert "proto_sha256" in recorded, "PROTO_SOURCE has no proto_sha256 line"
    assert recorded["proto_sha256"] == _sha256(VENDORED_PROTO), (
        "vendored proto no longer matches the hash recorded in PROTO_SOURCE; "
        "re-vendor from the hub instead of editing either file"
    )


def test_vendored_proto_matches_hub_checkout() -> None:
    """Live drift check against the sibling hub checkout (source of truth)."""
    if not HUB_PROTO.is_file():
        pytest.skip(
            f"hub proto not present at {HUB_PROTO}: cannot verify contract drift"
        )

    hub_hash = _sha256(HUB_PROTO)
    vendored_hash = _sha256(VENDORED_PROTO)
    assert vendored_hash == hub_hash, (
        "vendored proto has DRIFTED from the hub contract:\n"
        f"  hub      ({HUB_PROTO}) = {hub_hash}\n"
        f"  vendored ({VENDORED_PROTO}) = {vendored_hash}\n"
        "re-vendor with: cp <hub proto> proto/ecosys/v1/ecosys.proto"
    )


def test_generated_stubs_import_and_expose_contract() -> None:
    """The committed stubs must import and expose the frozen symbols."""
    from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc

    assert ecosys_pb2.DEVICE_ROLE_DAEMON == 2
    assert [f.name for f in ecosys_pb2.PairRequest.DESCRIPTOR.fields] == [
        "pin",
        "device_name",
        "role",
    ]
    assert [f.name for f in ecosys_pb2.StreamFrame.DESCRIPTOR.fields] == [
        "audio_pcm16_16k",
        "video_h264_nal",
        "transcript",
        "result",
    ]
    assert [f.name for f in ecosys_pb2.StreamResult.DESCRIPTOR.fields] == [
        "text",
        "speaker_label",
        "pts_ms",
    ]
    assert hasattr(ecosys_pb2_grpc, "PairingServiceStub")
    assert hasattr(ecosys_pb2_grpc, "StreamServiceStub")
