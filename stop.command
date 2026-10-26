#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"
unset PYTHONHOME PYTHONPATH
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
PYTHON_BIN="$(command -v python3 || echo /usr/bin/python3)"
[ -x .venv/bin/python ] && PYTHON_BIN=.venv/bin/python
"$PYTHON_BIN" stop.py
read -r -p "按回车键关闭…"
