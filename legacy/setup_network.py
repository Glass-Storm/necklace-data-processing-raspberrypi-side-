import asyncio
import json
import subprocess
from bleak import BleakPeripheral

# uuid that the app will search for
SERVICE_UUID = "39eeb7c4-d5e2-47fa-b535-8ee4fdfe7fc6"
CHAR_UUID    = "82ec26e0-f582-4153-b105-202d78032fd4"

# Global flag to stop the BLE server once connected to Wi-Fi
stop_ble_event = asyncio.Event()

def connect_to_wifi(ssid, password):
    """Feeds credentials into Debian 13 NetworkManager via nmcli"""
    print(f"Attempting to connect to Wi-Fi: {ssid}...")
    try:
        # Command to connect via nmcli
        cmd = f'sudo nmcli device wifi connect "{ssid}" password "{password}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        
        if result.returncode == 0:
            print("Successfully connected to Hotspot!")
            # Ensure it auto-connects in the future
            subprocess.run(f'sudo nmcli connection modify "{ssid}" connection.autoconnect yes', shell=True)
            return True
        else:
            print(f"Connection failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"Error executing nmcli: {e}")
        return False

def write_callback(characteristic, data):
    """Triggered when your mobile app sends data over BLE"""
    try:
        # Decode bytes payload from mobile app
        payload = data.decode("utf-8")
        credentials = json.loads(payload)
        
        ssid = credentials.get("ssid")
        password = credentials.get("pass")
        
        if ssid and password:
            success = connect_to_wifi(ssid, password)
            if success:
                # Signal the script to close the BLE server safely
                stop_ble_event.set()
    except Exception as e:
        print(f"Failed to process BLE data: {e}")

async def main():
    # Initialize the BLE peripheral
    peripheral = BleakPeripheral()
    
    # Add your custom network configuration service
    peripheral.add_service(SERVICE_UUID)
    peripheral.add_characteristic(
        SERVICE_UUID, 
        CHAR_UUID, 
        properties=["write"], 
        value=None, 
        write_callback=write_callback
    )
    
    print("Starting BLE Configuration Server... Connect your mobile app now.")
    await peripheral.start()
    
    # Wait here indefinitely until the Wi-Fi connection succeeds
    await stop_ble_event.wait()
    
    print("Shutting down BLE Server. Moving strictly to Wi-Fi Port communication.")
    await peripheral.stop()

if __name__ == "__main__":
    asyncio.run(main())