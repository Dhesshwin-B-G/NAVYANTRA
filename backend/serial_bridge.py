import serial
import urllib.request
import json

SERIAL_PORT = "COM7"
BAUD_RATE = 115200

API_URL = "http://127.0.0.1:8000/api/serial"


def send_to_backend(data):
    payload = json.dumps({
        "data": data
    }).encode("utf-8")

    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.read().decode("utf-8")

    except Exception as e:
        print(f"[BACKEND ERROR] {e}")
        return None


print(f"[SERIAL] Connecting to {SERIAL_PORT}...")

ser = serial.Serial(
    SERIAL_PORT,
    BAUD_RATE,
    timeout=1
)

print(f"[SERIAL] Connected to {SERIAL_PORT}")
print("[SERIAL] Waiting for MLDATA...")


while True:
    line = ser.readline().decode(
        errors="ignore"
    ).strip()

    if line.startswith("MLDATA,"):
        print(f"[ESP32] {line}")

        result = send_to_backend(line)

        if result:
            print("[BACKEND] Data received")