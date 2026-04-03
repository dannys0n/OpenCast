#!/usr/bin/env bash

set -euo pipefail
python3 manual_tts.py --requests-json '[
  {"voice": "june", "text": "Blue team crack the fight wide open."},
  {"voice": "scotty", "text": "Thats right June, a massive swing in momentum."}
]'
