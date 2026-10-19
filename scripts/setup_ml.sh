#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ ! -x .venv-ml/bin/python ]]; then
  uv venv --python 3.12 .venv-ml
fi
uv pip install --python .venv-ml/bin/python -e '.[ml]'
echo "ML environment ready at .venv-ml"
