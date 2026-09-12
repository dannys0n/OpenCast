#!/usr/bin/env bash
# Idempotent bootstrap for the OpenCast Cloud Agent dev environment.
#
# Scope: this sets up the self-contained, runnable part of the stack -- the CS2
# GSI prompt pipeline (deployment/tts-io-full/gsi/pipeline). That component and
# its test suite depend only on the Python 3.12 standard library.
#
# The rest of the stack (local text LLM via `docker model`, the OmniVoice TTS
# server, SoX audio playback, and a live CS2 client) needs a CUDA GPU, model
# downloads, audio hardware, and a separate omnivoice-server checkout, none of
# which exist in a headless cloud VM. Those pieces are intentionally left to be
# provisioned on a GPU workstation per the repo README.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE_DIR="$REPO_ROOT/deployment/tts-io-full/gsi/pipeline"

echo "==> Python toolchain"
python3 --version

# lsof lets the listener's default port-reclaim path work; best-effort only
# (the pipeline already degrades gracefully when it is absent).
if ! command -v lsof >/dev/null 2>&1; then
  echo "==> Installing lsof (best-effort)"
  sudo apt-get update -y && sudo apt-get install -y --no-install-recommends lsof || \
    echo "   (skipping lsof install; pipeline tolerates its absence)"
fi

echo "==> Byte-compiling the v5 GSI pipeline as a smoke check"
python3 -m py_compile \
  "$PIPELINE_DIR/gsi_prompt_pipeline_v5.py" \
  "$PIPELINE_DIR/prompt_queue_v5.py" \
  "$PIPELINE_DIR/gsi_prompt_pipeline_v2.py" \
  "$PIPELINE_DIR/tts_client.py" \
  "$PIPELINE_DIR/text_llm_client.py" \
  "$PIPELINE_DIR/tactical_rules_v4.py"

echo "==> Running the v5 pipeline unit tests"
( cd "$PIPELINE_DIR" && python3 -m unittest test_gsi_prompt_pipeline_v5 test_prompt_queue_v5 )

echo "==> OpenCast environment ready."
