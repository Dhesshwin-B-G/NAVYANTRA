"""AeroGuard Voice Layer - Web API Bridge.

Provides a lightweight, secure REST API bridge connecting the AeroGuard Ground Station
web dashboard (src/web_UI.html) to the existing AGY voice agent and Cartesia TTS.
"""

import base64
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from voice_layer.aero_api import AeroGuardClient
from voice_layer.agent import AeroGuardAgent
from voice_layer.config import VoiceConfig
from voice_layer.llm.client import create_llm_client
from voice_layer.session import (
    VoiceIntent,
    VoiceSessionManager,
    VoiceState,
    detect_voice_intent,
)
from voice_layer.tools.provider import LiveTelemetryProvider, MockTelemetryProvider
from voice_layer.tools.registry import ToolRegistry
from voice_layer.tts import CartesiaTTS, TTSError

logger = logging.getLogger("AeroGuard.WebAPI")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_service()
    logger.info("[BRIDGE] AeroGuard Voice Bridge initialized and ready.")
    yield


app = FastAPI(
    title="AeroGuard Voice Bridge",
    description="Bridge connecting web_UI.html to existing AGY agent and Cartesia TTS",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: Allow web dashboard on localhost (any port including 3000, 8080, 8000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    text: str


class QueryResponse(BaseModel):
    query: str
    response: str
    audio_base64: Optional[str] = None
    mime_type: Optional[str] = None
    should_exit: bool = False
    intent: str = "NORMAL_REQUEST"
    state: str = "LISTENING"
    session_active: bool = False


# Service state container
class VoiceService:
    def __init__(self) -> None:
        self.config = VoiceConfig.from_env()
        self.api_client = AeroGuardClient(base_url=self.config.aeroguard_api_base_url)

        if self.config.is_mock_telemetry:
            self.provider = MockTelemetryProvider()
            logger.info("[BRIDGE] Telemetry provider: MOCK")
        else:
            self.provider = LiveTelemetryProvider(self.api_client)
            logger.info(f"[BRIDGE] Telemetry provider: LIVE ({self.config.aeroguard_api_base_url})")

        self.tool_registry = ToolRegistry(provider=self.provider)
        self.llm_client = create_llm_client(self.config)
        self.agent = AeroGuardAgent(
            api_client=self.api_client,
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
        )
        self.tts = CartesiaTTS(self.config)
        self.session_manager = VoiceSessionManager()


service: Optional[VoiceService] = None


def get_service() -> VoiceService:
    global service
    if service is None:
        service = VoiceService()
    return service




@app.get("/voice/health")
def health_check() -> Dict[str, Any]:
    svc = get_service()
    return {
        "status": "healthy",
        "telemetry_mode": svc.config.aeroguard_data_mode,
        "is_llm_configured": svc.config.is_llm_configured,
        "is_cartesia_configured": svc.config.is_cartesia_configured,
        "session_active": svc.session_manager.is_active(),
        "state": svc.session_manager.get_state().value,
    }


@app.post("/voice/session/start")
def start_session() -> Dict[str, Any]:
    svc = get_service()
    state = svc.session_manager.start_session()
    return {"session_active": True, "state": state.value}


@app.post("/voice/session/stop")
def stop_session() -> Dict[str, Any]:
    svc = get_service()
    state = svc.session_manager.stop_session()
    return {"session_active": False, "state": state.value}


@app.get("/voice/session/status")
def session_status() -> Dict[str, Any]:
    svc = get_service()
    return {
        "session_active": svc.session_manager.is_active(),
        "state": svc.session_manager.get_state().value,
    }


@app.post("/voice/query", response_model=QueryResponse)
def handle_voice_query(req: QueryRequest) -> QueryResponse:
    svc = get_service()
    clean_query = req.text.strip()
    if not clean_query:
        raise HTTPException(status_code=400, detail="Query text cannot be empty.")

    print("[AGY] request received")
    print(f"[AGY] transcript = \"{clean_query}\"")
    logger.info(f"[AGY] request received: transcript = '{clean_query}'")

    # Intent detection & background chatter gate
    print("[AGY] intent classification started")
    logger.info("[AGY] intent classification started")
    intent, processed_text = detect_voice_intent(
        clean_query,
        in_session=svc.session_manager.is_active() or True,
    )
    print(f"[AGY] intent classification completed: {intent.value}")
    logger.info(f"[AGY] intent classification completed: {intent.value}")
    print(f"[VOICE] Speech detected")
    print(f"[VOICE] Transcript: \"{clean_query}\"")
    print(f"[VOICE] Intent: {intent.value}")
    logger.info(f"[VOICE] Speech detected")
    logger.info(f"[VOICE] Transcript: \"{clean_query}\"")
    logger.info(f"[VOICE] Intent: {intent.value}")

    # Case A: IGNORE (background noise or unrelated side conversation)
    if intent == VoiceIntent.IGNORE:
        logger.info(f"[BRIDGE] Utterance ignored as background chatter: '{clean_query}'")
        return QueryResponse(
            query=clean_query,
            response="",
            audio_base64=None,
            mime_type=None,
            should_exit=False,
            intent=VoiceIntent.IGNORE.value,
            state=VoiceState.LISTENING.value if svc.session_manager.is_active() else VoiceState.OFFLINE.value,
            session_active=svc.session_manager.is_active(),
        )

    # Case B: END_SESSION (explicit closing / goodbye)
    if intent == VoiceIntent.END_SESSION:
        svc.session_manager.transition_to(VoiceState.STOPPING)
        response_text = "Shutting down AeroGuard voice assistant. Radar offline."
        audio_b64: Optional[str] = None
        mime_type: Optional[str] = None

        if svc.config.is_cartesia_configured:
            try:
                wav_bytes = svc.tts.synthesize_bytes(response_text)
                if wav_bytes:
                    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
                    mime_type = "audio/wav"
            except Exception as e:
                logger.warning(f"[BRIDGE] Error synthesizing exit audio: {e}")

        svc.session_manager.stop_session()
        return QueryResponse(
            query=clean_query,
            response=response_text,
            audio_base64=audio_b64,
            mime_type=mime_type,
            should_exit=True,
            intent=VoiceIntent.END_SESSION.value,
            state=VoiceState.OFFLINE.value,
            session_active=False,
        )

    # Case C: NORMAL_REQUEST
    svc.session_manager.transition_to(VoiceState.PROCESSING)

    # 1. Process query through existing AGY agent
    try:
        response_text, should_exit = svc.agent.process_query(processed_text or clean_query)
    except Exception as e:
        logger.error(f"[BRIDGE] Agent query processing failed: {e}", exc_info=True)
        response_text = "An internal error occurred while processing the request."
        should_exit = False

    svc.session_manager.transition_to(VoiceState.SPEAKING)

    # 2. Synthesize audio with existing Cartesia TTS if configured
    audio_b64 = None
    mime_type = None

    if svc.config.is_cartesia_configured and response_text:
        try:
            wav_bytes = svc.tts.synthesize_bytes(response_text)
            if wav_bytes:
                audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
                mime_type = "audio/wav"
        except TTSError as e:
            logger.warning(f"[BRIDGE] Cartesia TTS error: {e}")
        except Exception as e:
            logger.warning(f"[BRIDGE] Unexpected error during TTS synthesis: {e}")

    if should_exit:
        svc.session_manager.stop_session()
        return QueryResponse(
            query=clean_query,
            response=response_text,
            audio_base64=audio_b64,
            mime_type=mime_type,
            should_exit=True,
            intent=VoiceIntent.END_SESSION.value,
            state=VoiceState.OFFLINE.value,
            session_active=False,
        )

    print("[AGY] response returned to frontend")
    logger.info("[AGY] response returned to frontend")

    # Normal answer: session remains alive!
    return QueryResponse(
        query=clean_query,
        response=response_text,
        audio_base64=audio_b64,
        mime_type=mime_type,
        should_exit=False,
        intent=VoiceIntent.NORMAL_REQUEST.value,
        state=VoiceState.SPEAKING.value,
        session_active=True,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("voice_layer.web_api:app", host="0.0.0.0", port=8001, reload=False)

