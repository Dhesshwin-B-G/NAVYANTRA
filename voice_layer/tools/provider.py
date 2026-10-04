"""Data provider abstraction for AeroGuard telemetry.

Supports:
- Live mode: queries AeroGuard Ground Station backend (GET /api/log).
- Mock mode: provides deterministic simulated telemetry when backend is offline.
"""

import abc
import logging
from typing import Any, Dict, List, Optional

from voice_layer.aero_api import AeroGuardAPIError, AeroGuardClient, NodeTelemetry

logger = logging.getLogger("AeroGuard.DataProvider")


class BaseTelemetryProvider(abc.ABC):
    """Abstract interface for retrieving telemetry and status data."""

    @abc.abstractmethod
    def get_system_status(self) -> Dict[str, Any]:
        """Return system-wide status."""
        pass

    @abc.abstractmethod
    def get_node_status(self, node_id: Any) -> Dict[str, Any]:
        """Return status for a specific node (online/offline, battery, etc.)."""
        pass

    @abc.abstractmethod
    def get_node_telemetry(self, node_id: Any) -> Optional[NodeTelemetry]:
        """Return telemetry metrics for a node."""
        pass

    @abc.abstractmethod
    def get_aircraft_status(self, aircraft_id: str) -> Optional[NodeTelemetry]:
        """Return status and telemetry for an aircraft."""
        pass

    @abc.abstractmethod
    def get_active_alerts(self) -> List[Dict[str, Any]]:
        """Return list of active emergency alerts."""
        pass

    @abc.abstractmethod
    def get_emergency_status(self) -> Dict[str, Any]:
        """Return emergency summary."""
        pass


class LiveTelemetryProvider(BaseTelemetryProvider):
    """Telemetry provider backed by live AeroGuard Ground Station API."""

    def __init__(self, client: AeroGuardClient) -> None:
        self.client = client

    def get_system_status(self) -> Dict[str, Any]:
        return self.client.get_system_status()

    def get_node_status(self, node_id: Any) -> Dict[str, Any]:
        return self.client.get_node_status(node_id)

    def get_node_telemetry(self, node_id: Any) -> Optional[NodeTelemetry]:
        return self.client.get_node_telemetry(node_id)

    def get_aircraft_status(self, aircraft_id: str) -> Optional[NodeTelemetry]:
        return self.client.get_aircraft_status(aircraft_id)

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        return self.client.get_active_alerts()

    def get_emergency_status(self) -> Dict[str, Any]:
        return self.client.get_emergency_status()


class MockTelemetryProvider(BaseTelemetryProvider):
    """Deterministic mock telemetry provider for development and testing.

    Provides exact reference values:
    Node 1: online, battery 94%, temp 27.5 C, alt 120 m, pressure 1008 hPa
    Node 2: online, battery 87%, temp 28.5 C, alt 145 m, pressure 1005 hPa
    Node 3: offline
    Aircraft AG7423: tracked, alt 35000 ft, speed 480 knots
    """

    def __init__(self, node_3_online: bool = False, active_alerts: Optional[List[Dict[str, Any]]] = None) -> None:
        self.node_3_online = node_3_online
        self._alerts = active_alerts or []

    def get_system_status(self) -> Dict[str, Any]:
        active_nodes = ["1", "2"]
        if self.node_3_online:
            active_nodes.append("3")

        return {
            "status": "ONLINE (MOCK)",
            "ground_station_url": "mock://aeroguard-sim",
            "total_packets": 142,
            "telemetry_packets": 120,
            "emergency_packets": len(self._alerts),
            "active_nodes_count": len(active_nodes),
            "active_nodes": active_nodes,
            "is_mock": True,
        }

    def get_node_status(self, node_id: Any) -> Dict[str, Any]:
        nid = str(node_id).strip()
        if "1" in nid:
            return {
                "node_id": "1",
                "status": "ONLINE",
                "last_seen": "JUST NOW",
                "battery_pct": 94.0,
                "is_mock": True,
            }
        elif "2" in nid:
            return {
                "node_id": "2",
                "status": "ONLINE",
                "last_seen": "JUST NOW",
                "battery_pct": 87.0,
                "is_mock": True,
            }
        elif "3" in nid:
            if self.node_3_online:
                return {
                    "node_id": "3",
                    "status": "ONLINE",
                    "last_seen": "JUST NOW",
                    "battery_pct": 72.0,
                    "is_mock": True,
                }
            return {
                "node_id": "3",
                "status": "OFFLINE",
                "last_seen": None,
                "battery_pct": None,
                "is_mock": True,
            }
        return {
            "node_id": nid,
            "status": "OFFLINE",
            "last_seen": None,
            "battery_pct": None,
            "is_mock": True,
        }

    def get_node_telemetry(self, node_id: Any) -> Optional[NodeTelemetry]:
        nid = str(node_id).strip()
        if "AC001" in nid.upper():
            return NodeTelemetry(
                node_id="AC001",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                altitude_ft=32800.0,
                speed_knots=450.0,
                heading_deg=90.0,
                battery_pct=94.0,
                fuel_kg=15000.0,
            )
        elif "AC002" in nid.upper():
            return NodeTelemetry(
                node_id="AC002",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                altitude_ft=28500.0,
                speed_knots=420.0,
                heading_deg=180.0,
                battery_pct=91.0,
                fuel_kg=13200.0,
            )
        elif "1" in nid:
            return NodeTelemetry(
                node_id="1",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                temperature_c=27.5,
                pressure_hpa=1008.0,
                altitude_m=120.0,
                altitude_ft=393.7,
                speed_knots=0.0,
                battery_pct=94.0,
            )
        elif "2" in nid:
            return NodeTelemetry(
                node_id="2",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                temperature_c=28.5,
                pressure_hpa=1005.0,
                altitude_m=145.0,
                altitude_ft=475.7,
                speed_knots=0.0,
                battery_pct=87.0,
            )
        elif "3" in nid and self.node_3_online:
            return NodeTelemetry(
                node_id="3",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                temperature_c=25.0,
                pressure_hpa=1013.0,
                altitude_m=50.0,
                altitude_ft=164.0,
                speed_knots=0.0,
                battery_pct=72.0,
            )
        return None

    def get_aircraft_status(self, aircraft_id: str) -> Optional[NodeTelemetry]:
        clean = aircraft_id.strip().upper()
        if "AC001" in clean:
            return NodeTelemetry(
                node_id="AC001",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                altitude_ft=32800.0,
                speed_knots=450.0,
                heading_deg=90.0,
                battery_pct=94.0,
                fuel_kg=15000.0,
            )
        elif "AC002" in clean:
            return NodeTelemetry(
                node_id="AC002",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                altitude_ft=28500.0,
                speed_knots=420.0,
                heading_deg=180.0,
                battery_pct=91.0,
                fuel_kg=13200.0,
            )
        elif "7423" in clean or "AG" in clean or clean in ("AIRCRAFT", "FLIGHT"):
            return NodeTelemetry(
                node_id="AG7423",
                packet_type="TELEMETRY",
                timestamp="MOCK_T0",
                online=True,
                altitude_ft=35000.0,
                speed_knots=480.0,
                heading_deg=270.0,
                battery_pct=99.0,
                temperature_c=-45.0,
                fuel_kg=12500.0,
            )
        return None

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        return list(self._alerts)

    def get_emergency_status(self) -> Dict[str, Any]:
        if self._alerts:
            latest = self._alerts[0]
            return {
                "has_emergency": True,
                "count": len(self._alerts),
                "latest_type": latest.get("type", "EMERGENCY"),
                "latest_source": latest.get("source", "UNKNOWN"),
                "latest_message": latest.get("message", "Emergency beacon received"),
            }
        return {
            "has_emergency": False,
            "count": 0,
            "latest_type": None,
            "latest_source": None,
            "latest_message": None,
        }
