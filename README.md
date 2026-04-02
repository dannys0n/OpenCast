# OpenCast Qwen3-TTS Setup

This repo is a clean local wrapper around `vllm-omni` for Qwen3-TTS on Linux/NVIDIA. It is set up to validate three things first:

- streaming PCM audio from `/v1/audio/speech`
- voice cloning with the `Base` model
- incremental text input over `/v1/audio/speech/stream`

The default path follows the current upstream recommendation:

- `uv`
- Python `3.12`
- host CUDA
- `vllm==0.18.0`
- `vllm-omni` installed from source at `v0.18.0`

## Requirements
- Linux
- NVIDIA GPU with CUDA support
- Python `3.12`
- `uv`
- enough VRAM for the selected model

This machine profile was tuned around a 12 GB GPU, so the default runtime path starts with:

- `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`
- `Qwen/Qwen3-TTS-12Hz-0.6B-Base`

`VoiceDesign` is left available, but only on the documented `1.7B` variant.
For this VRAM tier, the checked-in defaults also use `gpu_memory_utilization=0.3` and `max_model_len=8192`.

## Quick Start
1. Copy `.env.example` to `.env` and fill in `HF_TOKEN` if you need gated Hugging Face access.
2. Bootstrap the local environment:

```bash
bash ./setup_host_env.sh
```

3. Activate the environment:

```bash
source .venv/bin/activate
```

4. Start the default streaming-friendly server profile:

```bash
bash ./run_tts_server.sh --task-type CustomVoice --model-size 0.6B
```

## Repo Layout
The repo now separates the two model stacks while keeping the easiest commands at the root:

- repo root: wrappers, docs, env files, and setup
- `tts-model/`: Qwen3-TTS and vLLM-Omni implementation files
- `text-model/`: Docker Model Runner commentary implementation, prompts, and sample inputs
- `templates/`: reference material only

The supported operator workflow remains the root-level shell scripts.

## Common Commands
Start the `Base` voice cloning profile:

```bash
bash ./run_tts_server.sh --task-type Base --model-size 0.6B
```

Start the browser demo while also launching the server:

```bash
bash ./run_gradio_demo.sh --task-type CustomVoice --model-size 0.6B
```

Run the full smoke-test suite against a running server:

```bash
bash ./smoke_test_qwen3_tts.sh all
```

Run one smoke test at a time:

```bash
bash ./smoke_test_qwen3_tts.sh http-stream
bash ./smoke_test_qwen3_tts.sh voice-clone
bash ./smoke_test_qwen3_tts.sh ws-stream
```

## Commentary Bridge
The first commentary milestone keeps the small text model local and scriptable:

- Docker Model Runner serves the GGUF commentary model over `http://localhost:12434/engines/v1`
- `text-model/live_commentary_bridge.py` turns short match-state notes into shoutcaster-style lines
- those lines stream into the existing TTS WebSocket path at `ws://localhost:8091/v1/audio/speech/stream`

Recommended commentary model:

```bash
docker model run hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M
```

On this single-GPU setup, prime Docker Model Runner first so the GGUF model claims a small, explicit slice of GPU VRAM before Qwen3-TTS starts:

```bash
bash ./setup_commentary_model_runner.sh
bash ./run_tts_server.sh --task-type CustomVoice --model-size 0.6B
```

With the text model primed and the TTS server running, use the bridge wrapper:

```bash
bash ./run_commentary_bridge.sh --input-file ./text-model/examples/sample_match_state.txt
```

That command now plays streamed PCM live through your local audio stack as chunks arrive. The bridge auto-selects `pw-play`, then `aplay`, then `ffplay`, and still saves sentence artifacts under `.cache/` for debugging.

If you want to force a specific backend or disable speaker playback:

```bash
bash ./run_commentary_bridge.sh --audio-player pw-play --input-file ./text-model/examples/sample_match_state.txt
bash ./run_commentary_bridge.sh --no-play-live --input-file ./text-model/examples/sample_match_state.txt
```

The wrapper checks:

- Docker and `docker model`
- the Docker Model Runner API at `OPENCAST_COMMENTARY_DMR_BASE_URL`
- the TTS API at `OPENCAST_TTS_API_BASE`

Artifacts from the bridge land under `.cache/opencast/commentary-bridge/`.

For a repeatable end-to-end check:

```bash
bash ./smoke_test_commentary_bridge.sh
```

The commentary smoke path primes the GGUF model first and then starts the TTS server if needed.

## Helper Scripts
- `setup_host_env.sh`: creates `.venv`, installs local helper deps, installs `vllm==0.18.0`, clones `vllm-omni` at `v0.18.0`, and installs it editable.
- `run_tts_server.sh`: root wrapper for the TTS launcher in `tts-model/`.
- `run_gradio_demo.sh`: root wrapper that starts the TTS server and the Gradio demo from `tts-model/`.
- `smoke_test_qwen3_tts.sh`: root wrapper for repeatable TTS validation in `tts-model/`.
- `setup_commentary_model_runner.sh`: root wrapper that configures the small GGUF commentary model for limited GPU offload and primes it before the TTS server starts.
- `run_commentary_bridge.sh`: root wrapper that checks the commentary model and TTS endpoints before running the bridge in `text-model/`.
- `smoke_test_commentary_bridge.sh`: root wrapper for end-to-end commentary generation plus streaming TTS validation.
- `opencast_audio.py`: shared low-latency PCM playback helper for local live monitoring.
- `tts-model/`: TTS server, clients, stage config, Gradio wrapper, and runtime patch.
- `text-model/`: commentary model client, bridge, prompts, and sample match-state input.

## Templates
`templates/` is reference material only.

- Do not treat the files under `templates/` as the supported workflow.
- The maintained, reproducible entrypoints for this repo are the root-level scripts and docs.
- Keep `templates/` around as upstream-inspired examples and scratch references.

## Notes
- The checked-in stage config now lives at `tts-model/qwen3_tts.yaml`, which removes the old dependency on running the upstream example from inside the `vllm-omni` checkout.
- The repo-local launchers also redirect Hugging Face downloads into this repo's `.cache/` directory, so model downloads do not depend on a writable global cache.
- Streaming audio from `/v1/audio/speech` requires `stream=true` with `response_format="pcm"`.
- Incremental text input uses `ws://.../v1/audio/speech/stream`.
- The commentary bridge plays streamed PCM live by default; set `OPENCAST_COMMENTARY_PLAY_LIVE=0` or pass `--no-play-live` to disable speaker playback.
- For this 12 GB single-GPU workflow, the checked-in commentary path configures Docker Model Runner for limited GPU offload and primes the text model first so it can coexist with the TTS server.
- On a 12 GB GPU, the validated `Base` smoke path uses `x_vector_only_mode`. Full transcript-guided ICL cloning can still OOM this card.
- Output artifacts are written under `.cache/opencast/`.
