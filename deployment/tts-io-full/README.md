# TTS IO Full

This workspace now has a direct streaming path that does not require the `vllm` speech server.

## Direct Streaming

The main entrypoint is:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/stream_tts.py "That round is blown wide open."
```

It uses:

- `deployment/tts-io-full/Qwen3-TTS-streaming`
- local voice samples in `deployment/tts-io-full/voices/`
- matching `.txt` transcripts for full sample+transcript cloning
- `deployment/.venv`

## Voice Selection

Voice names are derived from the filenames in `voices/`.

Example:

```text
voices/scrawny E2 S0.wav
voices/scrawny E2 S0.txt
```

becomes:

```text
scrawny_e2_s0
```

The default comes from `TTS_DEFAULT_VOICE_NAME` in `.env`.

List discovered voices with:

```bash
python3 /home/danny/Desktop/OpenCast/deployment/tts-io-full/stream_tts.py --list-voices
```

## Config

Copy the example if needed:

```bash
cp /home/danny/Desktop/OpenCast/deployment/tts-io-full/.env.example /home/danny/Desktop/OpenCast/deployment/tts-io-full/.env
```

The direct path prefers `TTS_LOCAL_MODEL_PATH` so it can use your locally cached model snapshot without downloading.
