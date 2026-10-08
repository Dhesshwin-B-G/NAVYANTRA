"""AeroGuard Voice Layer package.

Independent voice assistant layer for the AeroGuard telemetry and ground station system.
"""

from .session import (
    VoiceIntent,
    VoiceSessionManager,
    VoiceState,
    detect_voice_intent,
    is_voice_session_active,
    start_voice_session,
    stop_voice_session,
)

__version__ = "1.0.0"
__all__ = [
    "VoiceState",
    "VoiceIntent",
    "VoiceSessionManager",
    "detect_voice_intent",
    "start_voice_session",
    "stop_voice_session",
    "is_voice_session_active",
]
