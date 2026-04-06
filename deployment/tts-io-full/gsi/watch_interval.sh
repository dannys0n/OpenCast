#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/.console/interval.log"

mkdir -p "$(dirname "${LOG_FILE}")"
touch "${LOG_FILE}"

echo "Watching interval snapshots: ${LOG_FILE}"
tail -n 80 -f "${LOG_FILE}"
