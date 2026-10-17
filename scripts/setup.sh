#!/usr/bin/env bash
# Linux/macOS: create a virtualenv, install dependencies and run the test suite.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pytest
echo
echo "Setup complete. Activate with: source .venv/bin/activate"
echo "Try the offline demo:        workflow-agent demo"
