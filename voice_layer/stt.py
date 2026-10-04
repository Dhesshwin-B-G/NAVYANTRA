"""Cartesia Ink Speech-to-Text (STT) interface.

Supports both push-to-talk buffer transcription and real-time streaming WebSocket
with Cartesia's auto-finalize native turn detection.
"""

import io
import logging
import time
import wave
from typing import Callable, Generator, Optional

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

from .config import VoiceConfig

logger = logging.getLogger("AeroGuard.STT")


class STTError(Exception):
    """Raised when an STT operation fails."""
    pass


class CartesiaSTT:
    """Manages Cartesia Ink Speech-to-Text operations."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self.sample_rate = 16000
        self.encoding = "pcm_s16le"
        self._client: Optional[Cartesia] = None

        if not self.config.is_cartesia_configured:
            logger.warning("[STT] Cartesia API key is not configured. Running in offline/mock mode.")
        else:
            self._init_client()

    def _init_client(self) -> None:
        """Initialize Cartesia client with validated API key."""
        try:
            self._client = Cartesia(api_key=self.config.cartesia_api_key)
            logger.info("[STT] Cartesia Ink client initialized.")
        except Exception as e:
            logger.error(f"[STT] Failed to initialize Cartesia client: {e}")
            raise STTError(f"Cartesia initialization failed: {e}") from e

    def _pcm_to_wav(self, pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
        """Convert raw PCM s16le bytes to a valid WAV file in memory."""
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return wav_io.getvalue()

    def transcribe_pcm(
        self,
        pcm_bytes: bytes,
        sample_rate: Optional[int] = None,
    ) -> str:
        """Transcribe raw PCM 16-bit LE mono audio bytes using Cartesia Ink.

        Args:
            pcm_bytes: Raw PCM audio recorded from the microphone.
            sample_rate: Sampling rate (defaults to 16000).

        Returns:
            The transcribed text.
        """
        if not pcm_bytes or len(pcm_bytes) < 1000:
            logger.debug("[STT] Audio chunk is empty or too short.")
            return ""

        if not self._client:
            raise STTError("Cartesia client is not configured. Check CARTESIA_API_KEY.")

        sr = sample_rate or self.sample_rate
        wav_data = self._pcm_to_wav(pcm_bytes, sr)
        audio_file = ("audio.wav", wav_data, "audio/wav")

        model = self.config.cartesia_stt_model
        # If model is configured as ink-2, map to batch model for file transcription if needed
        batch_model = "ink-whisper" if model in ("ink-2", "ink-whisper") else model

        logger.debug(f"[STT] Sending {len(wav_data)} bytes to Cartesia model '{batch_model}'...")
        try:
            response = self._client.stt.transcribe(
                file=audio_file,
                model=batch_model,
                language=self.config.cartesia_language,
                timeout=10.0,
            )
            transcript = getattr(response, "text", "") or ""
            transcript = transcript.strip()
            if transcript:
                logger.info(f"[STT] Transcript: {transcript}")
            else:
                logger.debug("[STT] Received empty transcript.")
            return transcript

        except AuthenticationError as e:
            logger.error("[STT] Authentication failed: invalid Cartesia API key.")
            raise STTError("Cartesia API key is invalid or expired.") from e
        except APIConnectionError as e:
            logger.error(f"[STT] Connection error to Cartesia: {e}")
            raise STTError("Cannot connect to Cartesia STT service. Check your internet connection.") from e
        except RateLimitError as e:
            logger.error("[STT] Cartesia rate limit exceeded.")
            raise STTError("Cartesia rate limit reached. Please wait a moment.") from e
        except CartesiaError as e:
            logger.error(f"[STT] Cartesia API error: {e}")
            raise STTError(f"Cartesia STT failed: {e}") from e
        except Exception as e:
            logger.error(f"[STT] Unexpected STT error: {e}")
            raise STTError(f"STT processing failed: {e}") from e

    def stream_transcribe(
        self,
        audio_chunks: Generator[bytes, None, None],
        on_partial: Optional[Callable[[str], None]] = None,
        max_silence_s: float = 2.0,
    ) -> str:
        """Stream PCM audio chunks to Cartesia auto-finalize WebSocket.

        Args:
            audio_chunks: Generator yielding raw PCM 16-bit LE 16kHz chunks.
            on_partial: Optional callback invoked when partial transcript is updated.
            max_silence_s: Max seconds of silence to wait for a turn end.

        Returns:
            Final recognized transcript.
        """
        if not self._client:
            raise STTError("Cartesia client is not configured. Check CARTESIA_API_KEY.")

        model = self.config.cartesia_stt_model
        if model not in ("ink-2",):
            model = "ink-2"

        final_transcript = ""
        try:
            logger.debug(f"[STT] Opening auto-finalize WebSocket (model={model}, sr={self.sample_rate})...")
            with self._client.stt.auto_finalize.websocket(
                encoding=self.encoding,  # type: ignore
                model=model,  # type: ignore
                sample_rate=self.sample_rate,
            ) as ws:
                logger.info("[STT] Realtime WebSocket connected. Streaming audio...")

                for chunk in audio_chunks:
                    ws.send_raw(chunk)
                    try:
                        # Attempt non-blocking or short-timeout event receive
                        event = ws.recv()
                        ev_type = getattr(event, "type", "")
                        text = getattr(event, "transcript", "")

                        if ev_type == "turn.update" and text:
                            logger.debug(f"[STT] Partial: {text}")
                            if on_partial:
                                on_partial(text)

                        elif ev_type == "turn.end" and text:
                            logger.info(f"[STT] Turn ended: {text}")
                            final_transcript = text
                            break
                    except Exception:
                        pass

                return final_transcript

        except AuthenticationError as e:
            logger.error("[STT] WebSocket Authentication failed.")
            raise STTError("Invalid Cartesia API key.") from e
        except APIConnectionError as e:
            logger.error(f"[STT] WebSocket connection failed: {e}")
            raise STTError("Could not connect to Cartesia realtime STT.") from e
        except Exception as e:
            logger.error(f"[STT] Realtime streaming error: {e}")
            raise STTError(f"Streaming STT error: {e}") from e
