"""System-level AeroGuard tools for LLM tool calling."""

from typing import Any, Dict
from voice_layer.tools.provider import BaseTelemetryProvider


def get_system_status(provider: BaseTelemetryProvider) -> Dict[str, Any]:
    """Retrieve the overall health and status of the AeroGuard mesh system.

    Returns:
        Dict containing status, active node count, packet count, and emergency count.
    """
    try:
        raw = provider.get_system_status()
        return {
            "status": raw.get("status", "UNKNOWN"),
            "active_nodes_count": raw.get("active_nodes_count", 0),
            "active_nodes": raw.get("active_nodes", []),
            "total_packets": raw.get("total_packets", 0),
            "emergency_packets": raw.get("emergency_packets", 0),
            "is_mock": raw.get("is_mock", False),
        }
    except Exception as e:
        return {
            "error": "Ground station unreachable",
            "details": str(e),
            "status": "OFFLINE",
        }
