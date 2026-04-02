#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OPENCAST_REPO_ROOT="${REPO_ROOT}"

exec bash "${REPO_ROOT}/text-model/setup_commentary_model_runner.sh" "$@"
