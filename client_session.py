"""Serves one connected phone: streams webcam frames out, plays speech in."""

import json
import socket
import threading
import time

from audio_output import play_speech_audio
from config import DEFAULT_SAMPLE_RATE, DEVICE_ID, FRAME_INTERVAL_SECONDS, PORT
from video_stream import VideoStream

SPEECH_MESSAGE_TYPE = "speech_output"
VIDEO_MESSAGE_TYPE = "video_frame"
HELLO_MESSAGE_TYPE = "hello"


class ClientSession:
    """Owns a single accepted connection and its camera."""

    def __init__(self, connection: socket.socket, address) -> None:
        self._connection = connection
        self._address = address
        self._stop_event = threading.Event()
        self._speech_thread: threading.Thread | None = None

    @property
    def address(self):
        return self._address

    def run(self) -> None:
        """Stream frames until the phone disconnects or the camera dies."""
        self._start_speech_listener()

        # Identify ourselves before opening the camera. A discovery probe reads
        # this greeting to confirm the host is a necklace node, then disconnects
        # without ever starting a video session.
        if not self._send_hello():
            return

        video = VideoStream()
        try:
            if not video.open():
                return
            self._stream_frames(video)
        finally:
            video.close()
            self._stop_event.set()
            self.close()
            self._join_speech_listener()

    def close(self) -> None:
        """Shut the socket down so the reader thread unblocks."""
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass

    def _send_hello(self) -> bool:
        """Announce this node to the client. Returns False if it already left."""
        payload = json.dumps(
            {
                "type": HELLO_MESSAGE_TYPE,
                "device": "necklace-pi",
                "device_id": DEVICE_ID,
                "protocol": 1,
                "port": PORT,
            }
        ) + "\n"
        try:
            self._connection.sendall(payload.encode("utf-8"))
        except OSError as exc:
            # The probe disconnected immediately after reading — expected.
            print(f"[Pi Node] Client left during handshake ({exc}).")
            return False
        return True


    def _stream_frames(self, video: VideoStream) -> None:
        while not self._stop_event.is_set():
            encoded = video.read_encoded_frame()
            if encoded is None:
                print("[Pi Camera] Frame capture failed, ending stream.")
                return
            if not self._send_frame(encoded):
                return
            time.sleep(FRAME_INTERVAL_SECONDS)

    def _send_frame(self, encoded_image: str) -> bool:
        payload = json.dumps(
            {
                "type": VIDEO_MESSAGE_TYPE,
                "image": encoded_image,
                "timestamp": time.time(),
            }
        ) + "\n"
        try:
            self._connection.sendall(payload.encode("utf-8"))
        except OSError as exc:
            print(f"[Pi Node] Send failed ({exc}). Phone disconnected.")
            return False
        return True

    def _start_speech_listener(self) -> None:
        self._speech_thread = threading.Thread(
            target=self._listen_for_speech,
            name="speech-listener",
            daemon=True,
        )
        self._speech_thread.start()

    def _join_speech_listener(self) -> None:
        if self._speech_thread is not None:
            self._speech_thread.join(timeout=2.0)
            self._speech_thread = None

    def _listen_for_speech(self) -> None:
        try:
            reader = self._connection.makefile("r", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"[Pi Receiver] Cannot read from phone: {exc}")
            return

        try:
            for line in reader:
                if self._stop_event.is_set():
                    break
                self._handle_message(line)
        except (OSError, ValueError) as exc:
            if not self._stop_event.is_set():
                print(f"[Pi Receiver Error]: {exc}")
        finally:
            try:
                reader.close()
            except OSError:
                pass

    def _handle_message(self, line: str) -> None:
        line = line.strip()
        if not line:
            return

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print("[Pi Receiver] Ignoring malformed payload.")
            return

        if message.get("type") != SPEECH_MESSAGE_TYPE:
            return

        print(f"[Pi Speaker Output]: {message.get('text', '')}")
        audio_b64 = message.get("audio_data", "")
        if not audio_b64:
            return

        sample_rate = message.get("sample_rate", DEFAULT_SAMPLE_RATE)
        play_speech_audio(audio_b64, sample_rate)
