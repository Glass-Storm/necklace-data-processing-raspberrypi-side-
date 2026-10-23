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
from dnn_hand_detector import DnnHandDetector, dnn_available
from hand_detector import HandDetector, mediapipe_available, mediapipe_usable
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
        print("[Pi Privacy] Aiming mode only. Re-enable with PI_NODE_PRIVACY=landmarks.")
        return None

    # PRIVACY_MODE is normalised in config, so it is always one of
    # config.PRIVACY_MODES here.
    try:
        detector = build_hand_detector()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[Pi Privacy] FATAL: {exc}")
        raise SystemExit(1) from exc

    what = "landmark coordinates only" if PRIVACY_MODE == "landmarks" else "hand crops only"
    print(f"[Pi Privacy] {PRIVACY_MODE} mode on: {what} leave this device.")
    return detector


def build_hand_detector() -> HandDetector | DnnHandDetector:
    """Build a hand detector with whichever backend this machine can run.

    MediaPipe is preferred when it loads. It does not on every board: its
    native library is built with the ARMv8 AES instruction, and on a CPU
    without it the import kills the process. In that case the same
    ``hand_landmarker.task`` models are run through OpenCV's DNN module, which
    needs no AES, so the landmarks mode works either way.

    Returns:
        The detector, whichever backend it came from.

    Raises:
        RuntimeError: neither backend can run.
    """
    if mediapipe_usable():
        try:
            return HandDetector(MODEL_PATH, margin=CROP_MARGIN)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"[Pi Privacy] MediaPipe detector unavailable: {exc}")
    elif mediapipe_available():
        # Importing mediapipe succeeds even on a CPU its native code cannot
        # run on; using it aborts the process instead of raising. Say so.
        print("[Pi Privacy] mediapipe is installed but this CPU has no AES "
              "instruction, which its native code requires. Using OpenCV.")
    else:
        print("[Pi Privacy] mediapipe is not installed. Using OpenCV.")

    if not dnn_available():
        raise RuntimeError(
            "no usable hand detector: mediapipe is missing and this OpenCV "
            "has no cv2.dnn.readNetFromTFLite (needs opencv-python 4.5.4+)"
        )

    print("[Pi Privacy] Using the bundled TFLite models through OpenCV "
          "(no MediaPipe needed).")
    return DnnHandDetector(MODEL_PATH, margin=CROP_MARGIN)


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
