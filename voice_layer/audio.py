"""Audio hardware interface for microphone recording and speaker playback.

Keeps audio hardware logic strictly decoupled from STT and TTS logic.
"""

import io
import logging
import queue
import time
from typing import Generator, Optional, Tuple
import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger("AeroGuard.Audio")


class AudioError(Exception):
    """Raised when an audio hardware error occurs."""
    pass


class AudioManager:
    """Manages audio capture (microphone) and playback (speaker)."""

    def __init__(
        self,
        default_sample_rate: int = 16000,
        tts_sample_rate: int = 24000,
        channels: int = 1,
    ) -> None:
        self.default_sample_rate = default_sample_rate
        self.tts_sample_rate = tts_sample_rate
        self.channels = channels
        self._check_devices()

    def _check_devices(self) -> Tuple[bool, bool]:
        """Verify that at least one input and output device exists."""
        has_input = False
        has_output = False
        try:
            devices = sd.query_devices()
            for d in devices:
                if d.get("max_input_channels", 0) > 0:
                    has_input = True
                if d.get("max_output_channels", 0) > 0:
                    has_output = True
        except Exception as e:
            logger.warning(f"[AUDIO] Could not query audio devices: {e}")
        return has_input, has_output

    def record_seconds(
        self,
        duration: float = 3.5,
        sample_rate: Optional[int] = None,
    ) -> bytes:
        """Record audio from the default microphone for a given duration.

        Args:
            duration: Duration in seconds to record.
            sample_rate: Sampling rate (defaults to 16000 Hz).

        Returns:
            Raw PCM 16-bit LE mono audio bytes.

        Raises:
            AudioError: If microphone capture fails.
        """
        sr = sample_rate or self.default_sample_rate
        num_frames = int(duration * sr)
        try:
            logger.debug(f"[AUDIO] Recording {duration:.1f}s at {sr}Hz...")
            recording = sd.rec(
                frames=num_frames,
                samplerate=sr,
                channels=self.channels,
                dtype="int16",
                blocking=True,
            )
            return recording.tobytes()
        except Exception as e:
            logger.error(f"[AUDIO] Microphone recording failed: {e}")
            raise AudioError(f"Microphone capture error: {e}") from e

    def record_speech(
        self,
        energy_threshold: float = 350.0,
        silence_timeout: float = 0.9,
        max_duration: float = 8.0,
        sample_rate: Optional[int] = None,
        poll_timeout: float = 2.0,
    ) -> bytes:
        """Continuously listen and record an utterance when speech is detected.

        Uses energy-based voice activity detection (VAD).
        - Waits until audio energy exceeds energy_threshold.
        - Records audio frames while speech continues.
        - Concludes recording when silence persists for silence_timeout seconds.
        - If no speech is detected within poll_timeout seconds, returns empty bytes without busy-waiting.

        Returns:
            Raw PCM 16-bit LE mono audio bytes (16kHz), or b"" if no speech.
        """
        sr = sample_rate or self.default_sample_rate
        chunk_ms = 50
        block_size = int(sr * (chunk_ms / 1000.0))
        audio_q: queue.Queue[bytes] = queue.Queue()

        def _callback(indata, frames, time_info, status):
            audio_q.put(indata.tobytes())

        try:
            with sd.InputStream(
                samplerate=sr,
                channels=self.channels,
                dtype="int16",
                blocksize=block_size,
                callback=_callback,
            ):
                is_speaking = False
                frames = []
                start_poll = time.time()
                speech_start = 0.0
                last_voice_time = 0.0

                while True:
                    try:
                        chunk = audio_q.get(timeout=0.1)
                    except queue.Empty:
                        if not is_speaking and (time.time() - start_poll) >= poll_timeout:
                            return b""
                        continue

                    # Calculate RMS energy of chunk
                    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0

                    now = time.time()
                    if not is_speaking:
                        if rms >= energy_threshold:
                            is_speaking = True
                            speech_start = now
                            last_voice_time = now
                            frames.append(chunk)
                        elif (now - start_poll) >= poll_timeout:
                            return b""
                    else:
                        frames.append(chunk)
                        if rms >= energy_threshold * 0.8:
                            last_voice_time = now

                        # Silence detected after speech
                        if (now - last_voice_time) >= silence_timeout:
                            break

                        # Max duration cap
                        if (now - speech_start) >= max_duration:
                            break

                if frames:
                    pcm = b"".join(frames)
                    # Minimum valid utterance duration is 0.4s (sr * 2 * 0.4 bytes)
                    if len(pcm) >= int(sr * 2 * 0.4):
                        return pcm
                return b""

        except Exception as e:
            logger.error(f"[AUDIO] Speech recording failed: {e}")
            raise AudioError(f"Microphone speech recording failed: {e}") from e

    def record_stream(
        self,
        sample_rate: Optional[int] = None,
        chunk_duration_ms: int = 100,
    ) -> Generator[bytes, None, None]:
        """Stream microphone PCM chunks for real-time WebSocket processing.

        Yields:
            Raw PCM 16-bit LE mono audio byte chunks (e.g. 100ms each).
        """
        sr = sample_rate or self.default_sample_rate
        block_size = int(sr * (chunk_duration_ms / 1000.0))
        audio_q: queue.Queue[bytes] = queue.Queue()

        def _callback(indata, frames, time_info, status):
            if status:
                logger.debug(f"[AUDIO] Input overflow/status: {status}")
            audio_q.put(indata.tobytes())

        try:
            with sd.InputStream(
                samplerate=sr,
                channels=self.channels,
                dtype="int16",
                blocksize=block_size,
                callback=_callback,
            ):
                while True:
                    try:
                        chunk = audio_q.get(timeout=1.0)
                        yield chunk
                    except queue.Empty:
                        continue
        except Exception as e:
            logger.error(f"[AUDIO] Microphone stream error: {e}")
            raise AudioError(f"Microphone streaming failed: {e}") from e

    def play_pcm(
        self,
        pcm_bytes: bytes,
        sample_rate: Optional[int] = None,
        dtype: str = "int16",
    ) -> None:
        """Play raw PCM audio through the speaker.

        Args:
            pcm_bytes: Raw PCM audio bytes.
            sample_rate: Audio sample rate in Hz.
            dtype: Numpy data type (e.g. 'int16' or 'float32').
        """
        if not pcm_bytes:
            return
        sr = sample_rate or self.tts_sample_rate
        try:
            audio_array = np.frombuffer(pcm_bytes, dtype=dtype)
            sd.play(audio_array, samplerate=sr, blocking=True)
        except Exception as e:
            logger.error(f"[AUDIO] Speaker playback error: {e}")
            raise AudioError(f"Speaker playback failed: {e}") from e

    def play_wav(self, wav_bytes: bytes) -> None:
        """Play WAV audio through the speaker.

        Args:
            wav_bytes: Complete WAV file bytes (including header).
        """
        if not wav_bytes:
            return
        try:
            with io.BytesIO(wav_bytes) as bio:
                data, sr = sf.read(bio, dtype="float32")
                sd.play(data, samplerate=sr, blocking=True)
        except Exception as e:
            logger.error(f"[AUDIO] WAV playback error: {e}")
            raise AudioError(f"WAV playback failed: {e}") from e
