"""Tool registry and dispatcher for AeroGuard LLM tool calling.

Generates standard JSON schema definitions for function calling and
dispatches execution to the corresponding tool implementations.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from voice_layer.tools.aircraft import get_aircraft_status
from voice_layer.tools.alerts import get_active_alerts
from voice_layer.tools.provider import BaseTelemetryProvider
from voice_layer.tools.system import get_system_status
from voice_layer.tools.telemetry import get_node_status, get_node_telemetry

logger = logging.getLogger("AeroGuard.Tools")


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "Get overall health, active node count, and packet counts of the AeroGuard ground station and mesh network.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_status",
            "description": "Get the operational connection status (ONLINE/OFFLINE) and battery level of a specific mesh node or aircraft (e.g. AC001, AC002, 1, 2, 3).",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "The node or aircraft identifier (e.g., 'AC001', 'AC002', '1', '2', '3').",
                    },
                },
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_telemetry",
            "description": "Get measured sensor telemetry from an AeroGuard aircraft or node (e.g. AC001, AC002, 1, 2). You MUST use this tool to answer any questions about battery, altitude, speed, fuel, heading, temperature, or pressure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "The aircraft or node identifier (e.g., 'AC001', 'AC002', '1', '2').",
                    },
                    "field": {
                        "type": "string",
                        "enum": ["battery", "altitude", "speed", "fuel", "heading", "temperature", "pressure"],
                        "description": "The specific metric requested by the user.",
                    },
                },
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_aircraft_status",
            "description": "Get live flight tracking information, altitude, speed, fuel, and status for an aircraft (e.g. AC001, AC002, AG7423).",
            "parameters": {
                "type": "object",
                "properties": {
                    "callsign": {
                        "type": "string",
                        "description": "Aircraft callsign or identifier (e.g. 'AC001', 'AC002', 'AG7423').",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_alerts",
            "description": "Check if there are any active emergency beacons, distress signals, or system alerts.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


class ToolRegistry:
    """Manages and executes AeroGuard tools with a telemetry provider."""

    def __init__(self, provider: BaseTelemetryProvider) -> None:
        self.provider = provider
        self._handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
            "get_system_status": self._exec_system_status,
            "get_node_status": self._exec_node_status,
            "get_node_telemetry": self._exec_node_telemetry,
            "get_aircraft_status": self._exec_aircraft_status,
            "get_active_alerts": self._exec_active_alerts,
        }

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return tool definitions formatted for LLM function calling."""
        return list(TOOL_DEFINITIONS)

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a tool call by function name and arguments.

        Args:
            name: The function name called by LLM.
            arguments: Dict of keyword arguments passed by LLM.

        Returns:
            Dict containing real or mock telemetry result.
        """
        handler = self._handlers.get(name)
        if not handler:
            logger.warning(f"[TOOLS] Unknown tool called: {name}")
            return {"error": f"Unknown tool '{name}'."}

        # Format clean tool log like: [TOOL] node_id=AC001 field=battery
        arg_str = " ".join(f"{k}={v}" for k, v in arguments.items())
        print(f"[TOOL] {arg_str}" if arg_str else f"[TOOL] {name}")
        logger.info(f"[TOOLS] Executing tool '{name}' with arguments {arguments}")
        try:
            return handler(arguments)
        except Exception as e:
            logger.error(f"[TOOLS] Error executing tool '{name}': {e}", exc_info=True)
            return {"error": f"Tool execution failed: {e}"}

    def _exec_system_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return get_system_status(self.provider)

    def _exec_node_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        node_id = args.get("node_id", "1")
        return get_node_status(self.provider, node_id)

    def _exec_node_telemetry(self, args: Dict[str, Any]) -> Dict[str, Any]:
        node_id = args.get("node_id", "1")
        field = args.get("field")
        return get_node_telemetry(self.provider, node_id, field=field)

    def _exec_aircraft_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        callsign = args.get("callsign", "AG7423")
        return get_aircraft_status(self.provider, callsign=callsign)

    def _exec_active_alerts(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return get_active_alerts(self.provider)
