#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${REQUEST_STATE_SECRET:?Defina REQUEST_STATE_SECRET com 32 ou mais bytes}"
python3 servidor-mcp/server.py
