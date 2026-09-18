import base64
import json
import socket
import threading
import time
import cv2
import numpy as np
import sounddevice as sd

HOST = "192.168.0.206"
PORT = 8765

def handle_incoming_speech(conn):
    """Receives text/audio payloads from Android and plays via 3.5mm jack."""
    try:
        reader = conn.makefile('r')
        while True:
            line = reader.readline()
            if not line:
                print("[Pi] Client disconnected.")
                break

            data = json.loads(line)
            if data.get("type") == "speech_output":
                text = data.get("text", "")
                audio_b64 = data.get("audio_data", "")
                sample_rate = data.get("sample_rate", 16000)

                print(f"[Pi Speaker Output]: {text}")

                if audio_b64:
                    pcm_bytes = base64.b64decode(audio_b64)
                    audio_np = np.frombuffer(pcm_bytes, dtype=np.int16)
                    sd.play(audio_np, samplerate=sample_rate)
                    sd.wait()
    except Exception as e:
        print(f"[Pi Receiver Error]: {e}")

def start_pi_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"[Pi Node Active] Listening on port {PORT}...")

    while True:
        conn, addr = server.accept()
        print(f"[Pi Node] Connected to Android Phone at {addr}")

        speech_thread = threading.Thread(target=handle_incoming_speech, args=(conn,), daemon=True)
        speech_thread.start()

        cap = cv2.VideoCapture(0)

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                frame = cv2.resize(frame, (320, 240))
                _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
                img_b64 = base64.b64encode(buffer).decode('utf-8')

                payload = json.dumps({
                    "type": "video_frame",
                    "image": img_b64,
                    "timestamp": time.time()
                }) + "\n"

                conn.sendall(payload.encode('utf-8'))
                time.sleep(0.1)  # 10 FPS
        except (BrokenPipeError, ConnectionResetError):
            print("[Pi] Connection lost. Waiting for reconnect...")
        finally:
            cap.release()
            conn.close()

if __name__ == '__main__':
    start_pi_server()