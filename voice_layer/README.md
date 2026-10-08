# AeroGuard Voice Layer (AGY)

An operational, JARVIS-style LLM-powered voice interface designed specifically for the **AeroGuard** mesh telemetry and ground control system.

> **CRITICAL ARCHITECTURAL RULES:**  
> 1. **Zero Telemetry Fabrication**: The LLM is strictly prohibited from guessing or hallucinating telemetry values. Telemetry and node metrics are retrieved exclusively via structured AeroGuard tools.
> 2. **JAZS Independence**: This voice layer is completely independent of JAZS. It does not integrate, import, or share architecture, database models, wake-word logic, or naming conventions with JAZS.
> 3. **Rule-Based Resilience**: If an LLM provider is offline or unconfigured, AGY automatically falls back to deterministic rule-based command execution without crashing.

---

## 1. What the Voice Layer Does

The AeroGuard Voice Layer provides hands-free, operational voice control and status query capabilities for flight operators, ground station crew, and telemetry monitors:

- **Speech-to-Text (STT)**: Powered by **Cartesia Ink** (`ink-2` for continuous real-time WebSocket turn detection, `ink-whisper` for push-to-talk audio buffers).
- **AGY Voice Agent**: Coordinates conversational turns, intent parsing, and tool execution.
- **LLM Function & Tool Calling**: Intelligently inspects operator queries and invokes defined AeroGuard tools (`get_node_telemetry`, `get_node_status`, `get_aircraft_status`, `get_system_status`, `get_active_alerts`).
- **Telemetry Provider Layer**: Supports **Mock Mode** (deterministic simulations for testing when physical ground nodes are offline) and **Live Mode** (interfacing directly with the Ground Station backend via `GET /api/log`).
- **Response Synthesis**: Synthesizes concise, JARVIS-style auditory feedback suitable for Text-to-Speech (no Markdown, natural spoken units).
- **Text-to-Speech (TTS)**: Powered by **Cartesia Sonic** (`sonic-latest`) streaming crisp, low-latency audio to the operator's speaker.

---

## 2. Architecture Diagram

```text
       Operator Microphone
                │
                ▼
       [voice_layer/audio.py] (PCM 16kHz Mono)
                │
                ▼
       [voice_layer/stt.py] (Cartesia Ink STT)
                │
                ▼ Transcript
       [voice_layer/agent.py] (AeroGuardAgent)
                │
                ├───────────────────────────────────────┐
                ▼                                       ▼
       [voice_layer/llm/client.py] (LLM Engine)   [Fallback Rule Engine]
                │                                 (Active if LLM offline)
                ▼ Tool Call Request
       [voice_layer/tools/registry.py] (Tool Registry)
                │
                ├───────────────┬───────────────────────┐
                ▼               ▼                       ▼
      [get_node_telemetry] [get_node_status]   [get_aircraft_status]
                │
                ▼
       [voice_layer/tools/provider.py]
                ├─► MOCK: Deterministic Telemetry (AEROGUARD_DATA_MODE=mock)
                └─► LIVE: Ground Station Backend (AEROGUARD_DATA_MODE=live)
                          (GET /api/log via voice_layer/aero_api.py)
                │
                ▼ Telemetry Payload
       [voice_layer/llm/client.py] (JARVIS Response Formatting)
                │
                ▼ Response Text
       [voice_layer/tts.py] (Cartesia Sonic TTS)
                │
                ▼
       [voice_layer/audio.py] (Speaker Playback)
                │
                ▼
          Operator Speaker
```

---

## 3. Folder Structure

```text
aero_final/
├── .gitignore
├── src/
│   └── web_UI.html              # Unified Ground Station Web Dashboard
└── voice_layer/
    ├── __init__.py              # Package initialization
    ├── config.py                # Environment loader and validation
    ├── audio.py                 # Microphone capture & speaker playback
    ├── stt.py                   # Cartesia Ink Speech-to-Text
    ├── tts.py                   # Cartesia Sonic Text-to-Speech
    ├── aero_api.py              # AeroGuard Ground Station API client (GET /api/log)
    ├── agent.py                 # Core AGY agent with LLM tool calling & rule fallback
    ├── session.py               # Persistent voice session state machine & intent gate
    ├── web_api.py               # FastAPI REST bridge connecting web dashboard to AGY agent
    ├── main.py                  # CLI application entry point
    ├── requirements.txt         # Project dependencies
    ├── .env                     # Local environment settings
    ├── .env.example             # Configuration template
    ├── README.md                # Voice layer documentation
    ├── llm/
    │   ├── __init__.py
    │   ├── client.py            # OpenAI-compatible & Mock LLM clients
    │   └── prompts.py           # JARVIS system prompt and operational guidelines
    ├── tools/
    │   ├── __init__.py
    │   ├── provider.py          # Live vs Mock telemetry provider abstraction
    │   ├── registry.py          # Tool registry and JSON schema definitions
    │   ├── system.py            # get_system_status tool
    │   ├── telemetry.py         # get_node_status, get_node_telemetry tools
    │   ├── aircraft.py          # get_aircraft_status tool
    │   └── alerts.py            # get_active_alerts tool
    └── tests/
        ├── __init__.py
        ├── test_config.py       # Configuration unit tests
        ├── test_agent.py        # Intent, routing & mock API tests
        ├── test_llm.py          # LLM, tool selection, mock telemetry & fallback tests
        ├── test_session.py      # Session state machine & intent detection tests
        └── test_web_api.py      # Web bridge REST API & CORS tests
```

---

## 4. Required API Keys & Configuration

### API Keys
1. **Cartesia API Key** (`CARTESIA_API_KEY`): Required for Cartesia Ink (STT) and Sonic (TTS). Obtain from [play.cartesia.ai](https://play.cartesia.ai/keys).
2. **LLM API Key** (`LLM_API_KEY`): Required when using live LLM providers such as Groq or OpenAI.
   - **Groq (Recommended)**: Extremely fast (<200ms latency, ideal for voice) and offers a free development tier. Obtain from [console.groq.com](https://console.groq.com/keys).
   - **OpenAI**: Compatible with standard OpenAI API keys (`gpt-4o-mini`).
   - **Local / Ollama**: No API key required when using local Ollama (`http://localhost:11434/v1`).
   - **Mock**: Set `LLM_PROVIDER=mock` for testing without any LLM API key.

### Configuration Template (`voice_layer/.env`)

```ini
# Cartesia API Configuration
CARTESIA_API_KEY=sk_car_...

# STT & TTS Models
CARTESIA_STT_MODEL=ink-2
CARTESIA_TTS_MODEL=sonic-latest
CARTESIA_VOICE_ID=79a125e8-cd45-4c13-8a67-188112f4dd22
CARTESIA_LANGUAGE=en

# AeroGuard Ground Station API
AEROGUARD_API_BASE_URL=http://localhost:8080

# Telemetry Data Mode: 'mock' (simulated offline) or 'live' (real /api/log)
AEROGUARD_DATA_MODE=mock

# LLM Configuration
# Providers: mock, groq, openai, openrouter, ollama
LLM_PROVIDER=mock
LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=gsk_...
# Optional custom endpoint:
# LLM_API_BASE_URL=https://api.groq.com/openai/v1
LLM_TEMPERATURE=0.2

# Logging Level (DEBUG, INFO, WARNING, ERROR)
VOICE_LOG_LEVEL=INFO
```

---

## 5. Mock Mode vs Live Mode

You can seamlessly switch telemetry sourcing using `AEROGUARD_DATA_MODE` in `.env`:

### Mock Mode (`AEROGUARD_DATA_MODE=mock`)
Ideal for standalone development, testing, and demos without requiring physical ESP32/LoRa hardware or an active Ground Station server:
- **Node 1**: Online, Battery 94%, Temperature 27.5 °C, Altitude 120 m, Pressure 1008 hPa.
- **Node 2**: Online, Battery 87%, Temperature 28.5 °C, Altitude 145 m, Pressure 1005 hPa.
- **Node 3**: Deterministically Offline.
- **Aircraft AG7423**: Tracked, Altitude 35,000 ft, Speed 480 knots, Heading 270°.
- **Alerts**: Nominal (no active distress beacons).

### Live Mode (`AEROGUARD_DATA_MODE=live`)
Interfaces directly with the AeroGuard Python Ground Station backend:
- Queries `GET /api/log` (matching `src/web_UI.html`).
- Automatically parses LoRa telemetry packets for active node telemetry, battery percentages, and emergency beacons.
- If the Ground Station is temporarily unreachable, AGY politely notifies the operator without crashing.

---

## 6. How AGY Decides Which Tool to Call

AGY provides the LLM with structured function definitions adhering to standard OpenAPI schemas:

| Function Name | Parameters | Purpose |
| :--- | :--- | :--- |
| `get_system_status` | *None* | Retrieves overall mesh health, total packet counts, and active node counts. |
| `get_node_status` | `node_id` (str) | Checks whether a specific node is ONLINE or OFFLINE, plus its battery percentage. |
| `get_node_telemetry` | `node_id` (str), `field` (enum: battery, temperature, altitude, pressure, speed) | Fetches exact measured sensor telemetry. **Mandatory** for any metric inquiry. |
| `get_aircraft_status` | `callsign` (str, default: 'AG7423') | Retrieves altitude, airspeed, heading, and tracking status for monitored aircraft. |
| `get_active_alerts` | *None* | Checks for active distress signals, warnings, or emergency beacons. |

### Decision Flow:
1. **User asks**: *"What is the battery level of Node 2?"*
2. **LLM analyzes**: Recognizes an inquiry about a measured metric on a specific node.
3. **Tool Invocation**: Emits `get_node_telemetry(node_id="2", field="battery")`.
4. **Tool Execution**: Reads value from telemetry provider: `{"node_id": "2", "field": "battery", "value": 87.0, "unit": "%"}`.
5. **Synthesis**: LLM receives verified tool result and speaks: *"Node 2's battery is at 87 percent."*

---

## 7. How to Run

### Mode 1: Push-To-Talk (Recommended Interactive Mode)
Press `[ENTER]` to speak into your microphone, or type a command directly into the prompt:
```powershell
python -m voice_layer.main --mode ptt
```

### Mode 2: Realtime Streaming Mode
Continuously monitors the microphone using Cartesia Ink native turn detection (`turn.start`, `turn.update`, `turn.end`):
```powershell
python -m voice_layer.main --mode realtime
```

### Mode 3: Direct Query Mode (Automated / Headless Testing)
Execute a single voice command query directly through the complete STT/LLM/Tool/TTS pipeline:
```powershell
python -m voice_layer.main --query "What is the battery level of Node 2"
python -m voice_layer.main --query "What is Node 1 temperature?"
python -m voice_layer.main --query "Is Node 3 online?"
python -m voice_layer.main --query "What is the status of aircraft AG7423?"
python -m voice_layer.main --query "Are there any emergencies?"
```

### Mode 4: Web-Integrated Voice Bridge (Browser Dashboard HUD Mode)
Starts the FastAPI REST bridge connecting the web dashboard (`src/web_UI.html`) to AGY and Cartesia TTS:
```powershell
python -m voice_layer.web_api
# or: uvicorn voice_layer.web_api:app --host 0.0.0.0 --port 8001
```
Then start the Ground Station web dashboard:
```powershell
python aeroguard_sim.py --role gs --id GS001 --port 9100 --web-port 8080
```
Open `http://localhost:8080` in Chrome/Edge and click the microphone button in the AGY Voice HUD (bottom-right).

---

## 8. Example Voice Commands & Responses

| Spoken Query | Tool Invoked | Sample Spoken Response |
| :--- | :--- | :--- |
| *"What is the battery level of Node 2?"* | `get_node_telemetry(node_id="2", field="battery")` | *"Node 2's battery is at 87 percent."* |
| *"What is Node 1 temperature?"* | `get_node_telemetry(node_id="1", field="temperature")` | *"Node 1 temperature is 27.5 degrees Celsius."* |
| *"Is Node 3 online?"* | `get_node_status(node_id="3")` | *"Node 3 is currently offline."* |
| *"Show me aircraft AG7423 status"* | `get_aircraft_status(callsign="AG7423")` | *"Aircraft AG7423 is tracked. Altitude 35000 feet, speed 480 knots."* |
| *"Are there any emergencies?"* | `get_active_alerts()` | *"There are no active emergency alerts. All monitored systems nominal."* |
| *"How is AeroGuard doing?"* | `get_system_status()` | *"AeroGuard system is fully operational. 2 mesh nodes active, 142 packets received."* |
| *"Hello AeroGuard"* | *Direct conversational* | *"AeroGuard online and monitoring. Standing by for command."* |
| *"What can you do?"* | *Direct conversational* | *"I can monitor mesh nodes, report telemetry such as battery or temperature, track aircraft, and check emergency alerts."* |
| *"Stop voice assistant"* | *Shutdown command* | *"Shutting down AeroGuard voice assistant. Radar offline."* |

---

## 9. Testing & Verification

Run the entire test suite (45 tests) verifying rule-based dispatch, LLM integration, tool selection, mock telemetry, web API bridge, persistent voice session lifecycle, and offline fallbacks:

```powershell
python -m pytest voice_layer/tests -v
```

All 45 tests verify:
- Configuration validation and environment loading.
- Deterministic mock telemetry output across all nodes and aircraft.
- Tool schemas and execution via `ToolRegistry`.
- Protection against telemetry fabrication.
- Graceful activation of rule-based fallback when the LLM is offline or unconfigured.
- Proper handling when the Ground Station backend is offline.
- Persistent voice session state machine (`OFFLINE` -> `LISTENING` -> `PROCESSING` -> `SPEAKING` -> `LISTENING` -> `STOPPING` -> `OFFLINE`).
- Intent classification: `NORMAL_REQUEST`, `END_SESSION`, and background conversation `IGNORE`.
- Courtesy disambiguation (e.g. *"Thank you, now tell me the battery voltage"* processed as `NORMAL_REQUEST`).
- Web API bridge endpoints (`/voice/health`, `/voice/query`, `/voice/session/*`), CORS headers, and TTS payload generation.
