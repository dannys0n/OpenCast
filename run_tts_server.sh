#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OPENCAST_REPO_ROOT="${REPO_ROOT}"

exec bash "${REPO_ROOT}/tts-model/run_tts_server_impl.sh" "$@"
