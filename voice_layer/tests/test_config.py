"""Unit tests for voice_layer configuration loader and validator."""

import os
import pytest
from voice_layer.config import VoiceConfig


def test_config_defaults(monkeypatch):
    """Test default values when no environment variables are provided."""
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_STT_MODEL", raising=False)
    monkeypatch.delenv("CARTESIA_TTS_MODEL", raising=False)
    monkeypatch.delenv("CARTESIA_VOICE_ID", raising=False)
    monkeypatch.delenv("AEROGUARD_API_BASE_URL", raising=False)

    config = VoiceConfig.from_env()
    assert config.cartesia_api_key is None
    assert config.cartesia_stt_model == "ink-2"
    assert config.cartesia_tts_model == "sonic-latest"
    assert config.aeroguard_api_base_url == "http://localhost:8080"
    assert not config.is_cartesia_configured


def test_config_validation_missing_key():
    """Test that validation raises error if Cartesia key is missing."""
    config = VoiceConfig(cartesia_api_key=None)
    with pytest.raises(ValueError, match="Missing CARTESIA_API_KEY"):
        config.validate(require_cartesia=True)

    # Should not raise if require_cartesia is False
    config.validate(require_cartesia=False)


def test_config_validation_placeholder_key():
    """Test that placeholder API keys are rejected."""
    config = VoiceConfig(cartesia_api_key="your_key_here")
    with pytest.raises(ValueError, match="placeholder value"):
        config.validate(require_cartesia=True)
    assert not config.is_cartesia_configured


def test_config_validation_valid():
    """Test valid configuration passes validation."""
    config = VoiceConfig(
        cartesia_api_key="test_live_key_12345",
        aeroguard_api_base_url="http://127.0.0.1:8080",
    )
    config.validate(require_cartesia=True)
    assert config.is_cartesia_configured


def test_config_invalid_api_url():
    """Test invalid AeroGuard API base URL."""
    config = VoiceConfig(
        cartesia_api_key="test_live_key_12345",
        aeroguard_api_base_url="ftp://invalid-url",
    )
    with pytest.raises(ValueError, match="Invalid AEROGUARD_API_BASE_URL"):
        config.validate(require_cartesia=False)
