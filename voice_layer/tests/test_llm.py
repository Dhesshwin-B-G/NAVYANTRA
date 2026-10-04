"""Unit tests for AeroGuard LLM integration, tool calling, and mock telemetry."""

import json
from unittest.mock import MagicMock, patch
import pytest

from voice_layer.aero_api import AeroGuardClient
from voice_layer.agent import AeroGuardAgent
from voice_layer.config import VoiceConfig
from voice_layer.llm.client import (
    BaseLLMClient,
    LLMError,
    LLMResponse,
    MockLLMClient,
    ToolCall,
    create_llm_client,
)
from voice_layer.tools.provider import LiveTelemetryProvider, MockTelemetryProvider
from voice_layer.tools.registry import ToolRegistry


@pytest.fixture
def mock_provider():
    """Deterministic mock provider."""
    return MockTelemetryProvider(node_3_online=False)


@pytest.fixture
def tool_registry(mock_provider):
    """Tool registry backed by mock provider."""
    return ToolRegistry(provider=mock_provider)


@pytest.fixture
def mock_llm():
    """Deterministic mock LLM client."""
    return MockLLMClient()


@pytest.fixture
def llm_agent(mock_provider, tool_registry, mock_llm):
    """AeroGuardAgent initialized with Mock LLM and ToolRegistry."""
    mock_api = MagicMock(spec=AeroGuardClient)
    return AeroGuardAgent(
        api_client=mock_api,
        llm_client=mock_llm,
        tool_registry=tool_registry,
    )


# 1. LLM configuration
def test_llm_configuration(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("LLM_API_KEY", "gsk_test_mock_12345")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.3")
    monkeypatch.setenv("AEROGUARD_DATA_MODE", "mock")

    cfg = VoiceConfig.from_env()
    assert cfg.llm_provider == "groq"
    assert cfg.llm_model == "llama-3.3-70b-versatile"
    assert cfg.llm_api_key == "gsk_test_mock_12345"
    assert cfg.llm_temperature == 0.3
    assert cfg.is_llm_configured
    assert cfg.is_mock_telemetry


# 2. LLM unavailable fallback
def test_llm_unavailable_fallback():
    mock_api = MagicMock(spec=AeroGuardClient)
    mock_api.get_system_status.return_value = {
        "status": "ONLINE",
        "active_nodes_count": 2,
        "total_packets": 50,
        "emergency_packets": 0,
    }

    failing_llm = MagicMock(spec=BaseLLMClient)
    failing_llm.generate_response.side_effect = LLMError("API Connection Refused")

    provider = MockTelemetryProvider()
    tools = ToolRegistry(provider=provider)

    agent = AeroGuardAgent(api_client=mock_api, llm_client=failing_llm, tool_registry=tools)
    response, exit_flag = agent.process_query("system status")

    assert not exit_flag
    # Proves fallback was activated and generated response via rule-based handler
    assert "AeroGuard system is fully operational" in response or "mesh nodes active" in response


# 3. Tool selection
def test_tool_selection(tool_registry):
    # Verify get_tool_definitions provides JSON schemas
    tool_defs = tool_registry.get_tool_definitions()
    tool_names = [t["function"]["name"] for t in tool_defs]
    assert "get_system_status" in tool_names
    assert "get_node_status" in tool_names
    assert "get_node_telemetry" in tool_names
    assert "get_aircraft_status" in tool_names
    assert "get_active_alerts" in tool_names

    # Test direct execution
    res = tool_registry.execute_tool("get_node_telemetry", {"node_id": "1", "field": "battery"})
    assert res.get("field") == "battery"
    assert res.get("value") == 94.0


# 4. Node 1 battery query
def test_node_1_battery_query(llm_agent):
    response, _ = llm_agent.process_query("What is the battery level of Node 1?")
    assert "94" in response
    assert "percent" in response.lower()


# 5. Node 2 temperature query
def test_node_2_temperature_query(llm_agent):
    response, _ = llm_agent.process_query("What is Node 2 temperature?")
    assert "28.5" in response
    assert "degrees" in response.lower() or "celsius" in response.lower()


# 6. Node 3 status query
def test_node_3_status_query(llm_agent):
    response, _ = llm_agent.process_query("Is Node 3 online?")
    assert "offline" in response.lower()


# 7. Aircraft status query
def test_aircraft_status_query(llm_agent):
    response, _ = llm_agent.process_query("What is the status of aircraft AG7423?")
    assert "AG7423" in response
    assert "35000" in response or "35 thousand" in response or "tracked" in response.lower()
    assert "480" in response


# 8. Emergency/alert query
def test_emergency_alert_query(llm_agent):
    response, _ = llm_agent.process_query("Are there any emergencies?")
    assert "no active emergency" in response.lower() or "nominal" in response.lower()


# 9. Unknown/general question
def test_unknown_general_question(llm_agent):
    response, _ = llm_agent.process_query("Hello AeroGuard")
    assert "online and monitoring" in response.lower() or "standing by" in response.lower()


# 10. Mock telemetry provider
def test_mock_telemetry_provider():
    provider = MockTelemetryProvider(node_3_online=False)

    # Node 1 checks
    n1_stat = provider.get_node_status("1")
    assert n1_stat["status"] == "ONLINE"
    assert n1_stat["battery_pct"] == 94.0

    n1_telem = provider.get_node_telemetry("1")
    assert n1_telem.temperature_c == 27.5
    assert n1_telem.altitude_m == 120.0
    assert n1_telem.pressure_hpa == 1008.0

    # Node 2 checks
    n2_stat = provider.get_node_status("2")
    assert n2_stat["status"] == "ONLINE"
    assert n2_stat["battery_pct"] == 87.0

    n2_telem = provider.get_node_telemetry("2")
    assert n2_telem.temperature_c == 28.5
    assert n2_telem.altitude_m == 145.0
    assert n2_telem.pressure_hpa == 1005.0

    # Node 3 checks
    n3_stat = provider.get_node_status("3")
    assert n3_stat["status"] == "OFFLINE"

    # Aircraft checks
    ac = provider.get_aircraft_status("AG7423")
    assert ac.altitude_ft == 35000.0
    assert ac.speed_knots == 480.0


# 11. Live API mode configuration
def test_live_api_mode_configuration(monkeypatch):
    monkeypatch.setenv("AEROGUARD_DATA_MODE", "live")
    cfg = VoiceConfig.from_env()
    assert not cfg.is_mock_telemetry
    assert cfg.aeroguard_data_mode == "live"

    client = MagicMock(spec=AeroGuardClient)
    provider = LiveTelemetryProvider(client)
    assert isinstance(provider, LiveTelemetryProvider)


# 12. Protection against fabricated telemetry
def test_protection_against_fabricated_telemetry(llm_agent, mock_provider):
    # Query metric for offline node 3
    tool_res = llm_agent.tools.execute_tool("get_node_telemetry", {"node_id": "3", "field": "temperature"})
    assert tool_res.get("available") is False
    assert "offline" in tool_res.get("message", "").lower()

    # Query unknown node 99
    stat_res = llm_agent.tools.execute_tool("get_node_status", {"node_id": "99"})
    assert stat_res.get("status") == "OFFLINE"
