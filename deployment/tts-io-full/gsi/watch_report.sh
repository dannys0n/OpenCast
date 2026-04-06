#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/.console/runtime_report.log"

mkdir -p "$(dirname "${LOG_FILE}")"
touch "${LOG_FILE}"

echo "Watching runtime observations: ${LOG_FILE}"
tail -n 80 -f "${LOG_FILE}"
