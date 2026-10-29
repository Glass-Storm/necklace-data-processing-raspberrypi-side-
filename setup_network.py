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
    GATTDescriptorProperties,
)

# Wire contract with the Android app -- do not change.
SERVICE_UUID = "39eeb7c4-d5e2-47fa-b535-8ee4fdfe7fc6"
CHAR_UUID = "82ec26e0-f582-4153-b105-202d78032fd4"
# Additive alternative input: one value per characteristic instead of a
# JSON payload. Entirely optional -- the JSON characteristic above is
# unchanged. A join is scheduled once both fields have been written.
SSID_CHAR_UUID = "82ec26e0-f582-4153-b105-202d78032fd5"
PASS_CHAR_UUID = "82ec26e0-f582-4153-b105-202d78032fd6"
# Standard Characteristic User Description descriptor, so scanner apps
# can label the characteristics for humans.
USER_DESC_UUID = "2901"

# Hard cap so this never advertises forever.
BLE_SESSION_TIMEOUT_SECONDS = 300.0
# NetworkManager needs longer than 15 s to associate + DHCP.
WIFI_JOIN_TIMEOUT_SECONDS = 60.0

SERVER_NAME = "NeuralNecklace-01"

# --- Advertising workaround --------------------------------------------------
# bluetoothd's LEAdvertisingManager1.RegisterAdvertisement fails on this
# board (BlueZ 5.82 / BCM43438): bluetoothd logs "Failed to add
# advertisement: Invalid Parameters (0x0d)", even for a minimal
# advertisement, so bless's server.start() dies before any phone can
# connect. Advertising through the kernel mgmt interface works
# (verified: `btmgmt add-adv` -> "Instance added: 1", and a phone
# scanner saw this Pi), so replace bless's advertising step only:
# the GATT server stays with bluetoothd/bless, discovery is done by a
# btmgmt advertising instance carrying our service UUID. The btmgmt
# calls run through a short-lived `sleep` pipe because btmgmt's
# one-shot commands hang when their stdin is /dev/null (as under
# systemd); with a pipe writer they return once the instance is added.
import subprocess as _subprocess


def _btmgmt_adv(*args: str) -> None:
    cmd = "sleep 2 | btmgmt " + " ".join(args)
    try:
        r = _subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
        print(f"[BLE] btmgmt {args[0]}: rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()}")
    except Exception as exc:
        # A timeout here usually means the instance was added and
        # btmgmt stayed in its monitor loop; advertising must not
        # take the GATT server down with it.
        print(f"[BLE] btmgmt {args[0]}: {type(exc).__name__}: {exc} (continuing)")


async def _btmgmt_start_advertising(self, adapter):
    try:
        await self.set_name(adapter, self.app_name)
    except Exception as exc:
        print(f"[BLE] Could not set adapter name: {exc}")
    # The kernel mgmt instance does not add the local name to the scan
    # response by itself, and phone scanners then show "N/A" instead of
    # the device name, so pass it explicitly as a Complete Local Name
    # AD structure in the scan response data.
    _name = SERVER_NAME.encode()
    _scan_rsp = (bytes([len(_name) + 1, 0x09]) + _name).hex()
    _btmgmt_adv("add-adv", "-u", SERVICE_UUID, "-s", _scan_rsp, "1")


async def _btmgmt_stop_advertising(self, adapter):
    _btmgmt_adv("rm-adv", "1")


try:
    from bless.backends.bluezdbus.dbus.application import BlueZGattApplication as _App
    _App.start_advertising = _btmgmt_start_advertising
    _App.stop_advertising = _btmgmt_stop_advertising
    print("[BLE] btmgmt advertising workaround installed.")
except Exception as exc:
    print(f"[BLE] Could not install btmgmt advertising workaround: {exc}")


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



async def _make_sole_autoconnect(ssid: str) -> None:
    """Make [ssid] the ONLY saved Wi-Fi.

    A phone provisions its hotspot over BLE precisely so the Pi never
    picks a network on its own. Remembering the new network is not
    enough: every other saved Wi-Fi profile is deleted, or a stray
    known network could win the race at the next boot (this is exactly
    what happened in live demos), and previous users' hotspot
    credentials would linger on the device.
    """
    code2, _out2, err2 = await _nmcli(
        "connection", "modify", ssid,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "100",
    )
    if code2 != 0:
        print(f"[BLE] Connected, but could not set autoconnect: {err2}")
        return
    code, out, err = await _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    if code != 0:
        print(f"[BLE] Could not list connections to remove others: {err}")
        return
    for line in out.splitlines():
        name, sep, typ = line.rpartition(":")
        if not sep or typ != "802-11-wireless" or not name or name == ssid:
            continue
        c, _o, e = await _nmcli("connection", "delete", name)
        if c == 0:
            print(f"[BLE] Deleted other Wi-Fi profile {name!r}.")
        else:
            print(f"[BLE] Could not delete other Wi-Fi profile {name!r}: {e}")

async def connect_to_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Ask NetworkManager to join [ssid]. Returns (ok, human-readable detail)."""
    print(f"[BLE] Attempting Wi-Fi join for SSID {ssid!r} ...")
    _set_status({"status": "connecting", "ssid": ssid})

    # Join strategy matters (live-test finding, 2026-10-30):
    # `nmcli device wifi connect <ssid> password ...` fails with
    # "802-11-wireless-security.key-mgmt: property is missing" when a
    # good saved profile already exists -- nmcli tries to assemble a
    # fresh profile instead of using the saved one. So:
    #   * SSID already saved -> update its PSK in place (this is also
    #     exactly how a user changes a hotspot password) and bring the
    #     existing profile up.
    #   * New SSID -> device wifi connect; if that hits the same
    #     key-mgmt error, create the profile explicitly with
    #     key-mgmt wpa-psk and bring it up.
    code_l, out_l, _err_l = await _nmcli("-t", "-f", "NAME", "connection", "show")
    existing = [ln.strip() for ln in out_l.splitlines() if ln.strip()] if code_l == 0 else []
    code, _out, err = 1, "", "not attempted"
    if ssid in existing:
        print(f"[BLE] SSID {ssid!r} already saved; updating its password in place.")
        code, _out, err = await _nmcli(
            "connection", "modify", ssid, "802-11-wireless-security.psk", password
        )
        if code == 0:
            code, _out, err = await _nmcli("connection", "up", ssid)
    else:
        code, _out, err = await _nmcli(
            "device", "wifi", "connect", ssid, "password", password
        )
        if code != 0 and "key-mgmt" in err:
            print("[BLE] device wifi connect hit the key-mgmt error; creating profile explicitly.")
            code, _out, err = await _nmcli(
                "connection", "add", "type", "wifi", "con-name", ssid,
                "ssid", ssid, "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", password, "connection.interface-name", "wlan0",
            )
            if code == 0:
                code, _out, err = await _nmcli("connection", "up", ssid)
    if code != 0:
        print(f"[BLE] Wi-Fi join failed (exit {code}): {err}")
        _set_status({"status": "failed", "detail": err or f"nmcli exit {code}"})
        return False, err or f"nmcli exit {code}"

    # Remember the network so the node comes back on its own after a
    # reboot -- and make it the only one that can auto-connect, so a
    # different saved Wi-Fi can never steal the next boot.
    await _make_sole_autoconnect(ssid)

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


# Values written to the single-field characteristics, waiting for their
# other half. A join is scheduled only once both have arrived.
_pending_fields: dict = {}


def _field_write(char_uuid: str, value) -> None:
    """Handle a write to the SSID or the password characteristic."""
    global _background_task

    try:
        text = bytes(value).decode("utf-8").strip()
    except Exception:
        print("[BLE] Rejected field write: value is not UTF-8 text.")
        _set_status({"status": "failed", "detail": "field values must be text"})
        return

    if char_uuid == SSID_CHAR_UUID:
        _pending_fields["ssid"] = text
        print(f"[BLE] SSID field received: {text!r}.")
    else:
        # The password itself is never logged.
        _pending_fields["pass"] = text
        print("[BLE] Password field received.")

    ssid = _pending_fields.get("ssid") or ""
    password = _pending_fields.get("pass") or ""
    if not ssid or not password:
        _set_status({"status": "waiting", "detail": "waiting for both ssid and password"})
        return

    if _event_loop is None:
        print("[BLE] No event loop captured; cannot schedule Wi-Fi join.")
        _set_status({"status": "failed", "detail": "server loop unavailable"})
        return

    _pending_fields.clear()
    print(f"[BLE] Credentials received for {ssid!r} (separate fields); scheduling join.")
    _set_status({"status": "pending", "ssid": ssid})
    _background_task = _event_loop.create_task(_handle_credentials(ssid, password))


def write_request(characteristic: BlessGATTCharacteristic, value, **_kwargs) -> None:
    """BLE write callback.

    bless calls this from its D-Bus loop, so it must return immediately: the
    nmcli join is scheduled as a task rather than awaited here. Doing it
    inline stalled the D-Bus loop and the phone's write timed out.
    """
    global _background_task

    char_uuid = str(characteristic.uuid).lower()
    if char_uuid in (SSID_CHAR_UUID, PASS_CHAR_UUID):
        _field_write(char_uuid, value)
        return

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

    # Optional single-field input (see SSID_CHAR_UUID above). The
    # password characteristic is write-only, so it cannot be read back.
    await server.add_new_characteristic(
        SERVICE_UUID,
        SSID_CHAR_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.read,
        None,
        GATTAttributePermissions.readable | GATTAttributePermissions.writeable,
    )
    await server.add_new_characteristic(
        SERVICE_UUID,
        PASS_CHAR_UUID,
        GATTCharacteristicProperties.write,
        None,
        GATTAttributePermissions.writeable,
    )
    # Human labels for the characteristics. (A custom service cannot
    # carry a name inside GATT; apps label the service UUID in code.)
    for _char_uuid, _label in (
        (CHAR_UUID, "credentials"),
        (SSID_CHAR_UUID, "ssid"),
        (PASS_CHAR_UUID, "password"),
    ):
        await server.add_new_descriptor(
            SERVICE_UUID,
            _char_uuid,
            USER_DESC_UUID,
            GATTDescriptorProperties.read,
            bytearray(_label.encode("utf-8")),
            GATTAttributePermissions.readable,
        )

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

