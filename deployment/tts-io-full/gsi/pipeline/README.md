# GSI Prompt Pipeline

This is a small, readable first-pass pipeline for CS2 commentary prompting.

Fresh v2 reset:

- `gsi_prompt_pipeline_v2.py` is the clean restart for the new event-first flow.
- `prompt_queue_v2.py` now handles the immediate prompt/runtime handoff used by the v2 listener.
- It only listens for GSI POSTs, appends every raw payload for future reference, filters a small set of important events, and formats player associations cleanly.
- The v2 output files are pretty-printed multi-line JSON records so they stay human readable.
- It also keeps overwrite-on-update `latest` files so you can always inspect the most recent raw payload and most recent filtered event batch quickly.
- Filtered batches are now also handed to a separate prompt runtime module so prompting stays isolated from the listener.
- Filter cleanup now happens in the filtering layer itself, so the filtered JSON is already prompt-ready before the prompt runtime sees it.
- Filtered output removes the camera-focused player entirely so prompting stays centered on actual event actors.
- Filtered output is event-only: no global snapshot summary, no transition context, and no before/after pairs unless an event truly needs them.
- Filtered player records omit noisy position, entity ids, association metadata, and other prompt-hostile internals.
- Live grenade entities are included only when a thrown grenade appears in top-level `grenades` or `allgrenades`.
- Starting a new v2 listener session clears the old v2 state files instead of preserving stale cross-session history.
- The current testing path immediately sends filtered batches into the text LLM and then straight into TTS playback with no delayed queue.

Goals:

- keep the GSI listener relatively dumb
- keep prompting logic outside the HTTP listener
- use an `Instruction` system prompt plus filtered `Gameplay snapshot`
- generate one very short caster sentence from the text LLM
- play that sentence back immediately

Current design:

- one HTTP listener receives all GSI payloads
- one in-memory `latest_snapshot` is updated on every POST
- when a filtered event batch is emitted, the prompt runtime builds:
  - `Instruction` as the text LLM system prompt
  - `Gameplay snapshot` as the filtered JSON batch
- the text LLM returns one short plain-text commentary sentence
- that sentence is sent straight into TTS playback
- prompt/runtime inputs and outputs are stored locally for inspection

TTS backend notes:

- the runtime expects an OpenAI-compatible `POST /v1/audio/speech` server
- the live queue/playback path requires true audio streaming and PCM chunks
- the current supported local backend is the Qwen OpenAI-compatible FastAPI server under `deployment/tts-io-full/Qwen3-TTS-Openai-Fastapi`

Important constraints:

- same caster voice for now
- no queueing logic for now; each filtered batch is handled immediately
- the instruction tells the model not to think and to prefer short, event-first sentences
- prompt logic should not need to clean up names or strip internal ids; the filtered batch should already be clean
- text generation now uses the existing `deployment/text-llm` OpenAI-compatible endpoint
- TTS dispatch stays immediate and backgrounded so GSI POSTs are acknowledged quickly
- if the text model is unreliable with JSON, use the plain-text variant below

Run:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline.py
```

Run the fresh v2 script:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_v2.py
```

Run the raw/filtered-only v3 script:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/gsi/pipeline/gsi_prompt_pipeline_v3.py
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
- `pipeline/.state/v2/prompt_runtime_pretty.jsonl`
- `pipeline/.state/v2/prompt_runtime_latest.json`
- `pipeline/.state/v2/pipeline_v2.log`

V3 output files:

- `pipeline/.state/v3/gsi_received_pretty.jsonl`
- `pipeline/.state/v3/gsi_received_latest.json`
- `pipeline/.state/v3/gsi_filtered_pretty.jsonl`
- `pipeline/.state/v3/gsi_filtered_latest.json`
- `pipeline/.state/v3/training_wrapper_pretty.jsonl`
- `pipeline/.state/v3/training_wrapper_latest.json`
- `pipeline/.state/v3/prompt_runtime_pretty.jsonl`
- `pipeline/.state/v3/prompt_runtime_latest.json`
- `pipeline/.state/v3/prompt_queue_state.json`
- `pipeline/.state/v3/pipeline_v3.log`

V3 training wrapper:

- stores a single top-level `input` object matching the intended text-model prompt shape
- includes a compact `context`
- includes `context.score`
- includes `context.alive_players` so prompt examples can reference current live player positions by callout
- includes `previous_events` from only the single last important filtered event context
- includes `current_events` as the full current filtered event batch
- includes a compact `request` block describing the intended response bundle
- trims `previous_events` down to a slimmer context shape for prompt examples
- is intended as a training/prompt-design helper without sending raw GSI to the model

What gets stored:

- raw GSI payload history and latest snapshot
- filtered event history and latest prompt-ready batch
- training-facing wrapper examples matching the intended text-model input shape
- prompt/runtime records for event prompts and idle interval prompts
- queue state for current and pending TTS lines
