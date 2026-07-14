#!/usr/bin/env bash
# Install Attune in editable mode and run the full test suite.
set -euo pipefail
cd "$(dirname "$0")/.."

pip install -e ".[dev]"

pytest -q
pip install -r deploy/republisher/requirements.txt
pytest deploy/republisher/test_main.py -q
echo "All tests passed."
