"""AeroGuard Voice Layer - Main Application Entrypoint.

Starts the AeroGuard voice assistant supporting:
1. Push-to-talk mode (Press ENTER to talk)
2. Continuous real-time streaming mode (Cartesia auto-finalize WebSocket)
3. Direct text command mode (for automated testing or mic-free operation)
"""

import argparse
import logging
import sys
import time

from .aero_api import AeroGuardClient
from .agent import AeroGuardAgent
from .audio import AudioError, AudioManager
from .config import VoiceConfig
from .llm.client import create_llm_client
from .stt import CartesiaSTT, STTError
from .tools.provider import LiveTelemetryProvider, MockTelemetryProvider
from .tools.registry import ToolRegistry
from .tts import CartesiaTTS, TTSError


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structured logging for the voice layer."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_ptt_loop(
    config: VoiceConfig,
    audio: AudioManager,
    stt: CartesiaSTT,
    tts: CartesiaTTS,
    agent: AeroGuardAgent,
    record_duration: float = 3.5,
) -> None:
    """Run Push-To-Talk interactive voice loop.

    Flow:
        ENTER key -> record mic -> Cartesia STT -> agent -> Cartesia TTS -> speaker
    """
    print("\n" + "=" * 60)
    print("  AEROGUARD VOICE LAYER — PUSH-TO-TALK MODE")
    print("=" * 60)
    print("Instructions:")
    print("  - Press [ENTER] to record speech (default 3.5s)")
    print("  - Type any command and press [ENTER] to run text-only")
    print("  - Type 'quit' or say 'stop voice assistant' to exit")
    print("=" * 60 + "\n")

    while True:
        try:
            prompt_input = input("[VOICE] Press ENTER to speak (or type a command): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[VOICE] Terminating session.")
            break

        # Check if user typed a command instead of pressing enter
        if prompt_input:
            user_text = prompt_input
            print(f"[INPUT] {user_text}")
        else:
            print("[VOICE] Listening... (speak now)")
            try:
                pcm_data = audio.record_seconds(duration=record_duration)
            except AudioError as e:
                print(f"[ERROR] Microphone failure: {e}")
                continue

            print("[VOICE] Processing speech with Cartesia Ink...")
            try:
                user_text = stt.transcribe_pcm(pcm_data)
            except STTError as e:
                print(f"[ERROR] STT Error: {e}")
                tts.speak("I encountered an issue transcribing your speech.")
                continue

            if not user_text:
                print("[VOICE] No speech detected.")
                continue

            print(f"[STT] {user_text}")

        # Dispatch to agent
        response_text, should_exit = agent.process_query(user_text)
        print(f"[AGENT] {response_text}")

        # Synthesize with Cartesia Sonic and play
        try:
            tts.speak(response_text, blocking=True)
        except TTSError as e:
            print(f"[ERROR] TTS Error: {e}")

        if should_exit:
            print("[VOICE] Shutdown confirmed.")
            break


def run_realtime_loop(
    config: VoiceConfig,
    audio: AudioManager,
    stt: CartesiaSTT,
    tts: CartesiaTTS,
    agent: AeroGuardAgent,
) -> None:
    """Run continuous real-time streaming voice loop with native turn detection."""
    print("\n" + "=" * 60)
    print("  AEROGUARD VOICE LAYER — REALTIME STREAMING MODE")
    print("=" * 60)
    print("Listening continuously using Cartesia Ink auto-finalize turn detection.")
    print("Press Ctrl+C to terminate.\n")

    while True:
        try:
            print("[VOICE] Listening...")
            stream_gen = audio.record_stream(chunk_duration_ms=100)

            def on_partial(text: str):
                print(f"\r[STT partial] {text}", end="", flush=True)

            final_text = stt.stream_transcribe(stream_gen, on_partial=on_partial)
            print()

            if not final_text:
                continue

            print(f"[STT] {final_text}")
            response_text, should_exit = agent.process_query(final_text)
            print(f"[AGENT] {response_text}")

            tts.speak(response_text, blocking=True)

            if should_exit:
                break

        except KeyboardInterrupt:
            print("\n[VOICE] Realtime loop stopped by user.")
            break
        except Exception as e:
            print(f"[ERROR] Stream error: {e}")
            time.sleep(1.0)


def run_continuous_voice_loop(
    config: VoiceConfig,
    audio: AudioManager,
    stt: CartesiaSTT,
    tts: CartesiaTTS,
    agent: AeroGuardAgent,
) -> None:
    """Run continuous hands-free voice assistant loop.

    Flow:
        Microphone (VAD) -> Cartesia STT -> AGY / Groq -> AeroGuard Tools -> Ground Station /api/log
        -> Telemetry Result -> Groq LLM -> Response -> Cartesia Sonic TTS -> Speaker -> Listen Again
    """
    print("\n" + "=" * 65)
    print("  AEROGUARD VOICE LAYER — CONTINUOUS VOICE ASSISTANT")
    print("=" * 65)
    print("Voice pipeline initialized.")
    print("Speak naturally to AeroGuard (e.g., 'What is the battery level of AC001?')")
    print("Say 'quit', 'exit', 'shutdown', or press Ctrl+C to stop.")
    print("=" * 65 + "\n")

    listening_logged = False

    while True:
        try:
            if not listening_logged:
                print("[VOICE] Listening... (speak to AeroGuard)")
                listening_logged = True

            pcm_data = audio.record_speech(
                energy_threshold=320.0,
                silence_timeout=0.9,
                max_duration=8.0,
                poll_timeout=2.0,
            )

            if not pcm_data or len(pcm_data) < int(audio.default_sample_rate * 2 * 0.4):
                # No speech detected during poll window; loop quietly
                continue

            listening_logged = False
            print("[STT] Transcribing...")
            try:
                user_text = stt.transcribe_pcm(pcm_data)
            except STTError as e:
                print(f"[ERROR] STT failure: {e}")
                continue

            if not user_text:
                print("[VOICE] No discernible speech recognized.")
                continue

            print(f"[STT] User: {user_text}")

            # Check for quick exit keywords
            clean_input = user_text.lower().strip()
            if clean_input in ("quit", "exit", "shutdown", "stop voice assistant"):
                print("[VOICE] Shutdown confirmed. Radar offline.")
                tts.speak("Shutting down AeroGuard voice assistant. Radar offline.", blocking=True)
                break

            # Dispatch to AGY / LLM
            response_text, should_exit = agent.process_query(user_text)

            # Synthesize and speak
            print("[TTS] Generating speech...")
            print("[AUDIO] Playing response...")
            try:
                tts.speak(response_text, blocking=True)
            except TTSError as e:
                print(f"[ERROR] TTS Error: {e}")

            if should_exit:
                print("[VOICE] Shutdown confirmed. Radar offline.")
                break

        except KeyboardInterrupt:
            print("\n[VOICE] Voice loop terminated by user.")
            break
        except AudioError as e:
            print(f"[ERROR] Audio device failure: {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"[ERROR] Voice pipeline error: {e}")
            time.sleep(1.0)


def main() -> None:
    """Application entrypoint."""
    parser = argparse.ArgumentParser(description="AeroGuard Voice Layer")
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Start continuous hands-free voice assistant mode (default if no mode specified)",
    )
    parser.add_argument(
        "--mode",
        choices=["voice", "ptt", "realtime", "text"],
        default="voice",
        help="Operating mode: voice (hands-free continuous), ptt (Push-To-Talk), or realtime",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=3.5,
        help="Recording duration in seconds for PTT mode (default: 3.5s)",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Execute a single voice command query and exit (useful for automated testing)",
    )
    args = parser.parse_args()

    # 1. Load configuration
    config = VoiceConfig.from_env()
    setup_logging(config.voice_log_level)
    logger = logging.getLogger("AeroGuard.Main")

    logger.info("Starting AeroGuard Voice Layer...")
    logger.info(f"Target Ground Station: {config.aeroguard_api_base_url}")
    logger.info(f"STT Model: {config.cartesia_stt_model} | TTS Model: {config.cartesia_tts_model}")

    # Validate configuration
    if not config.is_cartesia_configured:
        logger.warning(
            "CARTESIA_API_KEY is not set or using placeholder. "
            "Microphone transcription and Sonic synthesis will be offline. "
            "Please configure voice_layer/.env for full live voice operation."
        )

    # 2. Initialize Audio Manager
    audio = AudioManager(tts_sample_rate=24000)

    # 3. Initialize STT and TTS
    stt = CartesiaSTT(config)
    tts = CartesiaTTS(config, audio_manager=audio)

    # 4. Initialize Telemetry Provider (Mock or Live)
    api_client = AeroGuardClient(base_url=config.aeroguard_api_base_url)
    if config.is_mock_telemetry:
        logger.info("[TELEMETRY] AeroGuard Data Mode: MOCK (deterministic simulation active, ground station offline/bypassed).")
        telemetry_provider = MockTelemetryProvider()
    else:
        logger.info(f"[TELEMETRY] AeroGuard Data Mode: LIVE (querying {config.aeroguard_api_base_url}/api/log).")
        telemetry_provider = LiveTelemetryProvider(api_client)

    # 5. Initialize Tool Registry and LLM Client
    tool_registry = ToolRegistry(provider=telemetry_provider)
    llm_client = create_llm_client(config)
    if llm_client:
        logger.info(f"[LLM] AGY LLM Engine ACTIVE (provider={config.llm_provider}, model={config.llm_model}).")
    else:
        logger.info("[LLM] AGY LLM Engine not configured. Deterministic rule-based fallback ACTIVE.")

    agent = AeroGuardAgent(
        api_client=api_client,
        llm_client=llm_client,
        tool_registry=tool_registry,
    )

    # 5. Handle single-query mode if passed
    if args.query:
        print(f"\n[QUERY] {args.query}")
        response, _ = agent.process_query(args.query)
        api_client.close()
        return

    # 6. Run selected operating mode
    try:
        if args.voice or args.mode == "voice":
            run_continuous_voice_loop(config, audio, stt, tts, agent)
        elif args.mode == "realtime":
            run_realtime_loop(config, audio, stt, tts, agent)
        else:
            run_ptt_loop(config, audio, stt, tts, agent, record_duration=args.duration)
    finally:
        api_client.close()


if __name__ == "__main__":
    main()
