# GSI Prompt Pipeline

This is a small, readable first-pass pipeline for CS2 commentary prompting.

Fresh v2 reset:

- `gsi_prompt_pipeline_v2.py` is the clean restart for the new event-first flow.
- `prompt_queue_v2.py` is the new stubbed prompt handoff module used by the v2 listener.
- It only listens for GSI POSTs, appends every raw payload for future reference, filters a small set of important events, and formats player associations cleanly.
- The v2 output files are pretty-printed multi-line JSON records so they stay human readable.
- It also keeps overwrite-on-update `latest` files so you can always inspect the most recent raw payload and most recent filtered event batch quickly.
- Filtered batches are now also handed to a separate stub prompt queue module so the future LLM step has a clean seam.
- Filtered output removes the camera-focused player entirely so prompting stays centered on actual event actors.
- Filtered output is event-only: no global snapshot summary, no transition context, and no before/after pairs unless an event truly needs them.
- Filtered player records omit noisy position and nonessential stats, and event records keep only the after-state details directly relevant to the event.
- Live grenade entities are included only when a thrown grenade appears in top-level `grenades` or `allgrenades`.
- Starting a new v2 listener session clears the old v2 state files instead of preserving stale cross-session history.
- It intentionally does not call the text model or TTS yet, but it now queues stub prompt jobs for that future step.

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

Run the fresh v2 script:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_v2.py
```

Plain-text LLM variant:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_text_only.py
```

Main output files:

- `pipeline/.state/tts_prompt_database.json`
- `pipeline/.state/pipeline.log`

V2 output files:

- `pipeline/.state/v2/gsi_received_pretty.jsonl`
- `pipeline/.state/v2/gsi_received_latest.json`
- `pipeline/.state/v2/gsi_filtered_pretty.jsonl`
- `pipeline/.state/v2/gsi_filtered_latest.json`
- `pipeline/.state/v2/prompt_queue_pretty.jsonl`
- `pipeline/.state/v2/prompt_queue_latest.json`
- `pipeline/.state/v2/prompt_queue_state.json`
- `pipeline/.state/v2/pipeline_v2.log`

What gets stored:

- raw GSI payload history and latest snapshot
- filtered event history and latest batch
- stub prompt jobs built from filtered events
- prompt queue latest item and pending queue state
