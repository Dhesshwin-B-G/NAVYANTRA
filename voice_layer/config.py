"""Configuration loader and validator for the AeroGuard Voice Layer."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Search for .env in current directory and voice_layer directory
_BASE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BASE_DIR.parent

for env_path in [_BASE_DIR / ".env", _PROJECT_ROOT / ".env"]:
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)
        break
else:
    load_dotenv(override=False)


@dataclass
class VoiceConfig:
    """Holds configuration for AeroGuard Voice Layer."""

    cartesia_api_key: Optional[str]
    cartesia_stt_model: str = "ink-2"
    cartesia_tts_model: str = "sonic-latest"
    cartesia_voice_id: str = "79a125e8-cd45-4c13-8a67-188112f4dd22"
    cartesia_language: str = "en"
    aeroguard_api_base_url: str = "http://localhost:8080"
    voice_log_level: str = "INFO"

    # LLM configuration
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_api_key: Optional[str] = None
    llm_api_base_url: Optional[str] = None
    llm_temperature: float = 0.2

    # Telemetry data mode: 'mock' (simulated offline) or 'live' (backend /api/log)
    aeroguard_data_mode: str = "mock"

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        """Load configuration from environment variables."""
        api_key = os.getenv("CARTESIA_API_KEY", "").strip() or None
        stt_model = os.getenv("CARTESIA_STT_MODEL", "ink-2").strip() or "ink-2"
        tts_model = os.getenv("CARTESIA_TTS_MODEL", "sonic-latest").strip() or "sonic-latest"
        voice_id = os.getenv("CARTESIA_VOICE_ID", "79a125e8-cd45-4c13-8a67-188112f4dd22").strip()
        language = os.getenv("CARTESIA_LANGUAGE", "en").strip() or "en"
        api_url = os.getenv("AEROGUARD_API_BASE_URL", "http://localhost:8080").strip().rstrip("/")
        log_level = os.getenv("VOICE_LOG_LEVEL", "INFO").strip().upper() or "INFO"

        # LLM settings
        llm_provider = os.getenv("LLM_PROVIDER", "groq").strip().lower() or "groq"
        llm_model = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
        raw_llm_key = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        llm_api_key = raw_llm_key.strip() or None
        llm_api_base_url = os.getenv("LLM_API_BASE_URL", "").strip() or None

        try:
            llm_temp = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        except ValueError:
            llm_temp = 0.2

        data_mode = os.getenv("AEROGUARD_DATA_MODE", "mock").strip().lower()
        if data_mode not in ("mock", "live"):
            data_mode = "mock"

        return cls(
            cartesia_api_key=api_key,
            cartesia_stt_model=stt_model,
            cartesia_tts_model=tts_model,
            cartesia_voice_id=voice_id,
            cartesia_language=language,
            aeroguard_api_base_url=api_url,
            voice_log_level=log_level,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            llm_api_base_url=llm_api_base_url,
            llm_temperature=llm_temp,
            aeroguard_data_mode=data_mode,
        )

    def validate(self, require_cartesia: bool = True) -> None:
        """Validate configuration settings.

        Raises:
            ValueError: If required settings are missing or invalid.
        """
        if require_cartesia:
            if not self.cartesia_api_key:
                raise ValueError(
                    "Missing CARTESIA_API_KEY. Please provide CARTESIA_API_KEY in .env "
                    "or set the environment variable."
                )
            if self.cartesia_api_key.startswith("your_key_here"):
                raise ValueError(
                    "CARTESIA_API_KEY is using a placeholder value. Please set a valid Cartesia API key."
                )

        if not self.aeroguard_api_base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"Invalid AEROGUARD_API_BASE_URL: '{self.aeroguard_api_base_url}'. "
                "Must begin with http:// or https://"
            )

    @property
    def is_cartesia_configured(self) -> bool:
        """Return True if Cartesia API key is set and not a placeholder."""
        return bool(
            self.cartesia_api_key and not self.cartesia_api_key.startswith("your_key_here")
        )

    @property
    def is_llm_configured(self) -> bool:
        """Return True if LLM provider is active and configured."""
        if self.llm_provider in ("mock", "sim", "test"):
            return True
        return bool(
            self.llm_api_key
            and not self.llm_api_key.startswith("your_")
            and self.llm_provider != "none"
        )

    @property
    def is_mock_telemetry(self) -> bool:
        """Return True if AeroGuard data mode is mock."""
        return self.aeroguard_data_mode == "mock"

