"""AeroGuard Ground Station API client.

Communicates with the existing AeroGuard Python Ground Station backend.
Reuses the exact endpoint polled by src/web_UI.html:
    GET /api/log

Endpoints discovered in AeroGuard:
    - GET /api/log : Returns list of LoRa telemetry, emergency, and heartbeat packets.

Endpoints not currently implemented by Ground Station backend:
    - POST /api/command : (TODO: Planned for future bi-directional aircraft command uplink)
    - GET /api/nodes/<id> : Handled client-side via filtering /api/log packets.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("AeroGuard.API")


class AeroGuardAPIError(Exception):
    """Raised when an AeroGuard API call fails or backend is unreachable."""
    pass


@dataclass
class NodeTelemetry:
    """Parsed telemetry record for an AeroGuard mesh node or aircraft."""
    node_id: str
    packet_type: str
    timestamp: str
    online: bool
    temperature_c: Optional[float] = None
    pressure_hpa: Optional[float] = None
    altitude_m: Optional[float] = None
    altitude_ft: Optional[float] = None
    speed_knots: Optional[float] = None
    speed_kmh: Optional[float] = None
    heading_deg: Optional[float] = None
    roll_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    yaw_deg: Optional[float] = None
    battery_pct: Optional[float] = None
    fuel_kg: Optional[float] = None
    raw_payload: Optional[Dict[str, Any]] = None


class AeroGuardClient:
    """Client for querying the AeroGuard Ground Station backend."""

    def __init__(self, base_url: str = "http://localhost:8080", timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> "AeroGuardClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def get_packets(self) -> List[Dict[str, Any]]:
        """Fetch raw packets from the existing AeroGuard /api/log endpoint.

        Returns:
            List of packet dictionaries.

        Raises:
            AeroGuardAPIError: If Ground Station is unreachable or returns an error.
        """
        endpoint = "/api/log"
        logger.debug(f"[AEROGUARD] GET {self.base_url}{endpoint}")
        print(f"[API] GET {endpoint}")
        try:
            response = self._client.get(endpoint)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                logger.warning(f"[AEROGUARD] Expected list from /api/log, got {type(data)}")
                return []
            logger.debug(f"[AEROGUARD] Retrieved {len(data)} packets from Ground Station.")
            return data
        except httpx.ConnectError as e:
            logger.error(f"[AEROGUARD] Connection refused at {self.base_url}: {e}")
            raise AeroGuardAPIError("AeroGuard Ground Station is offline or unreachable.") from e
        except httpx.TimeoutException as e:
            logger.error(f"[AEROGUARD] Request timeout at {self.base_url}: {e}")
            raise AeroGuardAPIError("AeroGuard Ground Station request timed out.") from e
        except Exception as e:
            logger.error(f"[AEROGUARD] Failed to fetch /api/log: {e}")
            raise AeroGuardAPIError(f"AeroGuard Ground Station API error: {e}") from e

    def get_system_status(self) -> Dict[str, Any]:
        """Query overall AeroGuard system status.

        Returns:
            Dictionary with online status, packet count, active nodes, and active alerts.
        """
        packets = self.get_packets()
        active_nodes = set()
        emergency_count = 0
        telemetry_count = 0

        for p in packets:
            src = str(p.get("src", ""))
            ptype = str(p.get("type", "")).upper()
            if src:
                active_nodes.add(src)
            if ptype == "EMERGENCY":
                emergency_count += 1
            elif ptype in ("TELEMETRY", "NODE1_REAL"):
                telemetry_count += 1

        return {
            "status": "ONLINE",
            "ground_station_url": self.base_url,
            "total_packets": len(packets),
            "telemetry_packets": telemetry_count,
            "emergency_packets": emergency_count,
            "active_nodes_count": len(active_nodes),
            "active_nodes": sorted(list(active_nodes)),
        }

    def _normalize_id(self, node_id: Any) -> str:
        """Normalize node IDs (e.g. 'Node 2' -> '2', 'node_1' -> '1', 'AC001' -> 'AC001')."""
        s = str(node_id).strip()
        if re.match(r"^node[\s_-]?\d+$", s, re.IGNORECASE) or s.isdigit():
            m = re.search(r"(\d+)", s)
            return m.group(1) if m else s.upper()
        return s.upper()

    def _matches_node(self, target_id: str, src: str) -> bool:
        """Check if target node ID matches packet src."""
        target_norm = self._normalize_id(target_id)
        src_norm = self._normalize_id(src)
        if target_norm == src_norm:
            return True
        if target_norm in src.upper() or src.upper() in target_norm:
            return True
        return False

    def get_node_status(self, node_id: Any) -> Dict[str, Any]:
        """Get the latest connection and operational status for a specific node or aircraft.

        Args:
            node_id: Node or aircraft identifier (e.g. 1, 2, 'AC001', 'AC002').

        Returns:
            Dictionary with node status information.
        """
        packets = self.get_packets()
        target_str = str(node_id).strip().upper()
        # Find latest packet from this node (newest first)
        for p in reversed(packets):
            src = str(p.get("src", "")).strip()
            payload = p.get("payload") or {}
            ac_id = str(payload.get("aircraft_id", "")).strip()
            callsign = str(payload.get("callsign", "")).strip()

            src_u = src.upper()
            ac_u = ac_id.upper()
            cs_u = callsign.upper()

            matches = (
                target_str == src_u
                or target_str == ac_u
                or target_str == cs_u
                or self._matches_node(target_str, src)
                or (target_str in src_u and len(target_str) >= 3)
            )

            if matches:
                battery = (
                    payload.get("node_battery_pct")
                    or payload.get("battery")
                    or payload.get("batt")
                )
                return {
                    "node_id": ac_id or src or str(node_id),
                    "matched_src": src,
                    "status": "ONLINE",
                    "last_seen": p.get("time", "RECENT"),
                    "last_packet_type": p.get("type", "UNKNOWN"),
                    "battery_pct": battery,
                }

        return {
            "node_id": str(node_id),
            "status": "OFFLINE",
            "last_seen": None,
            "battery_pct": None,
        }

    def get_node_telemetry(self, node_id: Any) -> Optional[NodeTelemetry]:
        """Get latest telemetry for a specific node or aircraft (AC001, AC002, Node 1, etc.).

        Args:
            node_id: Node identifier (AC001, AC002, 1, 2, 3, etc.).

        Returns:
            NodeTelemetry object if telemetry is found, else None.
        """
        packets = self.get_packets()
        target_str = str(node_id).strip().upper()
        for p in reversed(packets):
            src = str(p.get("src", "")).strip()
            ptype = str(p.get("type", "")).upper()
            payload = p.get("payload") or {}
            ac_id = str(payload.get("aircraft_id", "")).strip()
            callsign = str(payload.get("callsign", "")).strip()

            src_u = src.upper()
            ac_u = ac_id.upper()
            cs_u = callsign.upper()

            matches = (
                target_str == src_u
                or target_str == ac_u
                or target_str == cs_u
                or self._matches_node(target_str, src)
                or (target_str in src_u and len(target_str) >= 3)
            )

            if matches and ptype in ("TELEMETRY", "NODE1_REAL"):
                gps = payload.get("gps") if isinstance(payload.get("gps"), dict) else {}
                alt_m = payload.get("altitude") or gps.get("altitude_m")
                return NodeTelemetry(
                    node_id=ac_id or src or str(node_id),
                    packet_type=ptype,
                    timestamp=p.get("time", "RECENT"),
                    online=True,
                    temperature_c=payload.get("temperature"),
                    pressure_hpa=payload.get("pressure"),
                    altitude_m=alt_m,
                    altitude_ft=payload.get("altitude_ft"),
                    speed_knots=payload.get("speed_knots"),
                    speed_kmh=payload.get("speed_kmh") or payload.get("speedKmh"),
                    heading_deg=payload.get("heading_deg") or payload.get("heading"),
                    roll_deg=payload.get("roll"),
                    pitch_deg=payload.get("pitch"),
                    yaw_deg=payload.get("yaw"),
                    battery_pct=payload.get("node_battery_pct") or payload.get("battery"),
                    fuel_kg=payload.get("fuel_kg"),
                    raw_payload=payload,
                )
        return None

    def get_aircraft_status(self, aircraft_id: str) -> Optional[NodeTelemetry]:
        """Get telemetry and status for a specific aircraft (e.g. AC001, AC002, AG7423).

        Args:
            aircraft_id: Aircraft identifier or callsign.

        Returns:
            NodeTelemetry object or None if not found.
        """
        packets = self.get_packets()
        clean_id = aircraft_id.strip().upper()

        for p in reversed(packets):
            src = str(p.get("src", "")).strip().upper()
            payload = p.get("payload") or {}
            ac_id = str(payload.get("aircraft_id", "")).strip().upper()
            callsign = str(payload.get("callsign", "")).strip().upper()

            if clean_id in (src, ac_id, callsign) or self._matches_node(clean_id, src) or (clean_id in src and len(clean_id) >= 3):
                ptype = str(p.get("type", "")).upper()
                gps = payload.get("gps") if isinstance(payload.get("gps"), dict) else {}
                alt_m = payload.get("altitude") or gps.get("altitude_m")
                return NodeTelemetry(
                    node_id=ac_id or src or clean_id,
                    packet_type=ptype,
                    timestamp=p.get("time", "RECENT"),
                    online=True,
                    altitude_ft=payload.get("altitude_ft"),
                    altitude_m=alt_m,
                    speed_knots=payload.get("speed_knots"),
                    heading_deg=payload.get("heading_deg") or payload.get("heading"),
                    battery_pct=payload.get("node_battery_pct") or payload.get("battery"),
                    fuel_kg=payload.get("fuel_kg"),
                    temperature_c=payload.get("temperature"),
                    raw_payload=payload,
                )
        return None

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        """Fetch all active emergency alerts from recent packets."""
        packets = self.get_packets()
        alerts = []
        for p in reversed(packets):
            if str(p.get("type", "")).upper() == "EMERGENCY":
                payload = p.get("payload") or {}
                alerts.append({
                    "time": p.get("time", "UNKNOWN"),
                    "source": p.get("src", "UNKNOWN"),
                    "type": payload.get("emergency_type", "EMERGENCY"),
                    "message": payload.get("message", "Emergency beacon received"),
                })
        return alerts

    def get_emergency_status(self) -> Dict[str, Any]:
        """Check whether there are any active emergencies in the system."""
        alerts = self.get_active_alerts()
        if alerts:
            latest = alerts[0]
            return {
                "has_emergency": True,
                "count": len(alerts),
                "latest_type": latest["type"],
                "latest_source": latest["source"],
                "latest_message": latest["message"],
            }
        return {
            "has_emergency": False,
            "count": 0,
            "latest_type": None,
            "latest_source": None,
            "latest_message": None,
        }

    # ============================================================
    # FUTURE COMMAND INTERFACES (TODO: Uplink control)
    # ============================================================
    def send_command(self, node_id: str, command: str) -> Dict[str, Any]:
        """TODO: Uplink control interface.

        AeroGuard Ground Station does not yet expose a POST /api/command endpoint.
        This interface is reserved for future hardware command uplink.
        """
        raise NotImplementedError(
            "Uplink command endpoint POST /api/command is not implemented on Ground Station. "
            "Telemetry is currently read-only."
        )
