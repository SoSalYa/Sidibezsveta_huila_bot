#!/usr/bin/env bash
set -euo pipefail

# start keepalive server in background
python dtek_api.py &
API_PID=$!
echo "Started dtek_api.py (pid=$API_PID)"

# trap to kill background server when script exits
_cleanup() {
  echo "Stopping background api (pid=$API_PID)..."
  kill "$API_PID" 2>/dev/null || true
}
trap _cleanup EXIT

# run the bot (this process holds the container)
python main.py