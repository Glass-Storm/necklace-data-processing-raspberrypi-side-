"""Entry point for the Raspberry Pi node.

Streams hand crops to the phone app over TCP using the JSON-lines protocol
the Android client already speaks, and plays back speech audio it sends.

Protocol (unchanged):
  Pi   -> phone: {"type": "hand_landmarks", "lm": <21x3 floats>, "hand": str, "timestamp": <float>}
                  (default privacy mode: no image data leaves the device at all)
  Pi   -> phone: {"type": "video_frame", "image": <base64 jpeg>, "timestamp": <float>}
                  (crop/segment/aiming modes)
  Pi   -> phone: {"type": "no_hand", "timestamp": <float>}
  phone -> Pi  : {"type": "speech_output", "text": str, "audio_data": <base64 pcm>, "sample_rate": int}
Both directions are newline delimited JSON.
"""

import socket

from audio_output import is_playback_available, playback_unavailable_reason
from client_session import ClientSession
from config import CROP_MARGIN, HOST, LISTEN_BACKLOG, MODEL_PATH, PORT, PRIVACY_MODE
from hand_detector import HandDetector, mediapipe_available
from segmenter import BackgroundRemover, segmenter_available
from config import SEGMENTER_MODEL_PATH

BANNER_WIDTH = 52


def create_detector() -> HandDetector | None:
    """Build the hand detector when privacy cropping is enabled.

    Runs once at startup so a broken setup fails loudly here instead of
    quietly streaming full frames: the whole point of the crop is that a
    full frame must never be sent by accident, so "detector unavailable"
    is treated as fatal rather than degrading to aiming mode.

    Returns:
        The detector, or None when PI_NODE_PRIVACY=off (aiming mode).
    """
    if PRIVACY_MODE == "off":
        print("[Pi Privacy] Crop mode OFF — full frames will be streamed.")
        print("[Pi Privacy] Aiming mode only. Re-enable with PI_NODE_PRIVACY=crop.")
        return None

    if PRIVACY_MODE not in ("crop", "landmarks"):
        print(f"[Pi Privacy] Unknown PI_NODE_PRIVACY='{PRIVACY_MODE}', using 'landmarks'.")

    if not mediapipe_available():
        print("[Pi Privacy] FATAL: PI_NODE_PRIVACY=crop but mediapipe is missing.")
        print("[Pi Privacy] Fix with: pip install mediapipe  (64-bit OS required)")
        raise SystemExit(1)

    try:
        detector = HandDetector(MODEL_PATH, margin=CROP_MARGIN)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[Pi Privacy] FATAL: {exc}")
        raise SystemExit(1) from exc

    what = "landmark coordinates only" if PRIVACY_MODE == "landmarks" else "hand crops only"
    print(f"[Pi Privacy] {PRIVACY_MODE} mode on: {what} leave this device.")
    return detector


def create_segmenter() -> BackgroundRemover | None:
    """Build the background remover for PI_NODE_PRIVACY=segment.

    Same fail-loud policy as the detector: a privacy mode that cannot load
    stops the node rather than degrading to full frames.
    """
    if not segmenter_available():
        print("[Pi Privacy] FATAL: PI_NODE_PRIVACY=segment but mediapipe is missing.")
        print("[Pi Privacy] Fix with: pip install mediapipe  (64-bit OS required)")
        raise SystemExit(1)

    try:
        segmenter = BackgroundRemover(SEGMENTER_MODEL_PATH)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[Pi Privacy] FATAL: {exc}")
        raise SystemExit(1) from exc

    print("[Pi Privacy] Segment mode on: scene removed, person kept.")
    return segmenter


def discover_local_addresses() -> list[str]:
    """Best-effort list of addresses the phone can reach this Pi on."""
    addresses: list[str] = []

    try:
        _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
        addresses.extend(host_ips)
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass

    return sorted({ip for ip in addresses if not ip.startswith("127.")})


def print_startup_banner(detector: HandDetector | None) -> None:
    print("=" * BANNER_WIDTH)
    print("[Pi Node] Camera + speech server starting")
    print(f"[Pi Node] Listening on {HOST}:{PORT}")
    print(f"[Pi Node] Privacy mode: {PRIVACY_MODE} (model: {MODEL_PATH})")
    for address in discover_local_addresses():
        print(f"[Pi Node]   -> point the app at {address}:{PORT}")
    if is_playback_available():
        print("[Pi Audio] Speaker playback ready.")
    else:
        print(f"[Pi Audio] Speaker playback off: {playback_unavailable_reason()}")
        print("[Pi Audio] Fix with: sudo apt install libportaudio2")
    print("=" * BANNER_WIDTH)


def create_server() -> socket.socket:
    """Create the listening socket, accepting connections on every interface."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(LISTEN_BACKLOG)
    return server


def serve_forever(
    server: socket.socket,
    detector: HandDetector | None,
    segmenter: BackgroundRemover | None,
) -> None:
    """Accept phones one at a time, serving each until it disconnects."""
    while True:
        connection, address = server.accept()
        print(f"[Pi Node] Phone connected from {address[0]}:{address[1]}")

        session = ClientSession(connection, address, detector=detector, segmenter=segmenter)
        try:
            session.run()
        except Exception as exc:  # keep the listener alive for the next phone
            print(f"[Pi Node] Session error: {exc}")
        finally:
            session.close()

        print("[Pi Node] Phone disconnected. Waiting for reconnect...")


def main() -> None:
    # Detector first: a privacy setup that cannot load must stop the node
    # before a socket is ever opened.
    if PRIVACY_MODE == "segment":
        detector = None
        segmenter = create_segmenter()
    else:
        detector = create_detector()
        segmenter = None
    print_startup_banner(detector)

    try:
        server = create_server()
    except OSError as exc:
        print(f"[Pi Node] FATAL: cannot listen on {HOST}:{PORT} -> {exc}")
        raise SystemExit(1) from exc

    try:
        serve_forever(server, detector, segmenter)
    except KeyboardInterrupt:
        print("\n[Pi Node] Shutting down.")
    finally:
        server.close()
        if detector is not None:
            detector.close()
        if segmenter is not None:
            segmenter.close()


if __name__ == "__main__":
    main()
