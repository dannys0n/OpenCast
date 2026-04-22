# OpenCast

OpenCast is a real-time Counter-Strike 2 commentary stack for turning live match state into spoken caster lines.

At a high level, the repo takes CS2 Game State Integration payloads, turns them into short event-aware prompts, generates commentary with a local text model, and speaks the result through streamed TTS. The current default stack is built around the `v5` GSI prompt pipeline and `omnivoice-server`.

## Use and Attribution

This repository is presented as an academic and portfolio project, not as a claim of full authorship over every component inside it.

- The root [LICENSE](/home/danny/Desktop/OpenCast/LICENSE) covers original OpenCast-specific work only.
- Third-party components inside the repo keep their own authorship and license terms.
- A short attribution summary lives in [THIRD_PARTY_NOTICES.md](/home/danny/Desktop/OpenCast/THIRD_PARTY_NOTICES.md).

## Current Pipeline

```text
CS2 GSI payloads
    ->
gsi_prompt_pipeline_v5.py
    - filters meaningful events
    - builds compact match context
    - derives tactical summary
    - alternates event and idle commentary modes
    ->
local text LLM (/v1/chat/completions)
    - generates short caster-ready lines
    ->
prompt_queue_v5.py
    - assigns lines to caster0 / caster1
    - routes each line to the right voice
    ->
omnivoice-server (/v1/audio/speech)
    - streams PCM audio
    ->
SoX play
    - immediate local playback
```

## What Makes This Version Different

- Event-first commentary: the pipeline reacts to kills, bomb states, round ends, and other meaningful match changes instead of narrating every snapshot.
- Two-caster flow: `v5` supports short event bundles plus quieter in-round exchanges between `caster0` and `caster1`.
- Voice-routed playback: play-by-play and color lines can use different cloned voices.
- OpenAI-style interfaces: both the text model and TTS layer expose familiar API shapes, which keeps the pipeline modular.
- Inspectable state: raw payloads, filtered batches, training wrappers, queue state, and runtime logs are written under `deployment/tts-io-full/gsi/pipeline/.state/v5/`.

## Default Local Stack

- GSI prompt runtime: `deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_v5.py`
- Text model: `deployment/text-llm/start_text_model.sh`
- TTS server: `deployment/tts-io-full/start_omnivoice_model.sh`
- Cast voice sources: `deployment/tts-io-full/voices/`

Default cast pairing in the current runtime:

- `caster0`: announcer-style play-by-play
- `caster1`: turret-style color/follow-up

## Quick Start

These scripts are Linux/WSL-oriented. The OmniVoice setup installs system packages like `sox`, `ffmpeg`, and `libportaudio2`.

### 1) Start OmniVoice

```bash
cd deployment/tts-io-full
cp omnivoice-server/.env.example omnivoice-server/.env
./setup_venv_omnivoice.sh
./start_omnivoice_model.sh
```

Default local endpoint:

- `http://127.0.0.1:8880`
- `GET /health`
- `POST /v1/audio/speech`

### 2) Start the Text Model

```bash
cd deployment/text-llm
cp .env.example .env
./start_text_model.sh
```

Default local endpoint:

- `http://127.0.0.1:12434/v1/chat/completions`

### 3) Start the Live Pipeline

```bash
cd deployment/tts-io-full/gsi/pipeline
python gsi_prompt_pipeline_v5.py
```

Default listener:

- `http://127.0.0.1:3000/`

Point your CS2 GSI config at that listener and the pipeline will handle filtering, prompting, generation, queueing, and playback locally.

## Repo Map

- `deployment/tts-io-full/gsi/pipeline/`: live CS2 listener, prompt building, queueing, tests, and runtime state
- `deployment/tts-io-full/omnivoice-server/`: OpenAI-compatible OmniVoice server checkout and config
- `deployment/tts-io-full/voices/`: local source clips for cloned caster voices
- `deployment/text-llm/`: local text generation service config and launcher
- `prototype/`: earlier notes and experiments

## Notes

- The older Qwen3 TTS path is still checked in under `deployment/tts-io-full/Qwen3-TTS-Openai-Fastapi/`, but it is no longer the default path described here.
- OmniVoice request-time tuning for OpenCast lives in `deployment/tts-io-full/omnivoice-server/.opencast.env`.
- The best quick reference for the live runtime behavior is the `v5` pipeline code and tests under `deployment/tts-io-full/gsi/pipeline/`.
