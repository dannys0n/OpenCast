# Local Esports Commentary Stack

This repo is reduced to two working directories:

- `tts-io/` for Qwen3-TTS
- `text-llm/` for the small text model

## TTS IO

Files:

- `tts-io/setup_linux_env.sh`
- `tts-io/start_tts_model.sh`
- `tts-io/add_custom_voice.sh`
- `tts-io/make_speaker_embedding.py`
- `tts-io/stream_tts.py`

Flow:

```bash
cd /home/danny/Desktop/vLLM-Omni
sh tts-io/setup_linux_env.sh
sh tts-io/start_tts_model.sh
```

In another terminal:

```bash
cd /home/danny/Desktop/vLLM-Omni
sh tts-io/add_custom_voice.sh
```

That step computes a local speaker embedding from `tts-io/scotty_full.wav` and saves:

- `tts-io/custom_voice.env`
- `tts-io/custom_voice_embedding.json`

Direct TTS test:

```bash
cd /home/danny/Desktop/vLLM-Omni
source .venv/bin/activate
source tts-io/custom_voice.env
python tts-io/stream_tts.py --speaker-embedding-file "$CUSTOM_VOICE_EMBEDDING_FILE" \
  "Team Alpha are pushing through mid. That is a huge opening pick."
```

## Text LLM

Files:

- `text-llm/start_text_model.sh`
- `text-llm/prompt_to_tts.sh`
- `text-llm/.env.example`

Copy the example config if you want to change model or defaults:

```bash
cd /home/danny/Desktop/vLLM-Omni/text-llm
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
cd /home/danny/Desktop/vLLM-Omni/text-llm
./start_text_model.sh
```

End-to-end prompt to TTS:

```bash
cd /home/danny/Desktop/vLLM-Omni
./text-llm/prompt_to_tts.sh "Call a clutch team wipe in one or two short esports lines."
```

## Notes

- The TTS path uses the WebSocket streaming text-input endpoint.
- The custom voice is a local precomputed speaker embedding, not a server-side uploaded WAV.
- The repo uses `Base + x-vector-only` only, with `speaker_embedding` passed directly.
- Audio is played live through SoX `play`.
