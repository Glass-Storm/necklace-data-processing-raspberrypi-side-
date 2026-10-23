"""BLE onboarding server for the neural necklace.

Phone writes {"ssid": ..., "pass": ...} to CHAR_UUID over BLE; we hand the
credentials to NetworkManager via nmcli, then stop advertising and exit 0.

Written against the current `bless` API. The previous version imported
`BleakPeripheral`, which bleak no longer has, so the unit died with
NameError on every start.
"""

from __future__ import annotations

import asyncio
import json
import os

from bless import (
    BlessGATTCharacteristic,
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

# Wire contract with the Android app -- do not change.
SERVICE_UUID = "39eeb7c4-d5e2-47fa-b535-8ee4fdfe7fc6"
CHAR_UUID = "82ec26e0-f582-4153-b105-202d78032fd4"

# Hard cap so this never advertises forever.
BLE_SESSION_TIMEOUT_SECONDS = 300.0
# NetworkManager needs longer than 15 s to associate + DHCP.
WIFI_JOIN_TIMEOUT_SECONDS = 60.0

SERVER_NAME = "NeuralNecklaceSetup"

stop_ble_event = asyncio.Event()
_event_loop: asyncio.AbstractEventLoop | None = None
_status_characteristic: BlessGATTCharacteristic | None = None
_background_task: asyncio.Task | None = None


def _set_status(payload: dict) -> None:
    """Publish a JSON status the phone can read back from CHAR_UUID."""
    global _status_characteristic
    if _status_characteristic is None:
        return
    try:
        encoded = json.dumps(payload).encode("utf-8")
        _status_characteristic.value = bytearray(encoded)
    except Exception as exc:  # status reporting must never crash the server
        print(f"[BLE] Could not update status characteristic: {exc}")


async def _nmcli(*args: str) -> tuple[int, str, str]:
    """Run nmcli with an argument list, never a shell.

    `sudo` is only prepended when this process is not already root.
    Returns (returncode, stdout, stderr).
    """
    command: list[str] = []
    if os.geteuid() != 0:
        command.append("sudo")
    command.append("nmcli")
    command.extend(args)

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            process.communicate(), timeout=WIFI_JOIN_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", f"nmcli timed out after {WIFI_JOIN_TIMEOUT_SECONDS:.0f}s"
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    return (
        process.returncode if process.returncode is not None else 1,
        stdout_b.decode("utf-8", "replace").strip(),
        stderr_b.decode("utf-8", "replace").strip(),
    )


async def connect_to_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Ask NetworkManager to join [ssid]. Returns (ok, human-readable detail)."""
    print(f"[BLE] Attempting Wi-Fi join for SSID {ssid!r} ...")
    _set_status({"status": "connecting", "ssid": ssid})

    code, _out, err = await _nmcli(
        "device", "wifi", "connect", ssid, "password", password
    )
    if code != 0:
        print(f"[BLE] Wi-Fi join failed (exit {code}): {err}")
        _set_status({"status": "failed", "detail": err or f"nmcli exit {code}"})
        return False, err or f"nmcli exit {code}"

    # Remember the network so the node comes back on its own after a reboot.
    code2, _out2, err2 = await _nmcli(
        "connection", "modify", ssid, "connection.autoconnect", "yes"
    )
    if code2 != 0:
        # Non-fatal: the link is up even if autoconnect could not be set.
        print(f"[BLE] Connected, but could not set autoconnect: {err2}")

    # The password is never printed or placed in the status payload.
    print(f"[BLE] Connected to {ssid!r}.")
    _set_status({"status": "connected", "ssid": ssid})
    return True, "connected"


async def _handle_credentials(ssid: str, password: str) -> None:
    """Do the slow work outside the BLE callback."""
    try:
        ok, _detail = await connect_to_wifi(ssid, password)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[BLE] Unexpected error during Wi-Fi join: {exc}")
        _set_status({"status": "failed", "detail": str(exc)})
        return

    if ok:
        stop_ble_event.set()


def write_request(characteristic: BlessGATTCharacteristic, value, **_kwargs) -> None:
    """BLE write callback.

    bless calls this from its D-Bus loop, so it must return immediately: the
    nmcli join is scheduled as a task rather than awaited here. Doing it
    inline stalled the D-Bus loop and the phone's write timed out.
    """
    global _background_task

    try:
        payload = json.loads(bytes(value).decode("utf-8"))
    except Exception as exc:
        print(f"[BLE] Rejected malformed write: {exc}")
        _set_status({"status": "failed", "detail": "malformed JSON payload"})
        return

    if not isinstance(payload, dict):
        print("[BLE] Rejected write: payload is not a JSON object.")
        _set_status({"status": "failed", "detail": "expected a JSON object"})
        return

    ssid = (payload.get("ssid") or "").strip()
    password = payload.get("pass") or payload.get("password") or ""

    if not ssid or not password:
        print("[BLE] Rejected write: missing ssid or pass.")
        _set_status({"status": "failed", "detail": "ssid and pass are required"})
        return

    if _event_loop is None:
        print("[BLE] No event loop captured; cannot schedule Wi-Fi join.")
        _set_status({"status": "failed", "detail": "server loop unavailable"})
        return

    # Never log the password.
    print(f"[BLE] Credentials received for {ssid!r}; scheduling join.")
    _set_status({"status": "pending", "ssid": ssid})
    _background_task = _event_loop.create_task(_handle_credentials(ssid, password))


def read_request(characteristic: BlessGATTCharacteristic, **_kwargs) -> bytes:
    """Report provisioning status to the app."""
    value = characteristic.value
    if value:
        return bytes(value)
    return json.dumps({"status": "waiting"}).encode("utf-8")


async def _run_session(server: BlessServer) -> None:
    """Advertise until credentials land or the session cap expires."""
    await server.start()
    print(f"[BLE] Advertising as {SERVER_NAME} -- connect the app now.")

    try:
        await asyncio.wait_for(
            stop_ble_event.wait(), timeout=BLE_SESSION_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        print(
            "[BLE] No credentials received within "
            f"{BLE_SESSION_TIMEOUT_SECONDS:.0f}s; giving up."
        )
        _set_status({"status": "failed", "detail": "provisioning session timed out"})
        raise
    finally:
        print("[BLE] Shutting down BLE server.")
        try:
            await server.stop()
        except Exception as exc:
            print(f"[BLE] Error while stopping BLE server: {exc}")


async def main() -> int:
    """Return the process exit code: 0 once Wi-Fi is joined."""
    global _event_loop, _status_characteristic, _background_task

    _event_loop = asyncio.get_running_loop()
    stop_ble_event.clear()

    server = BlessServer(name=SERVER_NAME)
    await server.add_new_service(SERVICE_UUID)

    await server.add_new_characteristic(
        SERVICE_UUID,
        CHAR_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.read,
        bytearray(json.dumps({"status": "waiting"}).encode("utf-8")),
        GATTAttributePermissions.readable | GATTAttributePermissions.writeable,
    )
    # BlueZDBus's add_new_characteristic returns None, so the object has to be
    # fetched back out of the server. Without this _status_characteristic stayed
    # None and every _set_status() call silently did nothing.
    _status_characteristic = server.get_characteristic(CHAR_UUID)

    server.read_request_func = read_request
    server.write_request_func = write_request

    try:
        await _run_session(server)
    except asyncio.TimeoutError:
        return 1

    if _background_task is not None and not _background_task.done():
        try:
            await _background_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[BLE] Wi-Fi join task failed: {exc}")
            return 1

    print("[BLE] Provisioning complete. Handing off to the node on TCP 8765.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)

