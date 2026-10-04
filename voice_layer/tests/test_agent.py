"""Unit tests for AeroGuard Voice Agent, Command Routing, and Intent Detection."""

from unittest.mock import MagicMock
import pytest

from voice_layer.aero_api import AeroGuardAPIError, AeroGuardClient, NodeTelemetry
from voice_layer.agent import (
    AeroGuardAgent,
    Intent,
    _extract_aircraft_id,
    _extract_node_id,
)


@pytest.fixture
def mock_api_client():
    """Create a mock AeroGuardClient with realistic telemetry."""
    client = MagicMock(spec=AeroGuardClient)

    client.get_system_status.return_value = {
        "status": "ONLINE",
        "ground_station_url": "http://localhost:8080",
        "total_packets": 142,
        "telemetry_packets": 120,
        "emergency_packets": 0,
        "active_nodes_count": 3,
        "active_nodes": ["1", "2", "3"],
    }

    def _mock_get_node_status(node_id):
        if str(node_id) in ("1", "2", "3"):
            return {
                "node_id": str(node_id),
                "matched_src": f"NODE_{node_id}",
                "status": "ONLINE",
                "last_seen": "12:00:00",
                "last_packet_type": "TELEMETRY",
                "battery_pct": 87.0 if str(node_id) == "2" else 95.0,
            }
        return {
            "node_id": str(node_id),
            "status": "OFFLINE",
            "last_seen": None,
            "battery_pct": None,
        }

    def _mock_get_node_telemetry(node_id):
        if str(node_id) == "1":
            return NodeTelemetry(
                node_id="1",
                packet_type="TELEMETRY",
                timestamp="12:00:01",
                online=True,
                temperature_c=31.2,
                pressure_hpa=1012.5,
                altitude_m=125.0,
                altitude_ft=410.0,
                speed_knots=45.0,
                heading_deg=180.0,
                battery_pct=95.0,
            )
        elif str(node_id) == "2":
            return NodeTelemetry(
                node_id="2",
                packet_type="TELEMETRY",
                timestamp="12:00:02",
                online=True,
                temperature_c=28.5,
                pressure_hpa=1014.0,
                altitude_m=250.0,
                altitude_ft=820.0,
                speed_knots=55.0,
                heading_deg=90.0,
                battery_pct=87.0,
            )
        return None

    def _mock_get_aircraft_status(ac_id):
        if ac_id in ("AG7423", "3"):
            return NodeTelemetry(
                node_id="AG7423",
                packet_type="TELEMETRY",
                timestamp="12:00:05",
                online=True,
                altitude_ft=35000.0,
                speed_knots=480.0,
                heading_deg=270.0,
                fuel_kg=14500.0,
                battery_pct=99.0,
            )
        return None

    client.get_node_status.side_effect = _mock_get_node_status
    client.get_node_telemetry.side_effect = _mock_get_node_telemetry
    client.get_aircraft_status.side_effect = _mock_get_aircraft_status
    client.get_emergency_status.return_value = {
        "has_emergency": False,
        "count": 0,
        "latest_type": None,
        "latest_source": None,
        "latest_message": None,
    }

    return client


def test_extract_node_id():
    """Test extracting node numbers from speech utterances."""
    assert _extract_node_id("Status of Node 1") == "1"
    assert _extract_node_id("How is Node 2?") == "2"
    assert _extract_node_id("Check node 3") == "3"
    assert _extract_node_id("node two status") == "2"
    assert _extract_node_id("node-1") == "1"
    assert _extract_node_id("system status") is None


def test_extract_aircraft_id():
    """Test extracting aircraft callsign or ID."""
    assert _extract_aircraft_id("Show me aircraft 3 status") == "3"
    assert _extract_aircraft_id("Check AG7423") == "AG7423"
    assert _extract_aircraft_id("Where is AG 7423") == "AG7423"


def test_greeting_intent(mock_api_client):
    """Test greeting command."""
    agent = AeroGuardAgent(mock_api_client)
    res, should_exit = agent.process_query("Hello AeroGuard")
    assert not should_exit
    assert "AeroGuard online" in res


def test_system_status_intent(mock_api_client):
    """Test system status intent handling."""
    agent = AeroGuardAgent(mock_api_client)

    for query in ["System status", "How is AeroGuard?", "Is the system online?"]:
        res, should_exit = agent.process_query(query)
        assert not should_exit
        assert "AeroGuard system is fully operational" in res
        assert "3 mesh nodes active" in res


def test_node_status_intent(mock_api_client):
    """Test node status for Node 1, 2, and 3."""
    agent = AeroGuardAgent(mock_api_client)

    res, _ = agent.process_query("Status of Node 2")
    assert "Node 2 is online" in res
    assert "87 percent" in res

    res, _ = agent.process_query("Is Node 1 online?")
    assert "Node 1 is online" in res

    res, _ = agent.process_query("Status of Node 9")
    assert "Node 9 is currently offline" in res


def test_node_telemetry_intent(mock_api_client):
    """Test querying specific sensor metrics."""
    agent = AeroGuardAgent(mock_api_client)

    # Temperature
    res, _ = agent.process_query("What is Node 1 temperature?")
    assert "Node 1 temperature is 31.2 degrees Celsius" in res

    # Altitude
    res, _ = agent.process_query("What is the altitude of Node 2?")
    assert "altitude is 250.0 meters" in res

    # Battery
    res, _ = agent.process_query("What is Node 2 battery level?")
    assert "battery level is 87.0 percent" in res

    # Pressure
    res, _ = agent.process_query("What is the pressure on Node 1?")
    assert "barometric pressure is 1012.5 hectopascals" in res


def test_aircraft_status_intent(mock_api_client):
    """Test aircraft status query."""
    agent = AeroGuardAgent(mock_api_client)

    res, _ = agent.process_query("Show me aircraft AG7423 status")
    assert "Aircraft AG7423 is tracked" in res
    assert "35000 feet" in res


def test_alerts_intent(mock_api_client):
    """Test nominal and emergency alert handling."""
    agent = AeroGuardAgent(mock_api_client)

    res, _ = agent.process_query("Are there any alerts?")
    assert "There are no active emergency alerts" in res

    # Simulate emergency
    mock_api_client.get_emergency_status.return_value = {
        "has_emergency": True,
        "count": 1,
        "latest_type": "RAPID_DESCENT",
        "latest_source": "AG7423",
        "latest_message": "Vertical speed -2400 ft/min",
    }
    res, _ = agent.process_query("Any emergencies?")
    assert "Alert: RAPID_DESCENT" in res
    assert "AG7423" in res


def test_help_intent(mock_api_client):
    """Test help command."""
    agent = AeroGuardAgent(mock_api_client)
    res, should_exit = agent.process_query("What can you do?")
    assert not should_exit
    assert "system status" in res
    assert "node status" in res


def test_shutdown_intent(mock_api_client):
    """Test shutdown command."""
    agent = AeroGuardAgent(mock_api_client)
    res, should_exit = agent.process_query("Stop voice assistant")
    assert should_exit
    assert "Shutting down AeroGuard voice assistant" in res


def test_api_error_graceful_handling(mock_api_client):
    """Test that agent fails gracefully if Ground Station backend is unreachable."""
    mock_api_client.get_system_status.side_effect = AeroGuardAPIError("Connection refused")
    mock_api_client.get_node_status.side_effect = AeroGuardAPIError("Connection refused")

    agent = AeroGuardAgent(mock_api_client)

    res, _ = agent.process_query("System status")
    assert "I can't reach the AeroGuard ground station" in res

    res, _ = agent.process_query("Status of Node 2")
    assert "I can't retrieve Node 2 status right now" in res


def test_unknown_command(mock_api_client):
    """Test unrecognized command response."""
    agent = AeroGuardAgent(mock_api_client)
    res, should_exit = agent.process_query("Play some jazz music")
    assert not should_exit
    assert "Command not recognized" in res
