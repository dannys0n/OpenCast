# GSI Prompt Pipeline

This is a small, readable first-pass pipeline for CS2 commentary prompting.

Goals:

- keep the GSI listener relatively dumb
- always keep the latest full GSI snapshot in memory
- emit a prompt payload every 2 seconds
- also emit immediately on meaningful events
- store prompt JSONs locally in a tiny capped database
- avoid pulling in TTS or LLM code yet

Current design:

- one HTTP listener receives all GSI payloads
- one in-memory `latest_snapshot` is updated on every POST
- one interval worker emits a prompt every 2 seconds
- one event detector emits a prompt immediately when meaningful non-noisy changes happen
- one background worker sends prompt text to `deployment/text-llm`
- one TTS worker plays queued commentary one item at a time
- one JSON database stores the full prompt payloads and TTS queue state, capped to 50 entries

Important constraints:

- same caster voice for now
- `caster_override` still matters because it changes commentary style
- prompting is stubbed for now, so this stores prompt inputs rather than generated speech
- text generation now uses the existing `deployment/text-llm` OpenAI-compatible endpoint
- TTS dispatch now streams queued commentary into the local Qwen3-TTS OpenAI-compatible endpoint
- if the text model is unreliable with JSON, use the plain-text variant below

Run:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline.py
```

Plain-text LLM variant:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_text_only.py
```

Main output files:

- `pipeline/.state/tts_prompt_database.json`
- `pipeline/.state/pipeline.log`

What gets stored:

- instruction
- full gameplay snapshot
- compact gameplay summary
- recent event details
- caster override
- emotion override
- speed override
- trigger metadata
- output schema
- assembled prompt text
- text-llm raw/parsed output
- TTS-ready prompt JSON
- TTS queue / completion status
