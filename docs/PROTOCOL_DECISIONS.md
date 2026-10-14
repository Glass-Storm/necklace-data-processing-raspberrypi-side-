# Protocol decisions (frozen)

These are the wire-contract and hub-behaviour facts the Pi client is built
against. They are FROZEN: the Pi vendors the proto byte-identically
(`proto/ecosys/v1/ecosys.proto`, sha256
`56cdbf19dad7669876a5b5899e4821bf42a328e53c1f8684cd82424d1941d2c2`) and MUST NOT
change the contract locally.

Provenance of the contract: see `proto/PROTO_SOURCE` (hub repo `phone-manager`,
`contract/src/main/proto/ecosys/v1/ecosys.proto`, source commit `50d77e8`).
Drift is detected by `tests/test_proto_drift.py`, which re-hashes the proto in
the sibling hub checkout.

## (a) The stream is UNAUTHENTICATED, and the session binds with `stream_open`

`StreamService/OpenStream` is a bidirectional stream opened with **no token**.
Pairing, PIN and bearer tokens were **removed from the hub runtime**, so the Pi
never calls `Pair` and never sends an `authorization` header. Instead the FIRST
frame on the stream is a `stream_open` (`StreamOpenRequest{device_id, kind,
role}`) that binds the session; the hub routes frames by the bound role.

`PairingService`/`Pair`/`Heartbeat` remain declared in the frozen `.proto` as an
unserved, reserved stub (wire compatibility) — the Pi must not call them.

## (b) The stream is LONG-LIVED

The bidi stream stays open for the whole session. **The Pi does NOT call
`done_writing()` until shutdown.** It keeps sending image/audio frames and reads
hub frames concurrently. A peer that half-closes after its first request is
wrong.

## (c) The Pi sends JPEG stills, NOT H.264

The video producer sends `StreamFrame.image` (`ImageFrame{jpeg, pts_ms, width,
height}`) at **320×240 and ~10 fps** (a 0.1 s pump cadence). It does **not** send
`video_h264_nal`. This is the ONE media kind the hub decodes — into a bitmap,
solely to run isolated gesture recognition. The hub never re-encodes, stores, or
renders it.

## (d) Hub-synthesized TTS: the Pi only plays

The hub runs text-to-speech and sends `StreamFrame.tts_audio`
(`TtsAudio.pcm16_16k`, raw little-endian PCM16 mono **@ 16 kHz**). The Pi **only
plays those bytes** — via `aplay -f S16_LE -r 16000 -c 1`, falling back to
`sounddevice` — and never synthesizes speech locally. A `gesture` frame is
informational: the spoken meaning arrives as `tts_audio`.

## (e) Hub frames the Pi consumes

The Pi's reader dispatches exactly three hub payload variants:

- `tts_audio` — played (section d).
- `gesture` — its recognized text is logged to **stderr** (never the frozen
  stdout contract).
- `transcript` — the legacy plain-text form; printed as the frozen
  `transcript <text>` stdout line.

`result` / `enroll_request` / `enroll_response` / `stream_open` are
glasses-facing (or peer-to-hub) frames the necklace does not consume.

## (f) Hub discovery: `nmcli` hotspot join, then the default gateway

The hub is the phone hotspot's access point, so the Pi:

1. joins the hotspot from config with
   `nmcli device wifi connect <ssid> password <pw>` (WPA2, or WPA3 personal with
   `wifi-sec.key-mgmt sae`), then
2. resolves the hub as the **default gateway** (`ip route`, falling back to
   `nmcli --terse --fields IP4.GATEWAY device show`) and appends the hub port.

There is **NO mDNS**: the precedence is `--hub` > `PI_ECOSYS_HUB` > default
gateway. When no gateway exists the client raises `HubNotFoundError` with a
clear message rather than hanging. Credentials come from the git-ignored
dotenv/config layer; nothing is hardcoded.

## (g) No heartbeat; liveness is implied by the open stream

The Pi **does not heartbeat**. The hub no longer serves `Heartbeat`, and liveness
is implied by the open `OpenStream`; a transport loss simply reconnects.

## (h) No language field; no local synthesis

The frozen proto carries no language/locale field. The Pi's `--lang` /
`PI_ECOSYS_TTS_LANG` setting therefore has **no effect on the wire** and no local
TTS engine to drive: the hub selects the voice and ships the audio. The setting
is retained for compatibility only.

## (i) The Pi caps its own frame size; the hub's 4 MiB limit is never hit

The hub's default gRPC inbound message limit is **4 MiB**. The Pi caps its own
frame size (chunking audio, one JPEG per frame) so a single frame is always well
under that bound. **No hub change is needed.**
