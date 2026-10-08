from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from datetime import datetime

app = FastAPI(title="AeroGuard Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SerialData(BaseModel):
    data: str


# Stores structured telemetry packets
telemetry_log: List[dict] = []


# ============================================================
# MLDATA → JSON CONVERTER
# ============================================================

def parse_mldata(raw_data: str):
    """
    Convert ESP32 MLDATA CSV into structured JSON.
    """

    raw_data = raw_data.strip()

    # Make sure this is MLDATA
    if not raw_data.startswith("MLDATA,"):
        return None

    fields = raw_data.split(",")

    # MLDATA + 21 fields = 22 total
    if len(fields) != 22:
        return None

    try:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        node_id = int(fields[2])
        return {
            "type": "TELEMETRY",

            "src": f"NODE-{node_id}",

            "time": current_time,

            "payload": {
                "timestamp_ms": int(fields[1]),
                "node_id": node_id,
                "packet": int(fields[3]),
                "gps": {
                    "latitude": float(fields[4]),
                    "longitude": float(fields[5])
                },
                "altitude": float(fields[6]),
                "altitude_ft": float(fields[6]) * 3.28084,
                "temperature": float(fields[7]),
                "pressure": float(fields[8]),
                "north": float(fields[9]),
                "east": float(fields[10]),
                "roll": float(fields[11]),
                "pitch": float(fields[12]),
                "yaw": float(fields[13]),
                "heading": float(fields[13]),
                "heading_deg": float(fields[13]),
                "speed": float(fields[14]),
                "speed_knots": float(fields[14]) * 0.539957,
                "vertical_speed": float(fields[15]),
                "status": fields[16],
                "link": fields[17],
                "rssi": int(fields[18]),
                "snr": float(fields[19]),
                "age_ms": int(fields[20]),
                "received": int(fields[21])
            }
        }

    except (ValueError, TypeError):
        return None


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "system": "AeroGuard",
        "status": "online"
    }


# ============================================================
# RECEIVE SERIAL DATA
# ============================================================

@app.post("/api/serial")
def receive_serial_data(payload: SerialData):

    packet = parse_mldata(payload.data)

    # Ignore invalid/non-MLDATA lines
    if packet is None:
        return {
            "status": "ignored",
            "data": payload.data
        }

    telemetry_log.append(packet)

    # Keep only latest 200 packets
    if len(telemetry_log) > 200:
        telemetry_log.pop(0)

    return {
        "status": "received",
        "packet": packet
    }


# ============================================================
# GET TELEMETRY LOG
# ============================================================

@app.get("/api/log")
def get_log():
    return telemetry_log


# ============================================================
# GET LATEST TELEMETRY
# ============================================================

@app.get("/api/latest")
def get_latest():

    if not telemetry_log:
        return {
            "status": "no_data",
            "data": None
        }

    return telemetry_log[-1]