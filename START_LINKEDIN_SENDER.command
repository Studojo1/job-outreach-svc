#!/bin/bash
# LinkedIn Sender — double-click to launch
# Requires Python 3.10+ and: pip install flask curl_cffi

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Use .venv if available, otherwise fall back to system python
if [ -f ".venv/bin/python3" ]; then
  PYTHON=".venv/bin/python3"
else
  PYTHON="python3"
fi

# Install dependencies if missing
$PYTHON -c "import flask, curl_cffi" 2>/dev/null || {
  echo "Installing dependencies..."
  $PYTHON -m pip install flask curl_cffi -q
}

$PYTHON linkedin_sender.py &
sleep 2
open http://localhost:5050
wait
