"""LLM client abstraction for AeroGuard Voice Layer.

Supports:
- OpenAI-compatible chat completions API (Groq, OpenAI, Ollama, OpenRouter, etc.)
- Deterministic Mock LLM for hermetic offline testing and verification.
- Tool/function calling orchestration.
"""

import abc
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from voice_layer.config import VoiceConfig
from voice_layer.llm.prompts import SYSTEM_PROMPT
from voice_layer.tools.registry import ToolRegistry

logger = logging.getLogger("AeroGuard.LLM")


class LLMError(Exception):
    """Raised when an LLM API request fails."""
    pass


@dataclass
class ToolCall:
    """Represents a tool/function call requested by the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """Response returned from an LLM chat turn."""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)


class BaseLLMClient(abc.ABC):
    """Abstract base class for LLM providers."""

    @abc.abstractmethod
    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """Send chat messages and optional tool definitions to the LLM."""
        pass


class OpenAICompatibleLLMClient(BaseLLMClient):
    """Client for OpenAI-compatible Chat Completions endpoints (Groq, OpenAI, Ollama, OpenRouter)."""

    def __init__(
        self,
        api_key: Optional[str],
        base_url: str,
        model: str,
        temperature: float = 0.2,
        timeout: float = 8.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._client = httpx.Client(timeout=self.timeout)

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        print(f"[LLM] Model: {self.model}")
        print(f"[LLM] Request started to {self.base_url}/chat/completions")
        logger.info(f"[LLM] Model: {self.model}, request started to {self.base_url}/chat/completions")
        try:
            resp = self._client.post(url, headers=headers, json=payload)
            print(f"[LLM] Response status: {resp.status_code}")
            logger.info(f"[LLM] Response status: {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()

            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")

            tool_calls = []
            if "tool_calls" in message and message["tool_calls"]:
                for tc in message["tool_calls"]:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args_raw = fn.get("arguments", "{}")
                    try:
                        fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                    except json.JSONDecodeError:
                        fn_args = {}
                    tool_calls.append(ToolCall(id=tc.get("id", "call_1"), name=fn_name, arguments=fn_args))

            print(f"[LLM] Response received: {len(content) if content else 0} chars, {len(tool_calls)} tool calls")
            logger.info(f"[LLM] Response received: {len(content) if content else 0} chars, {len(tool_calls)} tool calls")
            return LLMResponse(content=content, tool_calls=tool_calls)

        except httpx.HTTPStatusError as e:
            logger.error(f"[LLM] HTTP error {e.response.status_code} from provider: {e.response.text}")
            raise LLMError(f"LLM API returned status {e.response.status_code}") from e
        except Exception as e:
            logger.error(f"[LLM] Request failed: {e}")
            raise LLMError(f"LLM communication error: {e}") from e


class MockLLMClient(BaseLLMClient):
    """Deterministic Mock LLM for offline testing and verification.

    Analyzes prompt intent and issues genuine tool calls to verify tool execution
    and response generation without making external network calls.
    """

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        last_msg = messages[-1] if messages else {}

        # 1. If previous turn was a tool output, synthesize concise JARVIS response
        if last_msg.get("role") == "tool":
            raw_content = last_msg.get("content", "{}")
            try:
                data = json.loads(raw_content)
            except Exception:
                data = {}

            # Handle get_node_telemetry synthesis
            if "field" in data and "value" in data:
                nid = data.get("node_id", "1")
                val = data.get("value")
                unit = data.get("unit", "")
                fld = data.get("field", "metric")
                if val is not None:
                    if fld == "battery":
                        return LLMResponse(content=f"The battery level of {nid} is {val:.0f} percent.")
                    elif fld in ("temperature", "temp"):
                        return LLMResponse(content=f"Node {nid} temperature is {val:.1f} degrees Celsius.")
                    elif fld in ("altitude", "height"):
                        return LLMResponse(content=f"{nid} altitude is {val:.0f} {unit}.")
                    elif fld in ("pressure", "barometer"):
                        return LLMResponse(content=f"Node {nid} barometric pressure is {val:.0f} hectopascals.")
                    elif fld in ("speed", "velocity"):
                        return LLMResponse(content=f"{nid} speed is {val:.0f} {unit}.")
                    elif fld in ("fuel", "fuel_kg"):
                        return LLMResponse(content=f"{nid} fuel level is {val:.0f} kilograms.")
                return LLMResponse(content=f"{nid} has not reported a {fld} reading.")

            # Handle get_node_status synthesis
            if "node_id" in data and "status" in data:
                nid = data["node_id"]
                st = data["status"]
                batt = data.get("battery_pct")
                if st == "ONLINE":
                    if batt is not None:
                        return LLMResponse(content=f"{nid} is online with battery at {batt:.0f} percent.")
                    return LLMResponse(content=f"{nid} is online and transmitting.")
                else:
                    return LLMResponse(content=f"Node {nid} is currently offline." if nid.isdigit() else f"{nid} is currently offline.")

            # Handle get_aircraft_status synthesis
            if "callsign" in data:
                cs = data["callsign"]
                if data.get("tracked"):
                    alt = data.get("altitude_ft", 0)
                    spd = data.get("speed_knots", 0)
                    return LLMResponse(content=f"Aircraft {cs} is tracked. Altitude {alt:.0f} feet, speed {spd:.0f} knots.")
                return LLMResponse(content=f"Aircraft {cs} is not currently tracked.")

            # Handle get_active_alerts synthesis
            if "has_alerts" in data or "summary" in data:
                has_alerts = data.get("has_alerts")
                if has_alerts:
                    return LLMResponse(content="Warning: Active emergency alert reported in the system.")
                return LLMResponse(content="There are no active emergency alerts. All monitored systems nominal.")

            # Handle get_system_status synthesis
            if "active_nodes_count" in data:
                cnt = data.get("active_nodes_count", 0)
                pkts = data.get("total_packets", 0)
                return LLMResponse(content=f"AeroGuard system is fully operational. {cnt} mesh nodes active, {pkts} packets received.")

            return LLMResponse(content="System information retrieved and verified.")

        # 2. First turn: user prompt analysis -> select appropriate tool
        user_text = last_msg.get("content", "").lower()

        # Extract target node / aircraft ID
        target_id = "1"
        m_ac = re.search(r"\b(ac\s*\d+|ag\s*\d+)\b", user_text, re.IGNORECASE)
        if m_ac:
            target_id = m_ac.group(1).replace(" ", "").upper()
        elif "2" in user_text or "two" in user_text:
            target_id = "2"
        elif "3" in user_text or "three" in user_text:
            target_id = "3"

        # Telemetry queries
        if "battery" in user_text:
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_telemetry", arguments={"node_id": target_id, "field": "battery"})])

        if any(w in user_text for w in ["temp", "temperature", "hot", "cold"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_telemetry", arguments={"node_id": target_id, "field": "temperature"})])

        if any(w in user_text for w in ["alt", "altitude", "height", "high"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_telemetry", arguments={"node_id": target_id, "field": "altitude"})])

        if any(w in user_text for w in ["speed", "velocity", "fast"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_telemetry", arguments={"node_id": target_id, "field": "speed"})])

        if any(w in user_text for w in ["fuel", "fuel level", "gas"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_telemetry", arguments={"node_id": target_id, "field": "fuel"})])

        if any(w in user_text for w in ["pressure", "barometer"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_telemetry", arguments={"node_id": target_id, "field": "pressure"})])

        # Node / Aircraft status queries
        if ("status" in user_text and ("node" in user_text or "ac" in user_text)) or "online" in user_text or "offline" in user_text:
            if target_id.startswith("AC"):
                return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_aircraft_status", arguments={"callsign": target_id})])
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_node_status", arguments={"node_id": target_id})])

        # Aircraft queries
        if any(w in user_text for w in ["aircraft", "flight", "ag7423", "plane"]):
            cs = target_id if target_id.startswith("AC") else "AG7423"
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_aircraft_status", arguments={"callsign": cs})])

        # Emergency/alerts
        if any(w in user_text for w in ["emergency", "emergencies", "alert", "alerts", "distress"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_active_alerts", arguments={})])

        # System status
        if any(w in user_text for w in ["system status", "how is aeroguard", "system online", "system health"]):
            return LLMResponse(tool_calls=[ToolCall(id="call_mock_1", name="get_system_status", arguments={})])

        # Conversational / Greetings
        if any(w in user_text for w in ["hello", "hi", "hey", "are you there"]):
            return LLMResponse(content="AeroGuard online and monitoring. Standing by for command.")

        # Help
        if any(w in user_text for w in ["what can you do", "help", "commands"]):
            return LLMResponse(content="I can monitor mesh nodes, report telemetry such as battery or temperature, track aircraft, and check emergency alerts.")

        # Exit / Shutdown
        if any(w in user_text for w in ["stop voice assistant", "shutdown", "quit", "exit"]):
            return LLMResponse(content="Shutting down AeroGuard voice assistant. Radar offline.")

        # Networking query
        if any(w in user_text for w in ["machine networking", "networking", "network"]):
            return LLMResponse(content="Machine networking is the interconnection of devices and mesh nodes to reliably exchange telemetry and control data.")

        # General unknown
        return LLMResponse(content="I am standing by to assist with AeroGuard operations. Please state your command.")


def create_llm_client(config: VoiceConfig) -> Optional[BaseLLMClient]:
    """Factory to create configured LLM client based on VoiceConfig settings.

    Returns:
        BaseLLMClient instance or None if LLM is disabled or unavailable.
    """
    provider = (config.llm_provider or "none").strip().lower()

    if provider in ("mock", "sim", "test"):
        logger.info("[LLM] Initialized Mock LLM client.")
        return MockLLMClient()

    if provider == "none" or not config.llm_api_key:
        logger.info(f"[LLM] Provider is '{provider}' with no valid key. LLM mode disabled (rule-based fallback active).")
        return None

    # Base URLs for common providers
    base_urls = {
        "groq": "https://api.groq.com/openai/v1",
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434/v1",
    }
    base_url = config.llm_api_base_url or base_urls.get(provider, "https://api.groq.com/openai/v1")

    logger.info(f"[LLM] Initializing OpenAI-compatible LLM client (provider={provider}, model={config.llm_model})")
    return OpenAICompatibleLLMClient(
        api_key=config.llm_api_key,
        base_url=base_url,
        model=config.llm_model,
        temperature=config.llm_temperature,
    )
