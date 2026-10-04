"""Node telemetry and status tools for AeroGuard."""

from typing import Any, Dict, Optional
from voice_layer.tools.provider import BaseTelemetryProvider


def get_node_status(provider: BaseTelemetryProvider, node_id: Any) -> Dict[str, Any]:
    """Retrieve the operational online/offline status and battery of a specific mesh node.

    Args:
        provider: Telemetry data provider.
        node_id: Identifier of the node (e.g. 1, 2, 3, "Node 1").

    Returns:
        Dict with node status, battery level, and last seen information.
    """
    try:
        raw = provider.get_node_status(node_id)
        return {
            "node_id": str(node_id),
            "status": raw.get("status", "OFFLINE"),
            "battery_pct": raw.get("battery_pct"),
            "last_seen": raw.get("last_seen"),
            "is_mock": raw.get("is_mock", False),
        }
    except Exception as e:
        return {
            "node_id": str(node_id),
            "error": "Failed to retrieve node status",
            "details": str(e),
            "status": "OFFLINE",
        }


def get_node_telemetry(
    provider: BaseTelemetryProvider,
    node_id: Any,
    field: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve actual sensor telemetry for a specific AeroGuard node.

    Args:
        provider: Telemetry data provider.
        node_id: Identifier of the node (e.g. 1, 2, 3).
        field: Specific telemetry metric to query (e.g. 'battery', 'temperature', 'altitude', 'pressure', 'speed').

    Returns:
        Dict containing the exact measured value and unit, or all telemetry if field is omitted.
    """
    try:
        telem = provider.get_node_telemetry(node_id)
        if not telem or not telem.online:
            clean_str = str(node_id).strip().upper()
            if clean_str.startswith(("AC", "AG", "FLIGHT", "AIRCRAFT")):
                telem = provider.get_aircraft_status(clean_str)

        if not telem or not telem.online:
            return {
                "node_id": str(node_id),
                "available": False,
                "message": f"Node {node_id} is currently offline or has no telemetry recorded.",
            }

        # Field mapping dictionary
        alt_val = telem.altitude_ft if telem.altitude_ft is not None else telem.altitude_m
        alt_unit = "feet" if telem.altitude_ft is not None else "meters"
        spd_val = telem.speed_knots if telem.speed_knots is not None else telem.speed_kmh
        spd_unit = "knots" if telem.speed_knots is not None else "km/h"

        field_map = {
            "battery": (telem.battery_pct, "%"),
            "battery_pct": (telem.battery_pct, "%"),
            "power": (telem.battery_pct, "%"),
            "fuel": (telem.fuel_kg, "kg"),
            "fuel_kg": (telem.fuel_kg, "kg"),
            "temperature": (telem.temperature_c, "C"),
            "temp": (telem.temperature_c, "C"),
            "altitude": (alt_val, alt_unit),
            "alt": (alt_val, alt_unit),
            "height": (alt_val, alt_unit),
            "pressure": (telem.pressure_hpa, "hPa"),
            "barometer": (telem.pressure_hpa, "hPa"),
            "speed": (spd_val, spd_unit),
            "velocity": (spd_val, spd_unit),
            "heading": (telem.heading_deg, "degrees"),
            "bearing": (telem.heading_deg, "degrees"),
            "pitch": (telem.pitch_deg, "deg"),
            "roll": (telem.roll_deg, "deg"),
            "yaw": (telem.yaw_deg, "deg"),
        }

        if field:
            clean_field = field.strip().lower()
            if clean_field in field_map:
                val, unit = field_map[clean_field]
                if val is not None:
                    return {
                        "node_id": str(node_id),
                        "field": clean_field,
                        "value": val,
                        "unit": unit,
                        "available": True,
                    }
                else:
                    return {
                        "node_id": str(node_id),
                        "field": clean_field,
                        "value": None,
                        "unit": unit,
                        "available": False,
                        "message": f"Node {node_id} has not reported a {clean_field} reading.",
                    }

        # Return full telemetry summary if no specific field requested
        return {
            "node_id": str(node_id),
            "available": True,
            "battery": {"value": telem.battery_pct, "unit": "%"} if telem.battery_pct is not None else None,
            "temperature": {"value": telem.temperature_c, "unit": "C"} if telem.temperature_c is not None else None,
            "altitude": {"value": telem.altitude_m, "unit": "m"} if telem.altitude_m is not None else None,
            "pressure": {"value": telem.pressure_hpa, "unit": "hPa"} if telem.pressure_hpa is not None else None,
            "speed": {"value": telem.speed_knots, "unit": "knots"} if telem.speed_knots is not None else None,
            "heading": {"value": telem.heading_deg, "unit": "deg"} if telem.heading_deg is not None else None,
        }

    except Exception as e:
        return {
            "node_id": str(node_id),
            "available": False,
            "error": "Failed to retrieve node telemetry",
            "details": str(e),
        }
