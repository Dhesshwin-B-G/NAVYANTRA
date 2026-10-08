"""AeroGuard Voice Layer - Persistent Voice Session & Intent Classification Engine.

Implements the persistent voice-session state machine:
    OFFLINE -> LISTENING -> PROCESSING -> SPEAKING -> LISTENING (repeat)
                     └───────────────────► STOPPING -> OFFLINE

Provides:
- VoiceState: OFFLINE, LISTENING, PROCESSING, SPEAKING, STOPPING
- VoiceIntent: NORMAL_REQUEST, END_SESSION, IGNORE
- detect_voice_intent: High-precision intent classifier with background conversation filtering
- VoiceSessionManager: Thread-safe session lifecycle and state synchronization
"""

import enum
import logging
import re
import threading
from typing import Optional, Set, Tuple

logger = logging.getLogger("AeroGuard.Session")


class VoiceState(str, enum.Enum):
    """Voice session state machine states."""
    OFFLINE = "OFFLINE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    STOPPING = "STOPPING"


class VoiceIntent(str, enum.Enum):
    """Voice intent classification results."""
    NORMAL_REQUEST = "NORMAL_REQUEST"
    END_SESSION = "END_SESSION"
    IGNORE = "IGNORE"


# Low-information fillers and background noise words
NOISE_FILLERS: Set[str] = {
    "um", "uh", "ah", "err", "hmm", "hm", "huh", "mhm",
    "yep", "nope", "yeah", "oh", "cough", "throat", "shh",
    "like", "so", "well",
}

# Explicit assistant addressing wake keywords
ADDRESSING_KEYWORDS: Set[str] = {
    "aeroguard", "agy", "assistant", "jazs", "ground control", "computer"
}

# Domain keywords indicating operational interest in AeroGuard telemetry & system
DOMAIN_KEYWORDS: Set[str] = {
    # System / Ground Station
    "aeroguard", "system", "status", "health", "ground", "station", "mesh",
    "radar", "online", "operational",
    # Nodes & metrics
    "node", "nodes", "telemetry", "metric", "metrics", "packet", "packets",
    "battery", "voltage", "charge", "level", "percent", "percentage", "power",
    "altitude", "height", "feet", "meters",
    "temperature", "temp", "celsius", "degrees",
    "pressure", "barometer", "barometric", "hpa",
    "speed", "airspeed", "knots", "heading", "bearing", "pitch", "roll",
    # Aircraft & callsigns
    "aircraft", "plane", "flight", "target", "ag7423", "ac001", "relay",
    "relay01", "gs001",
    # Alerts & commands
    "alert", "alerts", "emergency", "emergencies", "warning", "warnings",
    "distress", "beacon", "help", "commands", "options",
    # Mesh networking & devices
    "network", "networking", "machine", "device", "connection", "link", "lora",
}

# Question starters & operational command verbs
QUESTION_VERBS: Set[str] = {
    "what", "whats", "what's", "how", "hows", "how's", "where", "why", "is", "are",
    "can", "could", "tell", "show", "give", "check", "report", "display",
    "get", "read", "verify", "who", "which", "explain", "describe", "define"
}

# End-session regex patterns (when utterance is primarily a closing/farewell intent)
END_SESSION_PATTERNS = [
    r"^(thank\s*you|thanks)\s*(so\s*much|a\s*lot)?$",
    r"^(thats\s*all|that\s*s\s*all|that\s*is\s*all)$",
    r"^(thats\s*it|that\s*s\s*it|that\s*is\s*it)$",
    r"^(thanks|thank\s*you)[,\s]+(thats\s*all|that\s*s\s*all|that\s*is\s*all|thats\s*it|that\s*is\s*it)$",
    r"^(thanks|thank\s*you)[,\s]+(goodbye|bye|bye\s*bye|have\s*a\s*good\s*one)$",
    r"^(you\s*can\s*stop|stop\s*listening|stop\s*now)$",
    r"^(end\s*session|end\s*the\s*session|close\s*session)$",
    r"^(goodbye|bye|bye\s*bye|see\s*you\s*later)$",
    r"^(you\s*can\s*go\s*offline|go\s*offline)$",
    r"^stop$",
    r"^(deactivate\s*microphone|turn\s*off\s*listening|turn\s*off\s*mic|mute\s*mic|mic\s*off)$",
    r"^(quit|exit|shutdown|power\s*down|stand\s*down)$",
    r"^(stop\s*voice\s*assistant|shutdown\s*voice\s*layer|turn\s*off\s*voice\s*assistant)$",
]

# Background conversation patterns (side talk between humans in the room)
BACKGROUND_CHATTER_PATTERNS = [
    r"\b(assignment|homework|exam|grade|grades|class|classes|school|college|semester)\b",
    r"\b(lunch|dinner|breakfast|food|snack|coffee|tea|restaurant|eat|eating)\b",
    r"\b(pass the (salt|pepper|plate|water|fork|spoon))\b",
    r"\b(on the phone|talking to my (friend|mom|dad|brother|sister|roommate))\b",
    r"\b(grab a coffee|grab some|let'?s go|we should leave|leave now|heading out)\b",
    r"\b(submit(ted)? the)\b",
    r"^(yeah|yep|nope|sure|ok|okay)[,\s]+(yesterday|today|sure|okay)?$",
    r"^(yesterday|tomorrow)$",
]

# Courtesy prefixes to strip if followed by a real request (e.g. "Thank you, what is the altitude?")
COURTESY_PREFIX_REGEX = re.compile(
    r"^(thank\s*you|thanks|okay|ok|please|hey|hello|hi)[,\s]+(now\s+|please\s+)?",
    re.IGNORECASE,
)


def detect_voice_intent(transcript: str, in_session: bool = True) -> Tuple[VoiceIntent, str]:
    """Classify transcribed speech into NORMAL_REQUEST, END_SESSION, or IGNORE.

    Filters out background conversation and random noise without requiring
    the wake word to be repeated for every utterance in an active session.

    Handles mixed utterances cleanly:
        "Thank you, now tell me the battery voltage" -> NORMAL_REQUEST
        "Thanks, that's all"                        -> END_SESSION
        "Did you submit the assignment?"             -> IGNORE
        "what is machine networking"                 -> NORMAL_REQUEST

    Args:
        transcript: Transcribed speech text.
        in_session: Whether an active persistent voice session is currently open.

    Returns:
        Tuple of (VoiceIntent, cleaned_text_ready_for_processing).
    """
    clean = transcript.strip()
    if not clean or len(clean) < 2:
        return VoiceIntent.IGNORE, ""

    # Normalize punctuation and casing for analysis
    # Strip apostrophes to handle contractions cleanly (e.g. "that's" -> "thats")
    no_apostrophe = re.sub(r"['’`]", "", clean.lower())
    normalized = re.sub(r"[^\w\s]", " ", no_apostrophe).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    tokens = [tok for tok in normalized.split() if tok]

    if not tokens:
        return VoiceIntent.IGNORE, ""

    # 1. Ignore pure noise / non-verbal fillers
    if all(tok in NOISE_FILLERS for tok in tokens):
        logger.debug(f"[INTENT] Ignored noise/filler: '{clean}'")
        return VoiceIntent.IGNORE, ""

    # Check for direct assistant addressing
    has_addressing = any(tok in ADDRESSING_KEYWORDS for tok in tokens)

    # Check for domain keywords & question verbs
    has_domain_keyword = any(tok in DOMAIN_KEYWORDS for tok in tokens)
    has_question_verb = any(tok in QUESTION_VERBS for tok in tokens)

    # Check for conversational greeting/help commands
    is_greeting_or_help = bool(re.search(
        r"\b(hello|hi|hey|good\s*(morning|afternoon|evening)|help|what\s*can\s*you\s*do|who\s*are\s*you)\b",
        normalized,
    ))

    # 2. Check for courtesy prefix followed by an operational request
    # e.g. "Thank you, now tell me the battery voltage" or "Thanks, what is the altitude?"
    if COURTESY_PREFIX_REGEX.match(clean) and (has_domain_keyword or has_question_verb) and len(tokens) > 2:
        cleaned_query = COURTESY_PREFIX_REGEX.sub("", clean).strip()
        if cleaned_query:
            logger.info(f"[INTENT] Utterance contains courtesy prefix + operational request -> NORMAL_REQUEST ('{cleaned_query}')")
            return VoiceIntent.NORMAL_REQUEST, cleaned_query

    # 3. Check for End-Session intent
    matches_end_pattern = any(re.search(pat, normalized) for pat in END_SESSION_PATTERNS)
    has_closing_words = any(w in tokens for w in ["bye", "goodbye", "quit", "exit", "shutdown", "offline", "stop"])

    if matches_end_pattern or (has_closing_words and not has_domain_keyword):
        logger.info(f"[INTENT] Utterance matched closing intent -> END_SESSION ('{clean}')")
        return VoiceIntent.END_SESSION, clean

    # 4. If addressed to assistant explicitly, always process
    if has_addressing:
        return VoiceIntent.NORMAL_REQUEST, clean

    # 5. Check for background conversation chatter
    is_bg_chatter = any(re.search(pat, normalized) for pat in BACKGROUND_CHATTER_PATTERNS)
    if is_bg_chatter:
        logger.info(f"[INTENT] Utterance filtered out as background/unrelated speech -> IGNORE ('{clean}')")
        return VoiceIntent.IGNORE, ""

    # 6. If domain keywords or greeting/help are present, process as normal request
    if has_domain_keyword or is_greeting_or_help:
        return VoiceIntent.NORMAL_REQUEST, clean

    # 7. In active session: general user questions or commands to assistant
    if in_session and (has_question_verb or len(tokens) >= 3):
        return VoiceIntent.NORMAL_REQUEST, clean

    # 8. Filter low-information utterances
    logger.info(f"[INTENT] Utterance filtered out as background/unrelated speech -> IGNORE ('{clean}')")
    return VoiceIntent.IGNORE, ""


class VoiceSessionManager:
    """Thread-safe manager for persistent voice sessions."""

    def __init__(self) -> None:
        self._state = VoiceState.OFFLINE
        self._lock = threading.Lock()
        self._is_active = False

    def is_active(self) -> bool:
        """Return True if voice session is actively open."""
        with self._lock:
            return self._is_active

    def get_state(self) -> VoiceState:
        """Get current voice state."""
        with self._lock:
            return self._state

    def start_session(self) -> VoiceState:
        """Start a persistent voice session."""
        with self._lock:
            self._is_active = True
            self._state = VoiceState.LISTENING
            print("[VOICE] Session started")
            print("[VOICE] State: LISTENING")
            logger.info("[VOICE] Session started")
            logger.info("[VOICE] State: LISTENING")
            return self._state

    def stop_session(self) -> VoiceState:
        """Terminate the active voice session."""
        with self._lock:
            self._is_active = False
            self._state = VoiceState.OFFLINE
            print("[VOICE] Session stopped")
            print("[VOICE] State: OFFLINE")
            logger.info("[VOICE] Session stopped")
            logger.info("[VOICE] State: OFFLINE")
            return self._state

    def transition_to(self, new_state: VoiceState) -> None:
        """Transition to a new state and log."""
        with self._lock:
            self._state = new_state
            print(f"[VOICE] State: {new_state.value}")
            logger.info(f"[VOICE] State: {new_state.value}")


# Default singleton instance
_GLOBAL_SESSION_MANAGER = VoiceSessionManager()


def get_session_manager() -> VoiceSessionManager:
    """Get the global VoiceSessionManager instance."""
    return _GLOBAL_SESSION_MANAGER


def start_voice_session() -> VoiceState:
    """Start global persistent voice session."""
    return _GLOBAL_SESSION_MANAGER.start_session()


def stop_voice_session() -> VoiceState:
    """Stop global persistent voice session."""
    return _GLOBAL_SESSION_MANAGER.stop_session()


def is_voice_session_active() -> bool:
    """Check if global persistent voice session is active."""
    return _GLOBAL_SESSION_MANAGER.is_active()
