"""Aircraft tracking and status tools for AeroGuard."""

from typing import Any, Dict
from voice_layer.tools.provider import BaseTelemetryProvider


def get_aircraft_status(provider: BaseTelemetryProvider, callsign: str = "AG7423") -> Dict[str, Any]:
    """Retrieve telemetry, tracking, and operational status for an aircraft.

    Args:
        provider: Telemetry data provider.
        callsign: Aircraft callsign or identifier (e.g. 'AG7423', '3').

    Returns:
        Dict with aircraft flight tracking metrics (altitude, speed, heading, status).
    """
    clean_callsign = str(callsign).strip().upper() or "AG7423"
    try:
        telem = provider.get_aircraft_status(clean_callsign)
        if not telem:
            telem = provider.get_node_telemetry(clean_callsign)

        if not telem:
            return {
                "callsign": clean_callsign,
                "tracked": False,
                "message": f"Aircraft {clean_callsign} is not currently tracked by ground radar.",
            }

        return {
            "callsign": clean_callsign,
            "tracked": True,
            "altitude_ft": telem.altitude_ft,
            "altitude_m": telem.altitude_m,
            "speed_knots": telem.speed_knots,
            "heading_deg": telem.heading_deg,
            "battery_pct": telem.battery_pct,
            "fuel_kg": telem.fuel_kg,
            "status": "TRACKED_NOMINAL",
        }
    except Exception as e:
        return {
            "callsign": clean_callsign,
            "tracked": False,
            "error": "Ground station query failed",
            "details": str(e),
        }
