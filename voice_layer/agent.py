"""AeroGuard Voice Command and Intent Agent.

Interprets transcribed user queries, dispatches structured command handlers,
queries the AeroGuard Ground Station API, and formats concise JARVIS-style responses.
"""

import abc
import enum
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .aero_api import AeroGuardAPIError, AeroGuardClient, NodeTelemetry
from .llm.client import BaseLLMClient, LLMError, LLMResponse, ToolCall
from .llm.prompts import SYSTEM_PROMPT
from .tools.registry import ToolRegistry

logger = logging.getLogger("AeroGuard.Agent")


class Intent(enum.Enum):
    """Supported AeroGuard Voice Intents."""
    SYSTEM_STATUS = "system_status"
    NODE_STATUS = "node_status"
    NODE_TELEMETRY = "node_telemetry"
    AIRCRAFT_STATUS = "aircraft_status"
    ALERTS = "alerts"
    HELP = "help"
    SHUTDOWN = "shutdown"
    GREETING = "greeting"
    UNKNOWN = "unknown"


@dataclass
class CommandContext:
    """Extracted parameters for a matched command."""
    intent: Intent
    raw_query: str
    node_id: Optional[str] = None
    aircraft_id: Optional[str] = None
    metric: Optional[str] = None  # e.g., 'temperature', 'altitude', 'battery', 'pressure', 'speed'


class BaseCommandHandler(abc.ABC):
    """Abstract base class for all AeroGuard command handlers."""

    @abc.abstractmethod
    def match(self, query: str) -> Optional[CommandContext]:
        """Check if this handler can process the given query."""
        pass

    @abc.abstractmethod
    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        """Execute the command and return a concise JARVIS-style response."""
        pass


def _extract_node_id(text: str) -> Optional[str]:
    """Extract node identifier from spoken text (e.g. 'Node 2', 'node one', 'node 3')."""
    # Number word mapping
    num_words = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5"}
    lower = text.lower()
    for word, digit in num_words.items():
        if f"node {word}" in lower or f"node-{word}" in lower:
            return digit

    match = re.search(r"node\s*[-_]?\s*(\d+)", lower)
    if match:
        return match.group(1)

    # Isolated digits if 'node' isn't explicitly spoken
    match = re.search(r"\b(\d+)\b", lower)
    if match:
        return match.group(1)

    return None


def _extract_aircraft_id(text: str) -> Optional[str]:
    """Extract aircraft identifier (e.g. 'aircraft 3', 'AG7423', 'aircraft AG 7423')."""
    lower = text.lower()
    ag_match = re.search(r"\b(ag\s*\d+)\b", lower)
    if ag_match:
        return ag_match.group(1).replace(" ", "").upper()

    ac_match = re.search(r"aircraft\s*[-_]?\s*([a-zA-Z0-9]+)", lower)
    if ac_match:
        return ac_match.group(1).upper()

    return None


class GreetingHandler(BaseCommandHandler):
    """Handles greetings and check-ins."""

    PATTERNS = [
        r"^(hello|hi|hey|good\s*(morning|afternoon|evening))\s*(aeroguard|assistant)?",
        r"^aeroguard$",
        r"^are you there",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        for pat in self.PATTERNS:
            if re.search(pat, q):
                return CommandContext(intent=Intent.GREETING, raw_query=query)
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        return "AeroGuard online and monitoring. Standing by for command."


class SystemStatusHandler(BaseCommandHandler):
    """Handles overall AeroGuard system status queries."""

    PATTERNS = [
        r"\bsystem status\b",
        r"\bhow is aeroguard\b",
        r"\bis the system online\b",
        r"\bstatus of (the )?system\b",
        r"\bground station status\b",
        r"\bsystem health\b",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        for pat in self.PATTERNS:
            if re.search(pat, q):
                return CommandContext(intent=Intent.SYSTEM_STATUS, raw_query=query)
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        try:
            status = client.get_system_status()
            node_count = status.get("active_nodes_count", 0)
            packets = status.get("total_packets", 0)
            emerg = status.get("emergency_packets", 0)

            if emerg > 0:
                return (
                    f"AeroGuard system is online with {node_count} active nodes and {packets} packets logged. "
                    f"Warning: {emerg} active emergency alerts recorded."
                )
            return (
                f"AeroGuard system is fully operational. "
                f"{node_count} mesh nodes active, {packets} telemetry packets received."
            )
        except AeroGuardAPIError:
            return "I can't reach the AeroGuard ground station right now."


class NodeStatusHandler(BaseCommandHandler):
    """Handles node connection and operational status queries."""

    PATTERNS = [
        r"\bstatus of node\b",
        r"\bhow is node\b",
        r"\bis node \w+ online\b",
        r"\bnode \w+ status\b",
        r"\bcheck node\b",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        # Ensure it's asking for status, not specific telemetry metrics like temperature
        if any(m in q for m in ["temperature", "altitude", "pressure", "speed", "heading", "pitch", "roll"]):
            return None

        for pat in self.PATTERNS:
            if re.search(pat, q):
                node_id = _extract_node_id(query)
                return CommandContext(intent=Intent.NODE_STATUS, raw_query=query, node_id=node_id)

        # Fallback: "node 1", "node 2"
        if re.match(r"^node\s*\d+$", q):
            node_id = _extract_node_id(query)
            return CommandContext(intent=Intent.NODE_STATUS, raw_query=query, node_id=node_id)

        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        if not ctx.node_id:
            return "Please specify which node number you want to check."

        try:
            res = client.get_node_status(ctx.node_id)
            if res.get("status") == "ONLINE":
                batt = res.get("battery_pct")
                if batt is not None:
                    return f"Node {ctx.node_id} is online. Battery is {batt:.0f} percent."
                return f"Node {ctx.node_id} is online and transmitting."
            else:
                return f"Node {ctx.node_id} is currently offline or unreachable."
        except AeroGuardAPIError:
            return f"I can't retrieve Node {ctx.node_id} status right now."


class NodeTelemetryHandler(BaseCommandHandler):
    """Handles queries for specific sensor metrics from a node."""

    METRICS = {
        "temperature": [r"\btemp(erature)?\b"],
        "pressure": [r"\bpress(ure)?\b", r"\bbaro(meter)?\b"],
        "altitude": [r"\balt(itude)?\b", r"\bheight\b"],
        "battery": [r"\bbatt(ery)?\b", r"\bpower\b"],
        "speed": [r"\bspeed\b", r"\bvelocity\b"],
        "heading": [r"\bheading\b", r"\bbearing\b", r"\bdirection\b"],
        "pitch": [r"\bpitch\b"],
        "roll": [r"\broll\b"],
    }

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        matched_metric = None
        for metric, patterns in self.METRICS.items():
            if any(re.search(p, q) for p in patterns):
                matched_metric = metric
                break

        if matched_metric:
            node_id = _extract_node_id(query)
            aircraft_id = _extract_aircraft_id(query)
            return CommandContext(
                intent=Intent.NODE_TELEMETRY,
                raw_query=query,
                node_id=node_id or (aircraft_id if not node_id else None),
                aircraft_id=aircraft_id,
                metric=matched_metric,
            )
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        target_id = ctx.node_id or ctx.aircraft_id or "1"
        try:
            telem = client.get_node_telemetry(target_id)
            if not telem:
                # Try aircraft lookup
                telem = client.get_aircraft_status(target_id)

            if not telem:
                return f"I can't retrieve telemetry for Node {target_id} right now."

            m = ctx.metric
            if m == "temperature":
                if telem.temperature_c is not None:
                    return f"Node {target_id} temperature is {telem.temperature_c:.1f} degrees Celsius."
                return f"Node {target_id} has not reported a temperature reading."

            elif m == "pressure":
                if telem.pressure_hpa is not None:
                    return f"Node {target_id} barometric pressure is {telem.pressure_hpa:.1f} hectopascals."
                return f"Node {target_id} has not reported atmospheric pressure."

            elif m == "altitude":
                if telem.altitude_m is not None:
                    return f"Node {target_id} altitude is {telem.altitude_m:.1f} meters."
                elif telem.altitude_ft is not None:
                    return f"Node {target_id} altitude is {telem.altitude_ft:.0f} feet."
                return f"Node {target_id} altitude is unavailable."

            elif m == "battery":
                if telem.battery_pct is not None:
                    return f"Node {target_id} battery level is {telem.battery_pct:.1f} percent."
                return f"Node {target_id} battery level is unavailable."

            elif m == "speed":
                if telem.speed_knots is not None:
                    return f"Node {target_id} speed is {telem.speed_knots:.0f} knots."
                elif telem.speed_kmh is not None:
                    return f"Node {target_id} speed is {telem.speed_kmh:.1f} kilometers per hour."
                return f"Node {target_id} speed is unavailable."

            elif m == "heading":
                if telem.heading_deg is not None:
                    return f"Node {target_id} heading is {telem.heading_deg:.1f} degrees."
                return f"Node {target_id} heading is unavailable."

            elif m == "pitch":
                if telem.pitch_deg is not None:
                    return f"Node {target_id} pitch is {telem.pitch_deg:.1f} degrees."
                return f"Node {target_id} pitch is unavailable."

            elif m == "roll":
                if telem.roll_deg is not None:
                    return f"Node {target_id} roll is {telem.roll_deg:.1f} degrees."
                return f"Node {target_id} roll is unavailable."

            return f"Node {target_id} is active."

        except AeroGuardAPIError:
            return f"I can't retrieve Node {target_id} telemetry right now."


class AircraftStatusHandler(BaseCommandHandler):
    """Handles specific aircraft queries (e.g. AG7423, Aircraft 3)."""

    PATTERNS = [
        r"\b(aircraft|flight)\s+([a-zA-Z0-9]+)\b",
        r"\b(show|get|what is)\s+(me\s+)?aircraft\b",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        if "aircraft" in q or "flight" in q or re.search(r"\bag\d+\b", q):
            # If asking specifically for a metric, let telemetry handler handle it
            if any(m in q for m in ["temperature", "altitude", "pressure", "speed"]):
                return None
            ac_id = _extract_aircraft_id(query) or _extract_node_id(query)
            return CommandContext(intent=Intent.AIRCRAFT_STATUS, raw_query=query, aircraft_id=ac_id)
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        ac_id = ctx.aircraft_id or "AG7423"
        try:
            status = client.get_aircraft_status(ac_id)
            if status:
                alt = f"{status.altitude_ft:.0f} feet" if status.altitude_ft else "nominal altitude"
                spd = f"{status.speed_knots:.0f} knots" if status.speed_knots else "nominal speed"
                return f"Aircraft {ac_id} is tracked. Altitude {alt}, speed {spd}."
            return f"No live telemetry found for aircraft {ac_id}."
        except AeroGuardAPIError:
            return f"I can't reach the ground station to check aircraft {ac_id}."


class AlertsHandler(BaseCommandHandler):
    """Handles emergency and alert queries."""

    PATTERNS = [
        r"\b(are there )?(any )?alerts\b",
        r"\b(any )?emergenc(y|ies)\b",
        r"\bshow active alerts\b",
        r"\bactive emergencies\b",
        r"\bdistress signals?\b",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        for pat in self.PATTERNS:
            if re.search(pat, q):
                return CommandContext(intent=Intent.ALERTS, raw_query=query)
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        try:
            emerg = client.get_emergency_status()
            if emerg.get("has_emergency"):
                src = emerg.get("latest_source", "UNKNOWN")
                etype = emerg.get("latest_type", "EMERGENCY")
                msg = emerg.get("latest_message", "")
                return f"Alert: {etype} reported from {src}. {msg}"
            return "There are no active emergency alerts. All monitored systems nominal."
        except AeroGuardAPIError:
            return "I can't retrieve emergency alerts right now. Ground station unreachable."


class HelpHandler(BaseCommandHandler):
    """Provides voice assistance instructions."""

    PATTERNS = [
        r"\bwhat can you do\b",
        r"\bhelp\b",
        r"\bcommand list\b",
        r"\bavailable commands\b",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        for pat in self.PATTERNS:
            if re.search(pat, q):
                return CommandContext(intent=Intent.HELP, raw_query=query)
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        return (
            "You can ask for system status, node status such as Node 1 or 2, "
            "node telemetry like temperature or altitude, aircraft status, "
            "active emergencies, or stop the voice assistant."
        )


class ShutdownHandler(BaseCommandHandler):
    """Handles assistant termination commands."""

    PATTERNS = [
        r"\bstop voice assistant\b",
        r"\bshutdown voice layer\b",
        r"\bquit\b",
        r"\bexit voice\b",
        r"\bstop listening\b",
        r"\bpower down\b",
    ]

    def match(self, query: str) -> Optional[CommandContext]:
        q = query.strip().lower()
        for pat in self.PATTERNS:
            if re.search(pat, q):
                return CommandContext(intent=Intent.SHUTDOWN, raw_query=query)
        return None

    def execute(self, ctx: CommandContext, client: AeroGuardClient) -> str:
        return "Shutting down AeroGuard voice assistant. Radar offline."


class AeroGuardAgent:
    """Core command and intent orchestration agent for AeroGuard.

    Supports:
    - LLM-powered tool calling and JARVIS natural language synthesis.
    - Deterministic rule-based fallback when LLM is offline or unconfigured.
    """

    def __init__(
        self,
        api_client: AeroGuardClient,
        llm_client: Optional[BaseLLMClient] = None,
        tool_registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.api = api_client
        self.llm = llm_client
        self.tools = tool_registry
        self.handlers: List[BaseCommandHandler] = [
            GreetingHandler(),
            SystemStatusHandler(),
            NodeStatusHandler(),
            NodeTelemetryHandler(),
            AircraftStatusHandler(),
            AlertsHandler(),
            HelpHandler(),
            ShutdownHandler(),
        ]

    def parse_intent(self, query: str) -> Tuple[Intent, Optional[BaseCommandHandler], Optional[CommandContext]]:
        """Parse query and find matching rule-based command handler."""
        clean = query.strip()
        if not clean:
            return Intent.UNKNOWN, None, None

        for handler in self.handlers:
            ctx = handler.match(clean)
            if ctx:
                return ctx.intent, handler, ctx

        return Intent.UNKNOWN, None, CommandContext(intent=Intent.UNKNOWN, raw_query=clean)

    def _process_rule_based(self, clean: str) -> Tuple[str, bool]:
        """Execute legacy rule-based intent parsing and response generation."""
        intent, handler, ctx = self.parse_intent(clean)
        node_str = f" node={ctx.node_id}" if ctx and ctx.node_id else ""
        metric_str = f" metric={ctx.metric}" if ctx and ctx.metric else ""
        logger.info(f"[RULE_BASED] intent={intent.value}{node_str}{metric_str} query='{clean}'")

        if intent == Intent.SHUTDOWN:
            return handler.execute(ctx, self.api), True  # type: ignore

        if handler and ctx:
            try:
                response = handler.execute(ctx, self.api)
                return response, False
            except Exception as e:
                logger.error(f"[AGENT] Error executing handler for {intent}: {e}")
                return "An internal error occurred while processing that command.", False

        # Fallback for unrecognized commands
        logger.warning(f"[AGENT] Unrecognized command: '{clean}'")
        return "Command not recognized. Say 'help' to hear available commands.", False

    def _process_with_llm(self, clean: str) -> Tuple[str, bool]:
        """Orchestrate LLM tool calling and response synthesis."""
        if not self.llm or not self.tools:
            return self._process_rule_based(clean)

        # Check for immediate shutdown keywords
        if any(w in clean.lower() for w in ["stop voice assistant", "shutdown voice layer", "power down"]):
            return "Shutting down AeroGuard voice assistant. Radar offline.", True

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": clean},
        ]

        tool_defs = self.tools.get_tool_definitions()
        print("[AGY] Processing query...")
        logger.info(f"[LLM] Processing query '{clean}' with {len(tool_defs)} tools available...")

        resp = self.llm.generate_response(messages, tools=tool_defs)

        # Handle tool calling loop if requested by LLM
        if resp.tool_calls:
            logger.info(f"[LLM] Model requested {len(resp.tool_calls)} tool call(s).")
            # Build assistant message with tool calls
            asst_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else tc.arguments,
                        },
                    }
                    for tc in resp.tool_calls
                ],
            }
            messages.append(asst_msg)

            for tc in resp.tool_calls:
                print(f"[AGY] Tool call: {tc.name}")
                tool_output = self.tools.execute_tool(tc.name, tc.arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_output),
                })

            # Get final synthesis from LLM
            final_resp = self.llm.generate_response(messages)
            text = (final_resp.content or "").strip()
        else:
            text = (resp.content or "").strip()

        print(f"[AGY] Response: {text}")

        if not text:
            logger.warning("[LLM] Empty response from model. Falling back to rule-based.")
            return self._process_rule_based(clean)

        # Check if model signaled shutdown
        should_shutdown = "shutting down aeroguard voice assistant" in text.lower() or "radar offline" in text.lower()
        return text, should_shutdown

    def process_query(self, query: str) -> Tuple[str, bool]:
        """Process transcribed speech input and return (response_text, should_shutdown).

        Args:
            query: Recognized speech text.

        Returns:
            Tuple of (response_string, is_shutdown_requested).
        """
        clean = query.strip()
        if not clean:
            return "I didn't catch that. Please repeat your command.", False

        # If LLM and tools are configured, attempt LLM processing first
        if self.llm and self.tools:
            try:
                return self._process_with_llm(clean)
            except Exception as e:
                logger.warning(f"[AGENT] LLM processing failed ({e}). Activating rule-based fallback.")
                return self._process_rule_based(clean)

        # Otherwise run rule-based processing directly
        return self._process_rule_based(clean)
