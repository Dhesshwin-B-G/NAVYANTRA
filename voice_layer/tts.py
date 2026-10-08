"""Cartesia Sonic Text-to-Speech (TTS) interface.

Handles voice synthesis requests to Cartesia Sonic, audio streaming,
and speaker playback via AudioManager.
"""

import logging
import threading
from typing import Optional

try:
    from cartesia import Cartesia
    from cartesia._exceptions import (
        APIConnectionError,
        AuthenticationError,
        CartesiaError,
        RateLimitError,
    )
except ImportError:
    Cartesia = None  # type: ignore
    APIConnectionError = Exception  # type: ignore
    AuthenticationError = Exception  # type: ignore
    CartesiaError = Exception  # type: ignore
    RateLimitError = Exception  # type: ignore

from .audio import AudioManager
from .config import VoiceConfig

logger = logging.getLogger("AeroGuard.TTS")


class TTSError(Exception):
    """Raised when a TTS operation fails."""
    pass


class CartesiaTTS:
    """Manages Cartesia Sonic Text-to-Speech synthesis and playback."""

    def __init__(self, config: VoiceConfig, audio_manager: Optional[AudioManager] = None) -> None:
        self.config = config
        self.audio = audio_manager or AudioManager()
        self.sample_rate = 24000
        self._client = None

        if not self.config.is_cartesia_configured:
            logger.warning("[TTS] Cartesia API key is not configured. Running in offline/mock mode.")
        else:
            self._init_client()

    def _init_client(self) -> None:
        """Initialize Cartesia client."""
        try:
            self._client = Cartesia(api_key=self.config.cartesia_api_key)
            logger.info("[TTS] Cartesia Sonic client initialized.")
        except Exception as e:
            logger.error(f"[TTS] Failed to initialize Cartesia TTS client: {e}")
            raise TTSError(f"Cartesia initialization failed: {e}") from e

    def synthesize_bytes(self, text: str) -> bytes:
        """Synthesize text into WAV audio bytes using Cartesia Sonic.

        Args:
            text: Text to speak.

        Returns:
            Complete WAV audio bytes.

        Raises:
            TTSError: If synthesis fails.
        """
        if not text or not text.strip():
            return b""

        if not self._client:
            raise TTSError("Cartesia client is not configured. Check CARTESIA_API_KEY.")

        model = self.config.cartesia_tts_model or "sonic-latest"
        voice_id = self.config.cartesia_voice_id
        logger.debug(f"[TTS] Synthesizing '{text[:40]}...' (model={model}, voice={voice_id})")

        output_format = {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": self.sample_rate,
        }

        try:
            # client.tts.bytes returns an Iterator[bytes]
            chunks = self._client.tts.bytes(
                model_id=model,
                transcript=text,
                voice=voice_id,
                output_format=output_format,  # type: ignore
                language=self.config.cartesia_language,  # type: ignore
                timeout=12.0,
            )
            audio_data = b"".join(chunks)
            logger.debug(f"[TTS] Received {len(audio_data)} audio bytes.")
            return audio_data

        except AuthenticationError as e:
            logger.error("[TTS] Authentication failed: invalid Cartesia API key.")
            raise TTSError("Cartesia API key is invalid or expired.") from e
        except APIConnectionError as e:
            logger.error(f"[TTS] Connection error to Cartesia: {e}")
            raise TTSError("Cannot connect to Cartesia TTS service.") from e
        except RateLimitError as e:
            logger.error("[TTS] Cartesia rate limit exceeded.")
            raise TTSError("Cartesia rate limit reached.") from e
        except CartesiaError as e:
            logger.error(f"[TTS] Cartesia API error: {e}")
            raise TTSError(f"Cartesia TTS failed: {e}") from e
        except Exception as e:
            logger.error(f"[TTS] Unexpected TTS error: {e}")
            raise TTSError(f"TTS synthesis failed: {e}") from e

    def speak(self, text: str, blocking: bool = True) -> None:
        """Synthesize and play the given text through the speaker.

        Args:
            text: Text to speak.
            blocking: If True, blocks until speech finishes. If False, runs in background thread.
        """
        clean_text = text.strip()
        if not clean_text:
            return

        logger.info(f"[TTS] Speaking: {clean_text}")

        def _play_task():
            try:
                wav_bytes = self.synthesize_bytes(clean_text)
                if wav_bytes:
                    self.audio.play_wav(wav_bytes)
            except TTSError as e:
                logger.warning(f"[TTS] Audio playback skipped due to TTS error: {e}")
            except Exception as e:
                logger.warning(f"[TTS] Audio playback failed: {e}")

        if blocking:
            _play_task()
        else:
            thread = threading.Thread(target=_play_task, daemon=True)
            thread.start()
