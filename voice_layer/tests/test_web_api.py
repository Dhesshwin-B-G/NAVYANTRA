"""Tests for the AeroGuard Voice Web API bridge (voice_layer.web_api)."""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from voice_layer.tools.provider import MockTelemetryProvider
from voice_layer.tools.registry import ToolRegistry
from voice_layer.agent import AeroGuardAgent
from voice_layer.web_api import app, get_service


@pytest.fixture
def mock_telemetry_service():
    """Ensure service uses MockTelemetryProvider so live Ground Station is not required during unit tests."""
    svc = get_service()
    orig_provider = svc.provider
    orig_registry = svc.tool_registry
    orig_agent = svc.agent

    # Use MockTelemetryProvider for deterministic offline testing
    mock_provider = MockTelemetryProvider()
    tool_reg = ToolRegistry(provider=mock_provider)
    svc.provider = mock_provider
    svc.tool_registry = tool_reg
    from voice_layer.aero_api import NodeTelemetry
    orig_api = svc.api_client
    mock_api = MagicMock()
    mock_api.get_node_telemetry.return_value = NodeTelemetry(
        node_id="1",
        packet_type="TELEMETRY",
        timestamp="12:00:00",
        online=True,
        battery_pct=94.5,
        altitude_ft=35000.0,
        altitude_m=10668.0,
        speed_knots=450.0,
    )
    mock_api.get_aircraft_status.return_value = NodeTelemetry(
        node_id="AC001",
        packet_type="TELEMETRY",
        timestamp="12:00:00",
        online=True,
        battery_pct=94.5,
        altitude_ft=35000.0,
        altitude_m=10668.0,
        speed_knots=450.0,
    )
    mock_api.get_node_status.return_value = {
        "node_id": "1",
        "status": "ONLINE",
        "battery_pct": 94.5,
    }
    mock_api.get_system_status.return_value = {
        "status": "ONLINE",
        "total_packets": 50,
        "active_nodes_count": 3,
    }
    mock_api.get_packets.return_value = []

    svc.api_client = mock_api
    svc.agent = AeroGuardAgent(
        api_client=mock_api,
        llm_client=None,
        tool_registry=tool_reg,
    )
    yield svc

    # Restore
    svc.api_client = orig_api
    svc.provider = orig_provider
    svc.tool_registry = orig_registry
    svc.agent = orig_agent


@pytest.fixture
def client(mock_telemetry_service):
    with TestClient(app) as test_client:
        yield test_client


def test_health_check(client):
    """Verify /voice/health returns operational status."""
    res = client.get("/voice/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "telemetry_mode" in data
    assert "is_llm_configured" in data


def test_cors_headers(client):
    """Verify CORS headers are present for localhost:3000."""
    res = client.options(
        "/voice/query",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_empty_query_rejected(client):
    """Verify empty or blank queries return 400 Bad Request."""
    res = client.post("/voice/query", json={"text": "   "})
    assert res.status_code == 400


def test_battery_query(client):
    """TEST 1: 'What is the battery level of AC001?'."""
    res = client.post("/voice/query", json={"text": "What is the battery level of AC001?"})
    assert res.status_code == 200
    data = res.json()
    assert data["query"] == "What is the battery level of AC001?"
    assert "battery" in data["response"].lower() or "percent" in data["response"].lower() or "online" in data["response"].lower()
    assert "audio_base64" in data


def test_altitude_query(client):
    """TEST 2: 'What is the altitude of AC001?'."""
    res = client.post("/voice/query", json={"text": "What is the altitude of AC001?"})
    assert res.status_code == 200
    data = res.json()
    assert data["query"] == "What is the altitude of AC001?"
    assert "altitude" in data["response"].lower() or "feet" in data["response"].lower() or "meters" in data["response"].lower()


def test_alerts_query(client):
    """TEST 3: 'Are there any alerts?'."""
    res = client.post("/voice/query", json={"text": "Are there any alerts?"})
    assert res.status_code == 200
    data = res.json()
    assert data["query"] == "Are there any alerts?"
    assert "alert" in data["response"].lower() or "nominal" in data["response"].lower()


def test_random_or_conversational_query(client):
    """TEST 4: Conversational/general query handling."""
    res = client.post("/voice/query", json={"text": "Hello AeroGuard, are you online?"})
    assert res.status_code == 200
    data = res.json()
    assert len(data["response"]) > 0


def test_backend_offline_telemetry_handling():
    """TEST 5: Verify clean degradation when ground station is offline."""
    svc = get_service()
    # Point client to dead port
    from voice_layer.aero_api import AeroGuardClient
    dead_client = AeroGuardClient(base_url="http://127.0.0.1:59999")
    from voice_layer.tools.provider import LiveTelemetryProvider
    dead_provider = LiveTelemetryProvider(dead_client)
    dead_registry = ToolRegistry(provider=dead_provider)
    dead_agent = AeroGuardAgent(
        api_client=dead_client,
        llm_client=None,
        tool_registry=dead_registry,
    )

    orig_agent = svc.agent
    svc.agent = dead_agent
    try:
        with TestClient(app) as dead_test_client:
            res = dead_test_client.post("/voice/query", json={"text": "What is the battery level of Node 1?"})
            assert res.status_code == 200
            data = res.json()
            assert "can't retrieve" in data["response"].lower() or "offline" in data["response"].lower()
    finally:
        svc.agent = orig_agent
        dead_client.close()


def test_session_lifecycle_endpoints(client):
    """Verify /voice/session/start, status, and stop endpoints."""
    # Start session
    res = client.post("/voice/session/start")
    assert res.status_code == 200
    assert res.json()["session_active"] is True
    assert res.json()["state"] == "LISTENING"

    # Status check
    res_status = client.get("/voice/session/status")
    assert res_status.status_code == 200
    assert res_status.json()["session_active"] is True

    # Stop session
    res_stop = client.post("/voice/session/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["session_active"] is False
    assert res_stop.json()["state"] == "OFFLINE"


def test_persistent_multi_turn_conversation(client):
    """Verify persistent voice session handles multiple queries without dropping offline."""
    client.post("/voice/session/start")

    # Turn 1
    r1 = client.post("/voice/query", json={"text": "What is the battery level of AC001?"})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["intent"] == "NORMAL_REQUEST"
    assert d1["session_active"] is True
    assert d1["should_exit"] is False

    # Turn 2
    r2 = client.post("/voice/query", json={"text": "What is the altitude of AC001?"})
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["intent"] == "NORMAL_REQUEST"
    assert d2["session_active"] is True
    assert d2["should_exit"] is False

    # Turn 3: Background chatter ignored
    r3 = client.post("/voice/query", json={"text": "Did you submit the assignment?"})
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["intent"] == "IGNORE"
    assert d3["response"] == ""
    assert d3["session_active"] is True

    # Turn 4: Thanks followed by question (must NOT terminate session)
    r4 = client.post("/voice/query", json={"text": "Thank you, now tell me the battery voltage"})
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["intent"] == "NORMAL_REQUEST"
    assert d4["session_active"] is True

    # Turn 5: Explicit farewell ends session
    r5 = client.post("/voice/query", json={"text": "That's all"})
    assert r5.status_code == 200
    d5 = r5.json()
    assert d5["intent"] == "END_SESSION"
    assert d5["should_exit"] is True
    assert d5["session_active"] is False


def test_task8_exact_sequence(client):
    """Verify exact requested debug sequence:
    1. 'what is machine networking' -> general LLM answer, session alive
    2. 'what is the battery status of Node 1' -> telemetry answer, session alive
    3. 'that's all' -> session ends, OFFLINE
    """
    client.post("/voice/session/start")

    # Turn 1: General question
    r1 = client.post("/voice/query", json={"text": "what is machine networking"})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["intent"] == "NORMAL_REQUEST"
    assert len(d1["response"]) > 0
    assert d1["session_active"] is True
    assert d1["should_exit"] is False

    # Turn 2: Telemetry question
    r2 = client.post("/voice/query", json={"text": "what is the battery status of Node 1"})
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["intent"] == "NORMAL_REQUEST"
    assert "battery" in d2["response"].lower() or "node 1" in d2["response"].lower() or "94" in d2["response"]
    assert d2["session_active"] is True
    assert d2["should_exit"] is False

    # Turn 3: End session
    r3 = client.post("/voice/query", json={"text": "that's all"})
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["intent"] == "END_SESSION"
    assert d3["should_exit"] is True
    assert d3["session_active"] is False
    assert d3["state"] == "OFFLINE"


