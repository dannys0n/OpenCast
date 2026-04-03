# Local Esports Commentary Stack

This repo is reduced to two working directories:

- `tts-io/` for Qwen3-TTS
- `text-llm/` for the small text model
- `testing/` for manual prompt and TTS test scripts

## TTS IO

Files:

- `tts-io/setup_linux_env.sh`
- `tts-io/start_tts_model.sh`
- `tts-io/add_custom_voice.sh`
- `tts-io/make_speaker_embedding.py`
- `tts-io/stream_tts.py`

Flow:

```bash
sh tts-io/setup_linux_env.sh
sh tts-io/start_tts_model.sh
```

In another terminal:

```bash
sh tts-io/add_custom_voice.sh
```

That step reads every source file under `tts-io/voices/`, converts each one to a compatible mono 24 kHz WAV, computes a local speaker embedding for each voice, and saves:

- `tts-io/voices/generated/default.env`
- `tts-io/voices/generated/voices.json`
- `tts-io/voices/generated/env/`
- `tts-io/voices/generated/embeddings/`

Voice names are derived from the audio filename. For example, `tts-io/voices/June Showcase.m4a` becomes the voice name `june_showcase`.

If you want a specific default caster, set it when you build voices:

```bash
DEFAULT_VOICE_NAME="june_showcase" sh tts-io/add_custom_voice.sh
```

Direct TTS test:

```bash
source .venv/bin/activate
source tts-io/voices/generated/default.env
python tts-io/stream_tts.py --speaker-embedding-file "$CUSTOM_VOICE_EMBEDDING_FILE" \
  "Team Alpha are pushing through mid. That is a huge opening pick."
```

Manual multi-caster TTS test from a JSON array:

```bash
python testing/manual_tts.py --requests-json '[
  {"voice": "june", "text": "Blue team crack the fight wide open."},
  {"voice": "scotty", "text": "That is a massive swing in momentum."}
]'
```

## Text LLM

Files:

- `text-llm/start_text_model.sh`
- `text-llm/.env.example`

Copy the example config if you want to change model or defaults:

```bash
cd /text-llm
cp .env.example .env
```

Then edit `text-llm/.env` and set `MODEL_NAME` to whichever model you want, for example:

```bash
MODEL_NAME="hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
```

or:

```bash
MODEL_NAME="hf.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"
```

Start the text model:

```bash
cd /text-llm
./start_text_model.sh
```

End-to-end prompt to TTS:

```bash
./testing/prompt_to_tts.sh "Call a clutch team wipe in one or two short esports lines."
```

To select a specific caster that was built from `tts-io/voices/`, set `VOICE_NAME`:

```bash
VOICE_NAME="june_showcase" ./testing/prompt_to_tts.sh "Call a clutch team wipe in one or two short esports lines."
```

## Testing

Files:

- `testing/prompt_to_tts.sh`
- `testing/manual_tts.py`

These are manual test harnesses. They use the `text-llm/.env` config, the generated voice manifest under `tts-io/voices/generated/`, and the live TTS websocket endpoint.

## Notes

- The TTS path uses the WebSocket streaming text-input endpoint.
- Voice inputs live in `tts-io/voices/`; generated normalized audio, env files, and embeddings live under `tts-io/voices/generated/`.
- The custom voice is a local precomputed speaker embedding, not a server-side uploaded WAV.
- The repo uses `Base + x-vector-only` only, with `speaker_embedding` passed directly.
- Audio is played live through SoX `play`.
