"""Unit tests for VoiceSessionManager and detect_voice_intent in voice_layer.session."""

import pytest
from voice_layer.session import (
    VoiceIntent,
    VoiceSessionManager,
    VoiceState,
    detect_voice_intent,
)


# ─────────────────────────────────────────────────────────────────────────────
# INTENT DETECTION TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_normal_telemetry_requests():
    """Verify operational domain requests are classified as NORMAL_REQUEST."""
    queries = [
        "What is the battery percentage?",
        "What is the altitude of AC001?",
        "What is Node 1 status?",
        "What is Node 2 temperature?",
        "Are there any alerts?",
        "How is AeroGuard doing?",
        "What can you do?",
        "Help",
        "Hello AeroGuard, are you online?",
        "JAZS, what's the battery percentage?",
        "Is Node 3 online?",
        "Show me aircraft AG7423 status",
    ]
    for q in queries:
        intent, processed = detect_voice_intent(q, in_session=True)
        assert intent == VoiceIntent.NORMAL_REQUEST, f"Expected NORMAL_REQUEST for '{q}', got {intent}"


def test_mixed_courtesy_and_requests():
    """Verify 'Thank you' followed by an operational question is NOT ended, but classified as NORMAL_REQUEST."""
    test_cases = [
        ("Thank you, now tell me the battery voltage", "tell me the battery voltage"),
        ("Thank you, what is the altitude?", "what is the altitude?"),
        ("Thanks, what is the battery voltage?", "what is the battery voltage?"),
        ("Okay, now tell me the battery voltage", "tell me the battery voltage"),
        ("Thanks, tell me Node 1 status", "tell me Node 1 status"),
        ("Thank you, are there any emergencies?", "are there any emergencies?"),
    ]
    for raw, expected_clause in test_cases:
        intent, processed = detect_voice_intent(raw, in_session=True)
        assert intent == VoiceIntent.NORMAL_REQUEST, f"Expected NORMAL_REQUEST for '{raw}', got {intent}"
        assert expected_clause.lower() in processed.lower(), f"Expected '{expected_clause}' in processed '{processed}'"


def test_explicit_end_session_intents():
    """Verify pure farewells and closing phrases are classified as END_SESSION."""
    closings = [
        "Thank you",
        "Thanks",
        "That's all",
        "That is all",
        "That's it",
        "That is it",
        "Thanks, that's all",
        "Thank you, goodbye",
        "You can stop",
        "Stop listening",
        "End session",
        "End the session",
        "Goodbye",
        "Bye",
        "Bye bye",
        "You can go offline",
        "Stop",
        "Deactivate microphone",
        "Turn off listening",
        "Quit",
        "Exit",
        "Shutdown",
        "Power down",
    ]
    for c in closings:
        intent, _ = detect_voice_intent(c, in_session=True)
        assert intent == VoiceIntent.END_SESSION, f"Expected END_SESSION for '{c}', got {intent}"


def test_background_conversations_ignored():
    """Verify unrelated side conversations are filtered out as IGNORE."""
    background_chatter = [
        "Did you submit the assignment?",
        "Yeah, yesterday.",
        "What are you having for lunch?",
        "Let's grab a coffee later.",
        "Can you pass the salt please?",
        "I was talking to my friend on the phone.",
        "We should leave now.",
    ]
    for chatter in background_chatter:
        intent, _ = detect_voice_intent(chatter, in_session=True)
        assert intent == VoiceIntent.IGNORE, f"Expected IGNORE for background chatter '{chatter}', got {intent}"


def test_empty_and_noise_fillers_ignored():
    """Verify empty strings and lone fillers are filtered out as IGNORE."""
    noises = [
        "",
        "   ",
        "um",
        "uh",
        "ah",
        "err",
        "hmm",
        "mhm",
        "cough",
    ]
    for n in noises:
        intent, _ = detect_voice_intent(n, in_session=True)
        assert intent == VoiceIntent.IGNORE, f"Expected IGNORE for filler/noise '{n}', got {intent}"


# ─────────────────────────────────────────────────────────────────────────────
# SESSION MANAGER TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_session_state_machine_lifecycle():
    """Verify complete state transitions from OFFLINE -> LISTENING -> PROCESSING -> SPEAKING -> LISTENING -> STOPPING -> OFFLINE."""
    mgr = VoiceSessionManager()

    # Initial state
    assert mgr.get_state() == VoiceState.OFFLINE
    assert not mgr.is_active()

    # Start session
    state = mgr.start_session()
    assert state == VoiceState.LISTENING
    assert mgr.get_state() == VoiceState.LISTENING
    assert mgr.is_active()

    # User speaks: transition to PROCESSING
    mgr.transition_to(VoiceState.PROCESSING)
    assert mgr.get_state() == VoiceState.PROCESSING
    assert mgr.is_active()

    # Assistant responds: transition to SPEAKING
    mgr.transition_to(VoiceState.SPEAKING)
    assert mgr.get_state() == VoiceState.SPEAKING
    assert mgr.is_active()

    # TTS completes: CRITICAL transition back to LISTENING
    mgr.transition_to(VoiceState.LISTENING)
    assert mgr.get_state() == VoiceState.LISTENING
    assert mgr.is_active()

    # User issues end-session command: STOPPING -> OFFLINE
    mgr.transition_to(VoiceState.STOPPING)
    assert mgr.get_state() == VoiceState.STOPPING
    assert mgr.is_active()

    # Terminate session
    final_state = mgr.stop_session()
    assert final_state == VoiceState.OFFLINE
    assert mgr.get_state() == VoiceState.OFFLINE
    assert not mgr.is_active()
