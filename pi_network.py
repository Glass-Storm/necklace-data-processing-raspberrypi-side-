"""Entry point for the Raspberry Pi node.

Streams webcam frames to the phone app over TCP using the JSON-lines protocol
the Android client already speaks, and plays back speech audio it sends.

Protocol (unchanged):
  Pi   -> phone: {"type": "video_frame", "image": <base64 jpeg>, "timestamp": <float>}
  phone -> Pi  : {"type": "speech_output", "text": str, "audio_data": <base64 pcm>, "sample_rate": int}
Both directions are newline delimited JSON.
"""

import socket

from audio_output import is_playback_available, playback_unavailable_reason
from client_session import ClientSession
from config import HOST, LISTEN_BACKLOG, PORT

BANNER_WIDTH = 52


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


def print_startup_banner() -> None:
    print("=" * BANNER_WIDTH)
    print("[Pi Node] Camera + speech server starting")
    print(f"[Pi Node] Listening on {HOST}:{PORT}")
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


def serve_forever(server: socket.socket) -> None:
    """Accept phones one at a time, serving each until it disconnects."""
    while True:
        connection, address = server.accept()
        print(f"[Pi Node] Phone connected from {address[0]}:{address[1]}")

        session = ClientSession(connection, address)
        try:
            session.run()
        except Exception as exc:  # keep the listener alive for the next phone
            print(f"[Pi Node] Session error: {exc}")
        finally:
            session.close()

        print("[Pi Node] Phone disconnected. Waiting for reconnect...")


def main() -> None:
    print_startup_banner()

    try:
        server = create_server()
    except OSError as exc:
        print(f"[Pi Node] FATAL: cannot listen on {HOST}:{PORT} -> {exc}")
        raise SystemExit(1) from exc

    try:
        serve_forever(server)
    except KeyboardInterrupt:
        print("\n[Pi Node] Shutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
