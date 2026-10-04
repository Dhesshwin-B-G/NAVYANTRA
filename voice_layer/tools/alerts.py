"""Emergency and alert tools for AeroGuard."""

from typing import Any, Dict, List
from voice_layer.tools.provider import BaseTelemetryProvider


def get_active_alerts(provider: BaseTelemetryProvider) -> Dict[str, Any]:
    """Retrieve all active emergency beacons, distress alerts, or warnings.

    Args:
        provider: Telemetry data provider.

    Returns:
        Dict detailing whether active emergencies exist, count, and alert items.
    """
    try:
        alerts = provider.get_active_alerts()
        status = provider.get_emergency_status()
        return {
            "has_alerts": bool(alerts),
            "alert_count": len(alerts),
            "alerts": alerts,
            "summary": status,
        }
    except Exception as e:
        return {
            "has_alerts": False,
            "alert_count": 0,
            "error": "Failed to retrieve alerts",
            "details": str(e),
        }
