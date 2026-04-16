# OpenCast

Real-time CS2 esports commentary pipeline: CS2 GSI events -> event-first prompting -> text LLM -> streamed Qwen3-TTS playback.

This repo wires three local components into an end-to-end “caster line” system:

- **GSI listener + prompt pipeline** (`deployment/tts-io-full/gsi/pipeline/`): receives gameplay snapshots, filters important events, and produces a short prompt.
- **Text LLM** (`deployment/text-llm/`): serves an **OpenAI-compatible** `POST /v1/chat/completions` API that returns structured commentary metadata.
- **Qwen3-TTS server** (`deployment/tts-io-full/`): serves an **OpenAI-compatible** `POST /v1/audio/speech` API that streams PCM to the audio output.

## Architecture

```text
            CS2 GSI (JSON)
                   |
                   v
        [ gsi_prompt_pipeline_v3.py ]
          - stores raw/filtered snapshots (event + interval prompts)
          - builds prompt (training wrapper + gameplay snapshot)
                   |
                   v
        [ Text LLM: /v1/chat/completions ]
          - returns one short caster line (+ metadata)
                   |
                   v
        [ Qwen3-TTS: /v1/audio/speech ]
          - stream=true + response_format=pcm
                   |
                   v
            SoX `play` (PCM streaming)
```

## Key Highlights

- **Event-first prompting**: keeps the GSI listener “dumb” and isolates prompting/runtime logic outside the HTTP handler.
- **Structured model output**: the text LLM is instructed to return JSON with consistent keys (`commentary`, `caster`, `emotion`).
- **Streaming TTS**: the TTS server streams PCM chunks; playback starts immediately (lower latency).
- **Voice cloning support**: TTS uses Qwen3-TTS’s voice-library profiles (`clone:<profile>` style voices).
- **Local-only & OpenAI-compatible endpoints**: both the text and TTS services can be consumed with OpenAI-style API calls.

## Getting Started (Local)

These scripts are bash- and Linux/WSL-oriented (they install SoX and use `apt` in `setup_venv.sh`). If you run on Windows directly, use WSL or adapt the setup.

### 1) Start Qwen3-TTS (audio server)

```bash
cd deployment/tts-io-full
cp .env.example .env
./setup_venv.sh
./start_tts_model.sh
```

Default server:

- HTTP base: `http://127.0.0.1:8880`
- `GET /health`
- `POST /v1/audio/speech` (set `stream=true`, `response_format=pcm` for PCM streaming)
- `GET /v1/voices`

Notes:

- Update paths in `.env` if you are not using the same repo location as in the example.
- `start_tts_model.sh` preloads clone voices into cache by default.

### 2) Start the Text LLM (commentary generator)

```bash
cd deployment/text-llm
cp .env.example .env
./start_text_model.sh
```

Default endpoint:

- `http://127.0.0.1:12434/v1/chat/completions`

The text LLM is configured (via `SYSTEM_PROMPT` in `.env`) to return a **single short** commentary line plus `caster` and `emotion` labels.

### 3) Run the GSI Prompt Pipeline (CS2 -> LLM -> TTS)

```bash
cd deployment/tts-io-full/gsi/pipeline
python gsi_prompt_pipeline_v3.py
```

Default listener:

- `CS2_GSI_HOST=127.0.0.1`
- `CS2_GSI_PORT=3000`

So your CS2 GSI should POST JSON to:

- `http://127.0.0.1:3000/`

Optional:

- Set `CS2_GSI_AUTH_TOKEN` (pipeline checks `payload["auth"]["token"]` and returns `403` on mismatch).
- Set `CS2_GSI_KILL_EXISTING_LISTENER=true` to reclaim the port automatically.

Pipeline behavior (v3):

- stores raw payloads and filtered event batches under `pipeline/.state/v3/`
- writes prompt “training wrapper” snapshots for prompt/runtime iteration
- sends filtered batches to the text LLM
- dispatches the resulting commentary to the TTS server
- plays audio immediately via SoX `play`

## Customization

### Default voice / caster identity

The TTS playback uses a default voice set in `deployment/tts-io-full/.env`:

- `TTS_DEFAULT_VOICE_NAME` (example default: `clone:scrawny_e0`)

Voices live under:

- `deployment/tts-io-full/Qwen3-TTS-Openai-Fastapi/voice_library/`

### Prompt & gameplay-event filtering

Prompt instructions and event shaping live in:

- `deployment/tts-io-full/gsi/pipeline/prompt_config_v3.json`
- `deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_v3.py`

## Project Structure (high level)

- `deployment/tts-io-full/`: TTS server + GSI integration + voice library
- `deployment/text-llm/`: OpenAI-compatible text LLM service
- `deployment/tts-io-full/gsi/pipeline/`: GSI listener, event filtering, prompt runtime, and TTS dispatch
- `prototype/`: earlier local-stack notes and experiments

## Testing

The GSI pipeline includes test modules next to the runtime scripts (e.g. `test_gsi_prompt_pipeline_v3.py`).
If you run tests from this folder, you can start with the v3 tests to validate the prompt runtime + queueing behavior.

