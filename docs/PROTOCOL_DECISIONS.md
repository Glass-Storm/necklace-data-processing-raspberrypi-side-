# Protocol decisions (frozen)

These are the wire-contract and hub-behaviour facts the Pi client is built
against. They are FROZEN: the Pi vendors the proto byte-identically
(`proto/ecosys/v1/ecosys.proto`, sha256
`a09d830989898908cd4073c52658acff7aa0302f10f254576618bf27662d7c3b`) and MUST NOT
change the contract locally.

Provenance of the contract: see `proto/PROTO_SOURCE` (hub repo `phone-manager`,
`contract/src/main/proto/ecosys/v1/ecosys.proto`, source commit `0d454ce`).
Drift is detected by `tests/test_proto_drift.py`, which re-hashes the proto in
the sibling hub checkout.

## (a) The stream is LONG-LIVED

`StreamService/OpenStream` is a bidirectional stream that stays open for the
whole session. **The Pi does NOT call `done_writing()` until shutdown.** The Pi
keeps sending audio/video frames and reads hub frames concurrently; transcripts
from the hub are expected and consumed _while the Pi is still sending_. A peer
that half-closes after its first request is wrong.

## (b) One `transcript` per 20 ms audio chunk; mock text for the default engine

The hub emits exactly **one `transcript` frame per 20 ms audio chunk** it
receives. Under the hub's default (mock) recogniser the text is:

```
mock:<len>:<fnv1a64>
```

where `<len>` is the byte length of the chunk and `<fnv1a64>` is the FNV-1a
64-bit hash of the chunk bytes rendered in hex. This is a **plumbing proof, not
recognition** — it demonstrates the audio path end-to-end without a real ASR
engine. Tests may rely on the deterministic `mock:<len>:<hash>` value for a
known input frame.

## (c) The hub NEVER emits `result` frames

The `result` variant of `StreamFrame.payload` (`StreamResult`) is never sent by
the hub. Therefore `pts_ms` and `speaker_label` are **always empty/zero** from
the hub — `StreamResult` is part of the frozen contract but unused on this wire.
The Pi must not block on result frames.

## (d) `DeviceRole` is stored but never enforced

The hub persists the role from `PairRequest.role` but performs **no enforcement**
based on it. This client uses `DEVICE_ROLE_DAEMON` (enum value `2`), matching the
Ubuntu-daemon peer role; the value is informational only.

## (e) There is NO language field in the contract

The frozen proto carries no language/locale field on any message. Therefore
`PI_ECOSYS_TTS_LANG` affects **only local TTS** playback on the Pi; it is never
transmitted to the hub and has no effect on the hub or the recogniser.

## (f) Real-hub acceptance evidence

Acceptance against a real hub is proven by the exact stdout lines the client
prints — `pair-ok device=<id>` and the per-chunk `transcript <text>` lines —
**plus a deterministic `mock:<len>:<hash>` value for a known input frame**. The
deterministic mock value is what makes the end-to-end audio path verifiable.

## (g) The Pi caps its own frame size; the hub's 4 MiB limit is never hit

The hub's default gRPC inbound message limit is **4 MiB**. The Pi caps its own
frame size (chunking audio/video) so a single frame is always well under that
bound. **No hub change is needed.**
