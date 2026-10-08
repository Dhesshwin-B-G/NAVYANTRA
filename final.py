"""
==============================================================================
   AEROGUARD — UNIFIED REAL-HARDWARE RUNTIME ENGINE
==============================================================================
Single-file PC-side entry point for the AeroGuard physical mesh network:
  Arduino IDE (ESP32 Nodes 1, 2, 3)
      ↓ LoRa
  Ground Node ESP32
      ↓ USB Serial (COM7 @ 115200)
  final.py (Serial Reader + MLDATA Parser + Shared Telemetry + Voice + Web Server)
      ↓
  Unified Web UI & JARVIS Voice HUD (http://127.0.0.1:8000)

Usage:
  python final.py
==============================================================================
"""

import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import serial
import uvicorn

# Configure terminal encoding for Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# 1. LOGGING & ENVIRONMENT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AeroGuard.Final")

# Load .env configurations
_ROOT_DIR = Path(__file__).resolve().parent
for env_candidate in [_ROOT_DIR / ".env", _ROOT_DIR / "voice_layer" / ".env"]:
    if env_candidate.is_file():
        load_dotenv(dotenv_path=env_candidate, override=False)
        break
else:
    load_dotenv(override=False)

SERIAL_PORT = os.getenv("SERIAL_PORT", "COM7").strip()
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
SERIAL_TIMEOUT = float(os.getenv("SERIAL_TIMEOUT", "1.0"))
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1").strip()
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# Locate web_UI.html
UI_PATH = _ROOT_DIR / "web" / "web_UI.html"
if not UI_PATH.exists():
    UI_PATH = _ROOT_DIR / "src" / "web_UI.html"

# ─────────────────────────────────────────────────────────────────────────────
# 2. MLDATA PARSER (22 TOKENS)
# ─────────────────────────────────────────────────────────────────────────────
def parse_mldata(raw_data: str) -> Optional[Dict[str, Any]]:
    """Convert raw ESP32 MLDATA CSV telemetry line into structured JSON packet.

    Expected Wire Format:
    MLDATA,<timestamp_ms>,<node_id>,<packet_seq>,<lat>,<lon>,<alt>,<temp>,<press>,
           <north>,<east>,<roll>,<pitch>,<yaw>,<speed>,<v_speed>,<status>,<link>,
           <rssi>,<snr>,<age_ms>,<received>
    """
    raw = raw_data.strip()
    if not raw.startswith("MLDATA,"):
        return None

    fields = raw.split(",")
    if len(fields) != 22:
        return None

    try:
        timestamp_ms = int(fields[1])
        node_id = int(fields[2])
        packet_seq = int(fields[3])
        lat = float(fields[4])
        lon = float(fields[5])
        alt = float(fields[6])
        temp = float(fields[7])
        press = float(fields[8])
        north = float(fields[9])
        east = float(fields[10])
        roll = float(fields[11])
        pitch = float(fields[12])
        yaw = float(fields[13])
        speed = float(fields[14])
        v_speed = float(fields[15])
        status = fields[16].strip()
        link = fields[17].strip()
        rssi = int(fields[18])
        snr = float(fields[19])
        age_ms = int(fields[20])
        received = int(fields[21])

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        src_label = f"NODE-{node_id}"

        return {
            "type": "TELEMETRY",
            "src": src_label,
            "time": current_time,
            "payload": {
                "timestamp_ms": timestamp_ms,
                "node_id": node_id,
                "packet": packet_seq,
                "gps": {
                    "latitude": lat,
                    "longitude": lon,
                },
                "altitude": alt,
                "altitude_ft": alt * 3.28084,
                "temperature": temp,
                "pressure": press,
                "north": north,
                "east": east,
                "roll": roll,
                "pitch": pitch,
                "yaw": yaw,
                "heading": yaw,
                "heading_deg": yaw,
                "speed": speed,
                "speed_knots": speed * 0.539957,
                "vertical_speed": v_speed,
                "status": status,
                "link": link,
                "rssi": rssi,
                "snr": snr,
                "age_ms": age_ms,
                "received": received,
            },
        }
    except (ValueError, TypeError, IndexError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. THREAD-SAFE SHARED TELEMETRY STATE
# ─────────────────────────────────────────────────────────────────────────────
class SharedTelemetryState:
    """Thread-safe in-memory store for rolling telemetry packets and node states."""

    def __init__(self, max_history: int = 200) -> None:
        self.max_history = max_history
        self._lock = threading.Lock()
        self._telemetry_log: List[Dict[str, Any]] = []
        self._latest_packet: Optional[Dict[str, Any]] = None
        self._latest_per_node: Dict[str, Dict[str, Any]] = {}
        self._active_nodes: set = set()
        self._total_packets: int = 0

    def add_packet(self, packet: Dict[str, Any]) -> None:
        with self._lock:
            self._telemetry_log.append(packet)
            if len(self._telemetry_log) > self.max_history:
                self._telemetry_log.pop(0)

            self._latest_packet = packet
            src = str(packet.get("src", "")).strip().upper()
            self._latest_per_node[src] = packet

            # Also index by numeric identifier (e.g. '1' for 'NODE-1')
            if src.startswith("NODE-"):
                num_id = src.replace("NODE-", "")
                self._latest_per_node[num_id] = packet

            self._active_nodes.add(src)
            self._total_packets += 1

    def get_log(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._telemetry_log)

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._latest_packet) if self._latest_packet else None

    def get_node_packet(self, node_id: Any) -> Optional[Dict[str, Any]]:
        clean = str(node_id).strip().upper()
        with self._lock:
            # Direct match
            if clean in self._latest_per_node:
                return dict(self._latest_per_node[clean])
            # Check NODE-X match
            alt_key = f"NODE-{clean}"
            if alt_key in self._latest_per_node:
                return dict(self._latest_per_node[alt_key])
            # Check prefix/suffix
            for k, pkt in self._latest_per_node.items():
                if clean == k or clean in k:
                    return dict(pkt)
            return None

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_packets": self._total_packets,
                "active_nodes": sorted(list(self._active_nodes)),
                "log_size": len(self._telemetry_log),
            }


shared_state = SharedTelemetryState(max_history=200)


# ─────────────────────────────────────────────────────────────────────────────
# 4. BACKGROUND SERIAL READER THREAD
# ─────────────────────────────────────────────────────────────────────────────
class SerialReaderWorker(threading.Thread):
    """Background daemon thread reading physical Ground Node ESP32 serial data."""

    def __init__(self, port: str, baud: int, timeout: float, state: SharedTelemetryState) -> None:
        super().__init__(name="AeroGuard-SerialWorker", daemon=True)
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.state = state
        self._stop_event = threading.Event()
        self._ser: Optional[serial.Serial] = None
        self._connected = False

    def run(self) -> None:
        last_log_time = 0.0
        while not self._stop_event.is_set():
            # Attempt serial connection
            try:
                self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
                self._connected = True
                print(f"\033[32m[SERIAL] Connected to {self.port} @ {self.baud} baud\033[0m")
                logger.info(f"[SERIAL] Connected to {self.port} @ {self.baud}")

                while not self._stop_event.is_set():
                    try:
                        line = self._ser.readline().decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue

                        if line.startswith("MLDATA,"):
                            packet = parse_mldata(line)
                            if packet:
                                self.state.add_packet(packet)
                                pl = packet["payload"]
                                gps = pl.get("gps", {})
                                lat = gps.get("latitude", 0.0)
                                lon = gps.get("longitude", 0.0)
                                alt = pl.get("altitude", 0.0)
                                stat = pl.get("status", "ONLINE")
                                print(
                                    f"[TELEMETRY] {packet['src']} | "
                                    f"GPS: {lat:.4f}, {lon:.4f} | "
                                    f"ALT: {alt:.1f}m | "
                                    f"STATUS: {stat}"
                                )
                    except (serial.SerialException, OSError) as e:
                        print(f"\033[33m[SERIAL] Device disconnected from {self.port}: {e}\033[0m")
                        logger.warning(f"[SERIAL] Device disconnected: {e}")
                        break

            except (serial.SerialException, OSError):
                self._connected = False
                now = time.time()
                if now - last_log_time > 8.0:
                    print(f"\033[33m[SERIAL] {self.port} unavailable — waiting for ESP32...\033[0m")
                    last_log_time = now
                self._stop_event.wait(2.0)
            finally:
                if self._ser and self._ser.is_open:
                    try:
                        self._ser.close()
                    except Exception:
                        pass
                self._ser = None
                self._connected = False

    def stop(self) -> None:
        self._stop_event.set()
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. DIRECT SHARED-STATE TELEMETRY PROVIDER FOR VOICE AGENT
# ─────────────────────────────────────────────────────────────────────────────
from voice_layer.aero_api import NodeTelemetry
from voice_layer.agent import AeroGuardAgent
from voice_layer.config import VoiceConfig
from voice_layer.llm.client import create_llm_client
from voice_layer.session import (
    VoiceIntent,
    VoiceSessionManager,
    VoiceState,
    detect_voice_intent,
)
from voice_layer.tools.provider import BaseTelemetryProvider
from voice_layer.tools.registry import ToolRegistry
from voice_layer.tts import CartesiaTTS, TTSError


class SharedStateTelemetryProvider(BaseTelemetryProvider):
    """Zero-HTTP telemetry provider querying SharedTelemetryState directly."""

    def __init__(self, state: SharedTelemetryState) -> None:
        self.state = state

    def get_system_status(self) -> Dict[str, Any]:
        stats = self.state.get_stats()
        return {
            "status": "ONLINE",
            "ground_station_url": "internal://shared-state",
            "total_packets": stats["total_packets"],
            "telemetry_packets": stats["total_packets"],
            "emergency_packets": 0,
            "active_nodes_count": len(stats["active_nodes"]),
            "active_nodes": stats["active_nodes"],
            "is_mock": False,
        }

    def get_node_status(self, node_id: Any) -> Dict[str, Any]:
        pkt = self.state.get_node_packet(node_id)
        if pkt:
            pl = pkt.get("payload", {})
            return {
                "node_id": str(node_id),
                "matched_src": pkt.get("src"),
                "status": pl.get("status", "ONLINE"),
                "battery_pct": pl.get("node_battery_pct") or pl.get("battery"),
                "last_seen": pkt.get("time"),
                "is_mock": False,
            }
        return {
            "node_id": str(node_id),
            "status": "OFFLINE",
            "battery_pct": None,
            "last_seen": None,
            "is_mock": False,
        }

    def get_node_telemetry(self, node_id: Any) -> Optional[NodeTelemetry]:
        pkt = self.state.get_node_packet(node_id)
        if not pkt:
            return None

        pl = pkt.get("payload", {})
        return NodeTelemetry(
            node_id=pkt.get("src", str(node_id)),
            packet_type="TELEMETRY",
            timestamp=pkt.get("time", "RECENT"),
            online=True,
            temperature_c=pl.get("temperature"),
            pressure_hpa=pl.get("pressure"),
            altitude_m=pl.get("altitude"),
            altitude_ft=pl.get("altitude_ft"),
            speed_knots=pl.get("speed_knots"),
            speed_kmh=pl.get("speed_knots") * 1.852 if pl.get("speed_knots") else None,
            heading_deg=pl.get("heading_deg") or pl.get("yaw"),
            roll_deg=pl.get("roll"),
            pitch_deg=pl.get("pitch"),
            yaw_deg=pl.get("yaw"),
            battery_pct=pl.get("node_battery_pct") or pl.get("battery"),
            fuel_kg=pl.get("fuel_kg"),
            raw_payload=pl,
        )

    def get_aircraft_status(self, aircraft_id: str) -> Optional[NodeTelemetry]:
        return self.get_node_telemetry(aircraft_id)

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        log = self.state.get_log()
        return [p for p in log if p.get("type") == "EMERGENCY"]

    def get_emergency_status(self) -> Dict[str, Any]:
        alerts = self.get_active_alerts()
        return {
            "has_emergencies": len(alerts) > 0,
            "active_alerts_count": len(alerts),
            "alerts": alerts,
        }

    # Compatibility methods for rule-based handlers
    def get_packets(self) -> List[Dict[str, Any]]:
        return self.state.get_log()


# ─────────────────────────────────────────────────────────────────────────────
# 6. UNIFIED FASTAPI APPLICATION & LIFESPAN
# ─────────────────────────────────────────────────────────────────────────────
class SerialData(BaseModel):
    data: str


class QueryRequest(BaseModel):
    text: str


class QueryResponse(BaseModel):
    query: str
    response: str
    audio_base64: Optional[str] = None
    mime_type: Optional[str] = None
    should_exit: bool = False
    intent: str = "NORMAL_REQUEST"
    state: str = "LISTENING"
    session_active: bool = False


# Global voice agent services container
class UnifiedVoiceService:
    def __init__(self, state: SharedTelemetryState) -> None:
        self.config = VoiceConfig.from_env()
        self.provider = SharedStateTelemetryProvider(state)
        self.tools = ToolRegistry(provider=self.provider)
        self.llm = create_llm_client(self.config)
        self.agent = AeroGuardAgent(
            api_client=self.provider,  # acts as in-memory client
            llm_client=self.llm,
            tool_registry=self.tools,
        )
        self.tts = CartesiaTTS(self.config)
        self.session_manager = VoiceSessionManager()


voice_service: Optional[UnifiedVoiceService] = None
serial_worker: Optional[SerialReaderWorker] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global voice_service, serial_worker
    # 1. Initialize Voice Engine
    voice_service = UnifiedVoiceService(shared_state)
    logger.info("[STARTUP] In-memory Voice Engine initialized.")

    # 2. Launch background serial thread
    serial_worker = SerialReaderWorker(
        port=SERIAL_PORT,
        baud=SERIAL_BAUD,
        timeout=SERIAL_TIMEOUT,
        state=shared_state,
    )
    serial_worker.start()
    logger.info(f"[STARTUP] Serial worker started on {SERIAL_PORT}.")

    yield

    # Shutdown
    if serial_worker:
        logger.info("[SHUTDOWN] Stopping serial reader worker...")
        serial_worker.stop()
        serial_worker.join(timeout=2.0)
    logger.info("[SHUTDOWN] AeroGuard shutdown completed.")


app = FastAPI(
    title="AeroGuard Unified Real-Hardware System",
    description="Single-file integrated runtime for physical ESP32 Ground Station and AGY voice",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# 7. HTTP ROUTES: WEB UI, TELEMETRY API, VOICE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def serve_ui():
    """Serve the unified ground station dashboard."""
    if not UI_PATH.exists():
        raise HTTPException(status_code=404, detail="web_UI.html not found.")
    with open(UI_PATH, "r", encoding="utf-8", errors="replace") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)


@app.get("/api/health")
def api_health():
    return {
        "system": "AeroGuard",
        "status": "online",
        "serial_port": SERIAL_PORT,
        "serial_connected": serial_worker._connected if serial_worker else False,
    }


@app.get("/api/log")
def get_telemetry_log():
    """Return the rolling telemetry buffer for the web UI."""
    return shared_state.get_log()


@app.get("/api/latest")
def get_latest_telemetry():
    """Return the most recent telemetry packet."""
    latest = shared_state.get_latest()
    if not latest:
        return {"status": "no_data", "data": None}
    return latest


@app.post("/api/serial")
def ingest_serial_line(payload: SerialData):
    """Support direct/test serial injection via HTTP without physical COM port."""
    packet = parse_mldata(payload.data)
    if not packet:
        return {"status": "ignored", "data": payload.data}
    shared_state.add_packet(packet)
    return {"status": "received", "packet": packet}


@app.get("/voice/health")
def voice_health():
    svc = voice_service
    if not svc:
        return {"status": "initializing"}
    return {
        "status": "healthy",
        "telemetry_mode": "hardware_shared_state",
        "is_llm_configured": svc.config.is_llm_configured,
        "is_cartesia_configured": svc.config.is_cartesia_configured,
        "session_active": svc.session_manager.is_active(),
        "state": svc.session_manager.get_state().value,
    }


@app.post("/voice/session/start")
def start_voice_session():
    if not voice_service:
        raise HTTPException(status_code=503, detail="Voice engine initializing.")
    state = voice_service.session_manager.start_session()
    return {"session_active": True, "state": state.value}


@app.post("/voice/session/stop")
def stop_voice_session():
    if not voice_service:
        raise HTTPException(status_code=503, detail="Voice engine initializing.")
    state = voice_service.session_manager.stop_session()
    return {"session_active": False, "state": state.value}


@app.get("/voice/session/status")
def voice_session_status():
    if not voice_service:
        return {"session_active": False, "state": "OFFLINE"}
    return {
        "session_active": voice_service.session_manager.is_active(),
        "state": voice_service.session_manager.get_state().value,
    }


@app.post("/voice/query", response_model=QueryResponse)
def handle_voice_query(req: QueryRequest):
    svc = voice_service
    if not svc:
        raise HTTPException(status_code=503, detail="Voice engine initializing.")

    clean_query = req.text.strip()
    if not clean_query:
        raise HTTPException(status_code=400, detail="Query text cannot be empty.")

    logger.info(f"[VOICE] Speech query received: '{clean_query}'")

    # Intent classification
    intent, processed_text = detect_voice_intent(
        clean_query,
        in_session=svc.session_manager.is_active() or True,
    )

    if intent == VoiceIntent.IGNORE:
        return QueryResponse(
            query=clean_query,
            response="",
            audio_base64=None,
            mime_type=None,
            should_exit=False,
            intent=VoiceIntent.IGNORE.value,
            state=VoiceState.LISTENING.value if svc.session_manager.is_active() else VoiceState.OFFLINE.value,
            session_active=svc.session_manager.is_active(),
        )

    if intent == VoiceIntent.END_SESSION:
        svc.session_manager.transition_to(VoiceState.STOPPING)
        exit_text = "Shutting down AeroGuard voice assistant. Radar offline."
        audio_b64 = None
        mime = None
        if svc.config.is_cartesia_configured:
            try:
                wb = svc.tts.synthesize_bytes(exit_text)
                if wb:
                    audio_b64 = base64.b64encode(wb).decode("ascii")
                    mime = "audio/wav"
            except Exception:
                pass
        svc.session_manager.stop_session()
        return QueryResponse(
            query=clean_query,
            response=exit_text,
            audio_base64=audio_b64,
            mime_type=mime,
            should_exit=True,
            intent=VoiceIntent.END_SESSION.value,
            state=VoiceState.OFFLINE.value,
            session_active=False,
        )

    # Process via AGY Agent with in-memory telemetry provider
    svc.session_manager.transition_to(VoiceState.PROCESSING)
    try:
        response_text, should_exit = svc.agent.process_query(processed_text or clean_query)
    except Exception as e:
        logger.error(f"[VOICE] Agent execution failed: {e}", exc_info=True)
        response_text = "An internal error occurred while processing the telemetry request."
        should_exit = False

    svc.session_manager.transition_to(VoiceState.SPEAKING)

    # Audio synthesis via Cartesia Sonic
    audio_b64 = None
    mime = None
    if svc.config.is_cartesia_configured and response_text:
        try:
            wb = svc.tts.synthesize_bytes(response_text)
            if wb:
                audio_b64 = base64.b64encode(wb).decode("ascii")
                mime = "audio/wav"
        except TTSError as e:
            logger.warning(f"[VOICE] Cartesia TTS error: {e}")
        except Exception as e:
            logger.warning(f"[VOICE] Audio synthesis error: {e}")

    if should_exit:
        svc.session_manager.stop_session()
        return QueryResponse(
            query=clean_query,
            response=response_text,
            audio_base64=audio_b64,
            mime_type=mime,
            should_exit=True,
            intent=VoiceIntent.END_SESSION.value,
            state=VoiceState.OFFLINE.value,
            session_active=False,
        )

    return QueryResponse(
        query=clean_query,
        response=response_text,
        audio_base64=audio_b64,
        mime_type=mime,
        should_exit=False,
        intent=VoiceIntent.NORMAL_REQUEST.value,
        state=VoiceState.SPEAKING.value,
        session_active=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("\033[36m" + "=" * 60 + "\033[0m")
    print("\033[1;32m   🛰  AEROGUARD UNIFIED REAL-HARDWARE RUNTIME ENGINE\033[0m")
    print("\033[36m" + "=" * 60 + "\033[0m")
    print(f"   Web Dashboard:  \033[1;34mhttp://{SERVER_HOST}:{SERVER_PORT}\033[0m")
    print(f"   Serial Port:    \033[1;33m{SERIAL_PORT} @ {SERIAL_BAUD} baud\033[0m")
    print(f"   Telemetry API:  \033[1;34mhttp://{SERVER_HOST}:{SERVER_PORT}/api/log\033[0m")
    print("   Voice Agent:    \033[1;32mREADY (Groq LLM + Cartesia Sonic TTS)\033[0m")
    print("   Physical ESP32: \033[1;35mWAITING FOR HARDWARE SERIAL STREAM\033[0m")
    print("\033[36m" + "=" * 60 + "\033[0m\n")

    try:
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    except KeyboardInterrupt:
        print("\n\033[33m[SHUTDOWN] Interrupted by operator. Closing AeroGuard...\033[0m")


if __name__ == "__main__":
    main()
